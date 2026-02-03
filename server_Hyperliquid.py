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

# mainnet default
HL_BASE_URL = os.getenv("HL_BASE_URL", constants.MAINNET_API_URL)

# LIVE / SAFE MODE
HL_LIVE_TRADING = os.getenv("HL_LIVE_TRADING", "false").lower() == "true"

# Market slippage tolerance (SDK uses this to compute an aggressive IOC limit price)
HL_SLIPPAGE = os.getenv("HL_SLIPPAGE", "0.01")  # 1% default

# Skip rules
TV_SKIP_ORDER_IDS = {"Exit Long", "Exit Short"}

# ===============================
# Init Hyperliquid clients
# ===============================
if not HL_ACCOUNT_ADDRESS or not HL_SECRET_KEY:
    # We do not hard-crash on import, but we will refuse live trading if missing.
    info = None
    exchange = None
else:
    info = Info(HL_BASE_URL, skip_ws=True)
    exchange = Exchange(HL_ACCOUNT_ADDRESS, HL_SECRET_KEY, base_url=HL_BASE_URL)


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


def normalize_tv_symbol_to_hl(symbol: str) -> str:
    """
    TradingView often sends symbols like BTCUSDT, ETHUSDT, BTCUSDT.P, etc.
    Hyperliquid perps are usually 'BTC', 'ETH', ...
    """
    s = str(symbol).strip().upper()

    # Remove common separators/suffixes
    for suf in [".P", "PERP", "-PERP", "_PERP"]:
        if s.endswith(suf):
            s = s[: -len(suf)]

    # Common TV perp format: BTCUSDT -> BTC
    if s.endswith("USDT") and len(s) > 4:
        s = s[:-4]

    # If you ever trade spot tokens on HL, you might need "@<index>" mapping,
    # but for perps this is fine.
    return s


def to_decimal(v):
    v = clean(v)
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


# ===============================
# Webhook
# ===============================
@app.post("/tv")
async def tv_webhook(req: Request):
    print("\n✅✅✅ /tv HIT (request received) ✅✅✅")
    print(f"content-type: {req.headers.get('content-type')}")

    raw = await req.body()
    text = raw.decode("utf-8", errors="replace").strip()

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
    coin = normalize_tv_symbol_to_hl(data["symbol"])

    action = str(data["action"]).lower()  # "buy" or "sell"
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

    # Optional limit price from TV (if you ever send limit orders)
    limit_price = to_decimal(extra.get("price") or data.get("price"))

    print("\n=== Parsed ===")
    print(
        f"coin={coin} is_buy={is_buy} sz={sz} order_type={order_type} "
        f"reduce_only={reduce_only_bool} tp_trigger={tp_trigger} sl_trigger={sl_trigger} limit_price={limit_price}"
    )

    # --- Safe checks ---
    if exchange is None or info is None:
        raise HTTPException(status_code=500, detail="Hyperliquid clients not configured (HL_ACCOUNT_ADDRESS/HL_SECRET_KEY missing)")

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

    # --- Place main order ---
    try:
        if order_type == "market":
            # If reduce-only, we do not want "market_close all".
            # We place an aggressive IOC order for the provided size with slippage tolerance.
            slippage = float(HL_SLIPPAGE)

            # market_open is a convenience wrapper (IOC + slippage around mid)
            # We can use it for both open and reduce-only closes, but we still need reduce_only.
            # The SDK market_open does not expose reduceOnly, so we fall back to exchange.order for reduce-only.
            if reduce_only_bool:
                mids = info.all_mids()
                mid = Decimal(mids[coin])
                px = mid * (Decimal("1") + Decimal(str(slippage))) if is_buy else mid * (Decimal("1") - Decimal(str(slippage)))
                main_result = exchange.order(coin, is_buy, float(sz), float(px), {"limit": {"tif": "Ioc"}}, reduce_only=True)
            else:
                main_result = exchange.market_open(coin, is_buy, float(sz), None, float(slippage))

        elif order_type == "limit":
            if limit_price is None:
                raise HTTPException(status_code=400, detail="Limit order requires price")
            main_result = exchange.order(coin, is_buy, float(sz), float(limit_price), {"limit": {"tif": "Gtc"}}, reduce_only=reduce_only_bool)

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported order_type: {order_type}")

    except TypeError:
        # In case your installed SDK version doesn't support reduce_only kwarg,
        # you can remove reduce_only usage and instead separate open/close logic.
        raise HTTPException(status_code=500, detail="SDK function signature mismatch. Tell me your installed hyperliquid-python-sdk version and I will adapt.")

    print("\n=== HL MAIN ORDER RESPONSE ===")
    print(main_result)

    # --- Optional TP/SL as trigger orders (separate orders) ---
    # Hyperliquid trigger orders: {"trigger": {"isMarket": bool, "triggerPx": "...", "tpsl": "tp"|"sl"}}
    # Protocol supports this directly. :contentReference[oaicite:5]{index=5}
    tpsl_results = []

    # For a long entry (buy), TP/SL should be sells; for a short entry (sell), TP/SL should be buys.
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

    except TypeError:
        # Same note as above: SDK signature might differ across versions.
        print("⚠️ Could not place TP/SL due to SDK signature mismatch.")
        tpsl_results.append({"warning": "TP/SL not placed (SDK signature mismatch)"})

    return {
        "ok": True,
        "main": main_result,
        "tpsl": tpsl_results,
        "tv_order_id": tv_order_id,
        "tv_comment": tv_comment,
    }
