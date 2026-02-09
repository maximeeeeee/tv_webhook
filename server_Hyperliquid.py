import os
import json
import traceback
from decimal import Decimal, InvalidOperation
from time import time

from fastapi import FastAPI, Request, HTTPException

from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

# NOTE: If you get "ModuleNotFoundError: eth_account", add "eth-account" to requirements.txt.
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

# Market slippage tolerance (unused now; we send true market)
HL_SLIPPAGE = os.getenv("HL_SLIPPAGE", "0.01")  # kept for compatibility

# Skip rules
TV_SKIP_ORDER_IDS = {"Exit Long", "Exit Short"}

# ===============================
# Hardcoded BTC rules (Render-safe)
# ===============================
BTC_SZ_STEP = Decimal("0.00001")  # minimal size increment
BTC_PX_STEP = Decimal("1")        # price tick increment (no decimals)

# ===============================
# Lazy init
# ===============================
_exchange = None

# ===============================
# Simple idempotency (prevents TV retry duplicates)
# In-memory dedup (single instance). For multi-instance, use Redis.
# ===============================
_seen = {}  # tv_order_id -> ts
DEDUP_TTL = 60  # seconds


def seen_recently(k: str) -> bool:
    now = time()
    # cleanup
    for key, ts in list(_seen.items()):
        if now - ts > DEDUP_TTL:
            _seen.pop(key, None)
    if not k:
        return False
    if k in _seen:
        return True
    _seen[k] = now
    return False


def get_hl_exchange():
    """
    Create Exchange only when needed (when /tv is hit).
    IMPORTANT: No Info() calls (Render gets 429 on /info spotMeta)
    """
    global _exchange

    if not HL_ACCOUNT_ADDRESS or not HL_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Missing HL_ACCOUNT_ADDRESS or HL_SECRET_KEY in env vars",
        )

    if _exchange is None:
        wallet = Account.from_key(HL_SECRET_KEY)

        # Support multiple SDK signatures
        try:
            _exchange = Exchange(wallet=wallet, base_url=HL_BASE_URL, account_address=HL_ACCOUNT_ADDRESS)
        except TypeError:
            try:
                _exchange = Exchange(wallet, HL_BASE_URL, HL_ACCOUNT_ADDRESS)
            except TypeError:
                _exchange = Exchange(HL_ACCOUNT_ADDRESS, HL_SECRET_KEY, base_url=HL_BASE_URL)

    return _exchange


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
    TradingView sends symbols like BTCUSDT / BTCUSDT.P / BINANCE:BTCUSDT etc.
    Hyperliquid perp "coin" is usually BTC / ETH / etc.
    """
    s = str(symbol).strip().upper()

    # Handle prefixes like BINANCE:BTCUSDT
    if ":" in s:
        s = s.split(":")[-1]

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
    Bitget-style logging: prints full error + full traceback in logs.
    """
    print(f"\n{prefix}")
    print(str(e))
    print("\n--- TRACEBACK ---")
    print(traceback.format_exc())
    print("--- END TRACEBACK ---\n")


def round_to_step(x: Decimal, step: Decimal) -> Decimal:
    """
    Round DOWN to a valid tick/step size using Decimal arithmetic.
    """
    if step <= 0:
        return x
    n = (x / step).to_integral_value(rounding="ROUND_FLOOR")
    return n * step


def order_ok_or_error(main_result: dict):
    """
    Hyperliquid often returns HTTP 200 with embedded order errors.
    Return error string if present, otherwise None.
    """
    try:
        statuses = main_result.get("response", {}).get("data", {}).get("statuses", [])
        for s in statuses:
            if isinstance(s, dict) and "error" in s:
                return str(s["error"])
        if not statuses:
            return "No statuses returned (suspicious response)"
    except Exception:
        return "Unknown order error (could not parse statuses)"
    return None


def status_has_key(main_result: dict, key: str) -> bool:
    """
    True if HL response statuses contains a dict with the given key (e.g. 'resting', 'filled').
    """
    try:
        statuses = main_result.get("response", {}).get("data", {}).get("statuses", [])
        return any(isinstance(s, dict) and key in s for s in statuses)
    except Exception:
        return False


