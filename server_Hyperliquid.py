import os
import json
from decimal import Decimal, InvalidOperation
from fastapi import FastAPI, Request, HTTPException

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

app = FastAPI()

# ===============================
# TradingView security
# ===============================
TV_WEBHOOK_TOKEN = os.getenv("TV_WEBHOOK_TOKEN", "CHANGE_ME")

# ===============================
# Hyperliquid credentials
# ===============================
HL_ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS", "")
HL_SECRET_KEY = os.getenv("HL_SECRET_KEY", "")

# Mainnet by default
HL_BASE_URL = os.getenv("HL_BASE_URL", constants.MAINNET_API_URL)

# LIVE / SAFE MODE
HL_LIVE_TRADING = os.getenv("HL_LIVE_TRADING", "false").lower() == "true"

# Market slippage tolerance (used for market-style IOC pricing)
HL_SLIPPAGE = os.getenv("HL_SLIPPAGE", "0.01")  # 1% default

# Skip rules (same as your Bitget server)
TV_SKIP_ORDER_IDS = {"Exit Long", "Exit Short"}

# ===============================
# Lazy init (IMPORTANT)
# Avoid API calls at import time to prevent Render crash on 429
# ===============================
_info = None
_exchange = None


def get_hl_clients():
    """
    Create Info/Exchange only when needed (when /tv is hit).
    Prevents startup crash due to transient 429 rate limits.
    """
    global _info, _exchange

    if not HL_ACCOUNT_ADDRESS or not HL_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Missing HL_ACCOUNT_ADDRESS or HL_SECRET_KEY in Render env vars",
        )

    if _info is None:
        # Note: Info() may call /info internally; we do it only on demand
        _info = Info(HL_BASE_URL, skip_ws=True)

    if _exchange is None:
        _exchange = Exchange(HL_ACCOUNT_ADDRESS, HL_SECRET_KEY, base_url=HL_BASE_URL)

    return _info, _exchange


# ===============================
# Helpers
# ===============================
def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s != "" else None


def parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def to_decimal(v):
    v = clean(v)
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def normalize_tv_symbol_to_hl(symbol: str) -> str:
    """
    TradingView sends symbols like BTCUSDT / BTCUSDT.P / ETHUSDT etc.
    Hyperliquid perp "coin" is usually BTC / ETH / etc.
    """
    s = str(symbol).strip().upper()

    # Remove common suffixes
    for suf in [".P", "PERP", "-PERP", "_PERP"]:
        if s.endswith(suf):
            s = s[: -len(suf)]

    # Convert BTCUSDT -> BTC
    if s.endswith("USDT") and len(s) > 4:
        s = s[:-4]

    return s


def is_rate_limited_error(e: Exception) -> bool:
    """
    Hyperliquid SDK raises ClientError with text containing "(429, ...)".
    We detect 429 robustly via string match.
    """
    msg = str(e)
    return "429" in msg or "rate" in msg.lower() and "limit" in msg.lower()


