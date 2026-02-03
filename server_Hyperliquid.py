import os
import json
import traceback
from decimal import Decimal, InvalidOperation
from fastapi import FastAPI, Request, HTTPException

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# NOTE: eth_account is commonly available via dependencies.
# If you get "ModuleNotFoundError: eth_account", add "eth-account" to requirements.txt.
from eth_account import Account

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

# Skip rules
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
        _info = Info(HL_BASE_URL, skip_ws=True)

    if _exchange is None:
        # Build a wallet from the private key. Keep account_address as the main account.
        wallet = Account.from_key(HL_SECRET_KEY)

        try:
            # Common style: Exchange(wallet=..., base_url=..., account_address=...)
            _exchange = Exchange(wallet=wallet, base_url=HL_BASE_URL, account_address=HL_ACCOUNT_ADDRESS)
        except TypeError:
            # Fallbacks for other SDK variants
            try:
                _exchange = Exchange(wallet, HL_BASE_URL, HL_ACCOUNT_ADDRESS)
            except TypeError:
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

    for suf in [".P", "PERP", "-PERP", "_PERP"]:
        if s.endswith(suf):
            s = s[: -len(suf)]

    if s.endswith("USDT") and len(s) > 4:
        s = s[:-4]

    return s


def is_rate_limited_error(e: Exception) -> bool:
    msg = str(e)
    return ("429" in msg) or ("rate" in msg.lower() and "limit" in msg.lower())


def log_exception(prefix: str, e: Exception):
    """
    Bitget-style logging: prints full error + full traceback in Render logs.
    """
    print(f"\n{prefix}")
    print(str(e))
    print("\n--- TRACEBACK ---")
    print(traceback.format_exc())
    print("--- END TRACEBACK ---\n")


def get_px_step(info: Info, coin: str) -> Decimal:
    """
    Return the price tick size (pxStep) for the given coin from Hyperliquid meta.
    Fallback to 0.1 if anything fails.
    """
    try:
        meta = info.meta()
        universe = meta.get("universe", [])
        for a in universe:
            if a.get("name") == coin and a.get("pxStep") is not None:
                return Decimal(str(a["pxStep"]))
    except Exception as e:
        print("⚠️ Could not fetch pxStep from meta:", e)

    # Safe fallback (BTC is typically 0.1 on many venues, but this is just a fallback)
    return Decimal("0.1")


def round_to_step(x: Decimal, step: Decimal) -> Decimal:
    """
    Round DOWN to the nearest valid tick (step).
    Floor rounding avoids invalid price due to too many decimals.
    """
    if step <= 0:
        return x
    return (x // step) * step


def order_ok_or_error(main_result: dict) -> str | None:
    """
    Hyperliquid often returns HTTP 200 with embedded order errors.
    Return error string if present, otherwise None.
    """
    try:
        statuses = main_result.get("response", {}).get("data", {}).get("statuses", [])
        for s in statuses:
            if isinstance(s, dict) and "error" in s:
                return str(s["error"])
    except Exception:
        return "Unknown order error (could not parse statuses)"
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

    # If some relay wrapped JSON into a quoted string, unquote it
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].replace('\\"', '"')

    try:
        data = json.loads(text)
    except Exception as e:
        log_exception("❌ INVALID JSON BODY ❌", e)
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

    coin = normalize_tv_symbol_to_hl(data.get("symbol", ""))

    action = str(data.get("action", "")).lower()
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

    tp_trigger = to_decimal(extra.get("tp_trigger") or data.get("tp_trigger"))
    sl_trigger = to_decimal(extra.get("sl") or data.get("sl"))
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
        log_exception("❌ HYPERLIQUID CLIENT INIT FAILED ❌", e)
        raise HTTPException(status_code=500, detail=f"Hyperliquid client init failed: {e}")

    # --- Place main order ---
    try:
        if order_type == "market":
            slippage = Decimal(str(HL_SLIPPAGE))

            mids = info.all_mids()
            if coin not in mids:
                raise HTTPException(status_code=400, detail=f"Unknown coin for mids: {coin}")

            mid = Decimal(str(mids[coin]))

            # Compute aggressive IOC price then round to tick size
            px_raw = mid * (Decimal("1") + slippage) if is_buy else mid * (Decimal("1") - slippage)
            px_step = get_px_step(info, coin)
            px = round_to_step(px_raw, px_step)

            print(f"\n=== PRICE DEBUG === coin={coin} mid={mid} px_raw={px_raw} px_step={px_step} px_rounded={px}")

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

            # Round limit price to tick too
            px_step = get_px_step(info, coin)
            lp = round_to_step(Decimal(str(limit_price)), px_step)
            print(f"\n=== LIMIT PRICE DEBUG === coin={coin} limit_price_raw={limit_price} px_step={px_step} limit_price_rounded={lp}")

            main_result = exchange.order(
                coin,
                is_buy,
                float(sz),
                float(lp),
                {"limit": {"tif": "Gtc"}},
                reduce_only=reduce_only_bool,
            )

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported order_type: {order_type}")

    except Exception as e:
        log_exception("❌❌❌ HYPERLIQUID LIVE ORDER FAILED ❌❌❌", e)

        if is_rate_limited_error(e):
            raise HTTPException(
                status_code=503,
                detail="Hyperliquid rate limited (429). Retry in a few seconds.",
            )

        raise HTTPException(
            status_code=500,
            detail=f"Hyperliquid order failed: {e}",
        )

    print("\n=== HL MAIN ORDER RESPONSE ===")
    print(main_result)

    # If HL returns embedded error despite HTTP 200, surface it
    embedded_err = order_ok_or_error(main_result)
    if embedded_err:
        print("\n❌❌❌ HYPERLIQUID EMBEDDED ORDER ERROR ❌❌❌")
        print(embedded_err)
        raise HTTPException(status_code=500, detail=f"Hyperliquid order error: {embedded_err}")

    # --- Optional TP/SL trigger orders ---
    tpsl_results = []
    tpsl_is_buy = not is_buy  # opposite side

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
        log_exception("⚠️ TP/SL PLACEMENT WARNING ⚠️", e)
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