# ===============================
# Webhook
# ===============================
@app.post("/tv")
async def tv_webhook(req: Request):
    print("\n✅✅✅ /tv HIT (request received) ✅✅✅")
    print(f"content-type: {req.headers.get('content-type')}")

    raw = await req.body()
    text = raw.decode("utf-8", errors="replace").strip()

    # handle body being double-quoted JSON string
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

    # Idempotency: ignore duplicates from retries
    if seen_recently(tv_order_id):
        print(f"⏭️ DUPLICATE webhook ignored: {tv_order_id}")
        return {"ok": True, "mode": "deduped", "tv_order_id": tv_order_id}

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

    # --- Init exchange (Render-safe: no Info()) ---
    try:
        exchange = get_hl_exchange()
    except Exception as e:
        log_exception("❌ HYPERLIQUID EXCHANGE INIT FAILED ❌", e)
        raise HTTPException(status_code=500, detail=f"Hyperliquid exchange init failed: {e}")

    # --- Hardcode BTC steps ---
    px_step = BTC_PX_STEP if coin.upper() == "BTC" else Decimal("0")  # 0 => no rounding for other coins (for now)
    sz_step = BTC_SZ_STEP if coin.upper() == "BTC" else Decimal("0")

    if sz_step > 0:
        sz_rounded = round_to_step(sz, sz_step)
        if sz_rounded <= 0:
            raise HTTPException(status_code=400, detail=f"Qty too small after rounding to lot step: {sz_step}")
        if sz_rounded != sz:
            print(f"ℹ️ Size rounded: raw_sz={sz} sz_step={sz_step} sz_rounded={sz_rounded}")
        sz = sz_rounded

    def round_trigger_px_float(v: Decimal) -> float:
        if px_step > 0:
            return float(round_to_step(Decimal(str(v)), px_step))
        return float(v)

    # --- Place main order ---
    try:
        if order_type == "market":
            # True market order (no Info calls)
            main_result = exchange.order(
                coin,
                is_buy,
                float(sz),
                "market",
                reduce_only=reduce_only_bool,
            )

        elif order_type == "limit":
            if limit_price is None:
                raise HTTPException(status_code=400, detail="Limit order requires price")

            lp = Decimal(str(limit_price))
            if px_step > 0:
                lp = round_to_step(lp, px_step)

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
                detail="Hyperliquid rate limited (429). Render IP throttling likely.",
            )

        raise HTTPException(status_code=500, detail=f"Hyperliquid order failed: {e}")

    print("\n=== HL MAIN ORDER RESPONSE ===")
    print(main_result)

    embedded_err = order_ok_or_error(main_result)
    if embedded_err:
        print("\n❌❌❌ HYPERLIQUID EMBEDDED ORDER ERROR ❌❌❌")
        print(embedded_err)
        raise HTTPException(status_code=500, detail=f"Hyperliquid order error: {embedded_err}")

    # Decide TP/SL reduce_only:
    entry_is_resting = status_has_key(main_result, "resting")
    entry_is_filled = status_has_key(main_result, "filled")
    tpsl_reduce_only = False if entry_is_resting else True
    print(f"ℹ️ Entry status: resting={entry_is_resting} filled={entry_is_filled} -> tpsl_reduce_only={tpsl_reduce_only}")

    # --- Optional TP/SL trigger orders ---
    tpsl_results = []
    tpsl_is_buy = not is_buy

    try:
        if tp_trigger:
            tp_res = exchange.order(
                coin,
                tpsl_is_buy,
                float(sz),
                0.0,
                {"trigger": {"isMarket": True, "triggerPx": round_trigger_px_float(tp_trigger), "tpsl": "tp"}},
                reduce_only=tpsl_reduce_only,
            )
            print("\n=== HL TP RESPONSE ===")
            print(tp_res)
            tp_err = order_ok_or_error(tp_res)
            if tp_err:
                print("⚠️ TP embedded error:", tp_err)
            tpsl_results.append({"tp": tp_res, "tp_err": tp_err, "reduce_only": tpsl_reduce_only})

        if sl_trigger:
            sl_res = exchange.order(
                coin,
                tpsl_is_buy,
                float(sz),
                0.0,
                {"trigger": {"isMarket": True, "triggerPx": round_trigger_px_float(sl_trigger), "tpsl": "sl"}},
                reduce_only=tpsl_reduce_only,
            )
            print("\n=== HL SL RESPONSE ===")
            print(sl_res)
            sl_err = order_ok_or_error(sl_res)
            if sl_err:
                print("⚠️ SL embedded error:", sl_err)
            tpsl_results.append({"sl": sl_res, "sl_err": sl_err, "reduce_only": tpsl_reduce_only})

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