# ===============================
# Webhook
# ===============================
@app.post("/tv")
async def tv_webhook(req: Request):
    print("\n✅✅✅ /tv HIT (request received) ✅✅✅")
    print(f"content-type: {req.headers.get('content-type')}")

    # tolerant JSON parsing (works for application/json AND text/plain)
    raw = await req.body()
    text = raw.decode("utf-8", errors="replace").strip()

    # If some relay wrapped JSON into a quoted string, unquote it
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].replace('\\"', '"')

    try:
        data = json.loads(text)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if data.get("token") != TV_WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")

    print("\n=== TradingView payload ===")
    print(data)

    if data.get("type") != "order":
        return {"ok": True, "mode": "ignored"}

    tv_order_id = str(data.get("tv_order_id", "")).strip()
    tv_comment = str(data.get("tv_comment", "")).strip()

    if tv_order_id in TV_SKIP_ORDER_IDS:
        print(f"⏭️ SKIPPED: {tv_order_id}")
        return {"ok": True, "mode": "skipped"}

    extra = data.get("extra") or {}

    # --- Extract fields ---
    coin = normalize_tv_symbol_to_hl(data.get("symbol", ""))

    action = str(data.get("action", "")).lower()  # "buy" or "sell"
    if action not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="Invalid action (must be buy or sell)")
    is_buy = action == "buy"

    qty = clean(data.get("qty"))
    if qty is None:
        raise HTTPException(status_code=400, detail="Missing qty")

    sz = to_decimal(qty)
    if sz is None or sz <= 0:
        raise HTTPException(status_code=400, detail="Invalid qty")

    order_type = (extra.get("order_type") or data.get("order_type") or "market").lower()
    reduce_only_bool = parse_bool(extra.get("reduce_only", data.get("reduce_only", False)))

    # Optional TP/SL from TV
    tp_trigger = to_decimal(extra.get("tp_trigger") or data.get("tp_trigger"))
    sl_trigger = to_decimal(extra.get("sl") or data.get("sl"))

    # Optional limit price
    limit_price = to_decimal(extra.get("price") or data.get("price"))

    print("\n=== Parsed ===")
    print(
        f"coin={coin} is_buy={is_buy} sz={sz} order_type={order_type} "
        f"reduce_only={reduce_only_bool} tp_trigger={tp_trigger} sl_trigger={sl_trigger} limit_price={limit_price}"
    )

    # --- SAFE MODE ---
    if not HL_LIVE_TRADING:
        print("⚠️ SAFE MODE — not sent")
        return {
            "ok": True,
            "mode": "safe",
            "coin": coin,
            "is_buy": is_buy,
            "sz": str(sz),
            "order_type": order_type,
            "reduce_only": reduce_only_bool,
            "tp_trigger": str(tp_trigger) if tp_trigger else None,
            "sl_trigger": str(sl_trigger) if sl_trigger else None,
            "tv_order_id": tv_order_id,
            "tv_comment": tv_comment,
        }

    # --- Lazy init clients (only now) ---
    try:
        info, exchange = get_hl_clients()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hyperliquid client init failed: {e}")

    # --- Place main order ---
    try:
        if order_type == "market":
            slippage = Decimal(str(HL_SLIPPAGE))

            # market_close/market_open wrappers in SDK might not expose reduce_only in all versions
            # So we do aggressive IOC limit ourselves to support reduce-only reliably.
            mids = info.all_mids()  # may rate-limit sometimes
            if coin not in mids:
                raise HTTPException(status_code=400, detail=f"Unknown coin for mids: {coin}")

            mid = Decimal(str(mids[coin]))
            px = mid * (Decimal("1") + slippage) if is_buy else mid * (Decimal("1") - slippage)

            main_result = exchange.order(
                coin,
                is_buy,
                float(sz),
                float(px),
                {"limit": {"tif": "Ioc"}},
                reduce_only=reduce_only_bool,
            )

        elif order_type == "limit":
            if limit_price is None:
                raise HTTPException(status_code=400, detail="Limit order requires price")

            main_result = exchange.order(
                coin,
                is_buy,
                float(sz),
                float(limit_price),
                {"limit": {"tif": "Gtc"}},
                reduce_only=reduce_only_bool,
            )

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported order_type: {order_type}")

    except Exception as e:
        if is_rate_limited_error(e):
            raise HTTPException(status_code=503, detail="Hyperliquid rate limited (429). Retry in a few seconds.")
        raise HTTPException(status_code=500, detail=f"Main order failed: {e}")

    print("\n=== HL MAIN ORDER RESPONSE ===")
    print(main_result)

    # --- Optional TP/SL as trigger orders (separate orders) ---
    # For a long entry (buy), TP/SL are sells; for a short entry (sell), TP/SL are buys.
    tpsl_results = []
    tpsl_is_buy = not is_buy

    try:
        if tp_trigger:
            tp_res = exchange.order(
                coin,
                tpsl_is_buy,
                float(sz),
                0.0,
                {"trigger": {"isMarket": True, "triggerPx": str(tp_trigger), "tpsl": "tp"}},
                reduce_only=True,
            )
            tpsl_results.append({"tp": tp_res})

        if sl_trigger:
            sl_res = exchange.order(
                coin,
                tpsl_is_buy,
                float(sz),
                0.0,
                {"trigger": {"isMarket": True, "triggerPx": str(sl_trigger), "tpsl": "sl"}},
                reduce_only=True,
            )
            tpsl_results.append({"sl": sl_res})

    except Exception as e:
        # Do not fail the whole webhook if TP/SL fails
        if is_rate_limited_error(e):
            tpsl_results.append({"warning": "TP/SL not placed due to 429 rate limit. Retry later."})
        else:
            tpsl_results.append({"warning": f"TP/SL not placed: {e}"})

    return {
        "ok": True,
        "mode": "live",
        "main": main_result,
        "tpsl": tpsl_results,
        "tv_order_id": tv_order_id,
        "tv_comment": tv_comment,
    }
