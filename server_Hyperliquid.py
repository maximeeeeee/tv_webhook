import os
import json
import traceback
from decimal import Decimal, InvalidOperation
from time import time
from typing import Optional

from fastapi import FastAPI, Request, HTTPException

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from eth_account import Account

app = FastAPI()

TV_WEBHOOK_TOKEN = os.getenv("TV_WEBHOOK_TOKEN", "CHANGE_ME")

HL_ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS", "")
HL_SECRET_KEY = os.getenv("HL_SECRET_KEY", "")
HL_BASE_URL = os.getenv("HL_BASE_URL", constants.MAINNET_API_URL)

HL_LIVE_TRADING = os.getenv("HL_LIVE_TRADING", "false").lower() == "true"
HL_SLIPPAGE = os.getenv("HL_SLIPPAGE", "0.01")

TV_SKIP_ORDER_IDS = {"Exit Long", "Exit Short"}

_info = None
_exchange = None

_seen = {}
DEDUP_TTL = 60


def seen_recently(k: str) -> bool:
    now = time()
    for key, ts in list(_seen.items()):
        if now - ts > DEDUP_TTL:
            _seen.pop(key, None)
    if not k:
        return False
    if k in _seen:
        return True
    _seen[k] = now
    return False


def get_hl_clients():
    global _info, _exchange

    if not HL_ACCOUNT_ADDRESS or not HL_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Missing HL_ACCOUNT_ADDRESS or HL_SECRET_KEY in Render env vars",
        )

    if _info is None:
        _info = Info(HL_BASE_URL, skip_ws=True)

    if _exchange is None:
        wallet = Account.from_key(HL_SECRET_KEY)
        try:
            _exchange = Exchange(wallet=wallet, base_url=HL_BASE_URL, account_address=HL_ACCOUNT_ADDRESS)
        except TypeError:
            try:
                _exchange = Exchange(wallet, HL_BASE_URL, HL_ACCOUNT_ADDRESS)
            except TypeError:
                _exchange = Exchange(HL_ACCOUNT_ADDRESS, HL_SECRET_KEY, base_url=HL_BASE_URL)

    return _info, _exchange


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
    s = str(symbol).strip().upper()
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
    print(f"\n{prefix}")
    print(str(e))
    print("\n--- TRACEBACK ---")
    print(traceback.format_exc())
    print("--- END TRACEBACK ---\n")


_meta_cache = {"ts": 0, "data": None}
META_TTL_SEC = 300


def get_meta_cached(info: Info):
    now = time()
    if _meta_cache["data"] is None or now - _meta_cache["ts"] > META_TTL_SEC:
        _meta_cache["data"] = info.meta()
        _meta_cache["ts"] = now
    return _meta_cache["data"]


def round_to_step(x: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return x
    n = (x / step).to_integral_value(rounding="ROUND_FLOOR")
    return n * step


def infer_px_step_from_l2(info: Info, coin: str) -> Optional[Decimal]:
    try:
        snap = info.l2_snapshot(coin)
        levels = snap.get("levels", [])
        if not levels or len(levels) < 2:
            return None

        def extract_prices(side_levels):
            prices = []
            for lvl in side_levels:
                if isinstance(lvl, (list, tuple)) and len(lvl) >= 1:
                    prices.append(Decimal(str(lvl[0])))
                elif isinstance(lvl, dict) and "px" in lvl:
                    prices.append(Decimal(str(lvl["px"])))
            return prices

        bids = extract_prices(levels[0]) if len(levels) > 0 else []
        asks = extract_prices(levels[1]) if len(levels) > 1 else []

        prices = sorted(set(bids + asks))
        if len(prices) < 2:
            return None

        diffs = []
        for i in range(1, len(prices)):
            d = prices[i] - prices[i - 1]
            if d > 0:
                diffs.append(d)

        if not diffs:
            return None

        return min(diffs)

    except Exception as e:
        print(f"⚠️ Could not infer pxStep from L2 for {coin}: {e}")
        return None


def get_px_step(info: Info, coin: str) -> Decimal:
    try:
        meta = get_meta_cached(info)
        universe = meta.get("universe", [])
        for a in universe:
            if str(a.get("name", "")).upper() == coin.upper():
                if a.get("pxStep") is not None:
                    step = Decimal(str(a["pxStep"]))
                    print(f"✅ pxStep from meta: coin={coin} pxStep={step}")
                    return step
                if a.get("pxDecimals") is not None:
                    d = int(a["pxDecimals"])
                    step = Decimal("1") / (Decimal("10") ** d)
                    print(f"✅ pxStep from meta(pxDecimals): coin={coin} pxStep={step}")
                    return step
        print(f"⚠️ pxStep NOT FOUND in meta() for coin={coin}. Trying L2 inference...")
    except Exception as e:
        print("⚠️ meta() pxStep lookup failed:", e)

    inferred = infer_px_step_from_l2(info, coin)
    if inferred is not None:
        print(f"✅ Inferred pxStep from L2: coin={coin} pxStep={inferred}")
        return inferred

    print(f"⚠️ Falling back to pxStep=1 for coin={coin}")
    return Decimal("1")


def get_sz_step(info: Info, coin: str) -> Decimal:
    try:
        meta = get_meta_cached(info)
        universe = meta.get("universe", [])
        for a in universe:
            if str(a.get("name", "")).upper() == coin.upper():
                if a.get("szStep") is not None:
                    step = Decimal(str(a["szStep"]))
                    print(f"✅ szStep from meta: coin={coin} szStep={step}")
                    return step
                if a.get("szDecimals") is not None:
                    d = int(a["szDecimals"])
                    step = Decimal("1") / (Decimal("10") ** d)
                    print(f"✅ szStep from meta(szDecimals): coin={coin} szStep={step}")
                    return step
    except Exception as e:
        print("⚠️ Could not fetch szStep from meta:", e)

    return Decimal("0.001")


def tick_ok(px: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return True
    q = px / step
    return q == q.to_integral_value()


def order_ok_or_error(main_result: dict):
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

    try:
        info, exchange = get_hl_clients()
    except Exception as e:
        log_exception("❌ HYPERLIQUID CLIENT INIT FAILED ❌", e)
        raise HTTPException(status_code=500, detail=f"Hyperliquid client init failed: {e}")

    try:
        sz_step = get_sz_step(info, coin)
        sz_rounded = round_to_step(sz, sz_step)
        if sz_rounded <= 0:
            raise HTTPException(status_code=400, detail=f"Qty too small after rounding to lot step: {sz_step}")
        if sz_rounded != sz:
            print(f"ℹ️ Size rounded: raw_sz={sz} sz_step={sz_step} sz_rounded={sz_rounded}")
        sz = sz_rounded
    except HTTPException:
        raise
    except Exception as e:
        log_exception("⚠️ SIZE STEP CHECK WARNING ⚠️", e)

    px_step = get_px_step(info, coin)

    # ✅ FIX: Hyperliquid SDK expects triggerPx as FLOAT (not str)
    def round_trigger_px_float(v: Decimal) -> float:
        return float(round_to_step(Decimal(str(v)), px_step))

    try:
        if order_type == "market":
            slippage = Decimal(str(HL_SLIPPAGE))

            mids = info.all_mids()
            if coin not in mids:
                raise HTTPException(status_code=400, detail=f"Unknown coin for mids: {coin}")

            mid = Decimal(str(mids[coin]))
            px_raw = mid * (Decimal("1") + slippage) if is_buy else mid * (Decimal("1") - slippage)
            px = round_to_step(px_raw, px_step)

            print(f"\n=== PRICE DEBUG === coin={coin} mid={mid} px_raw={px_raw} px_step={px_step} px_rounded={px}")
            print(f"=== TICK CHECK === px={px} px_step={px_step} tick_ok={tick_ok(px, px_step)}")

            if not tick_ok(px, px_step):
                raise HTTPException(status_code=500, detail=f"Computed px is not divisible by tick: px={px} step={px_step}")

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

            lp = round_to_step(Decimal(str(limit_price)), px_step)

            print(f"\n=== LIMIT PRICE DEBUG === coin={coin} limit_price_raw={limit_price} px_step={px_step} limit_price_rounded={lp}")
            print(f"=== TICK CHECK === lp={lp} px_step={px_step} tick_ok={tick_ok(lp, px_step)}")

            if not tick_ok(lp, px_step):
                raise HTTPException(status_code=500, detail=f"Limit price not divisible by tick: lp={lp} step={px_step}")

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
            raise HTTPException(status_code=503, detail="Hyperliquid rate limited (429). Retry in a few seconds.")
        raise HTTPException(status_code=500, detail=f"Hyperliquid order failed: {e}")

    print("\n=== HL MAIN ORDER RESPONSE ===")
    print(main_result)

    embedded_err = order_ok_or_error(main_result)
    if embedded_err:
        print("\n❌❌❌ HYPERLIQUID EMBEDDED ORDER ERROR ❌❌❌")
        print(embedded_err)
        raise HTTPException(status_code=500, detail=f"Hyperliquid order error: {embedded_err}")

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
                reduce_only=True,
            )
            tpsl_results.append({"tp": tp_res})

        if sl_trigger:
            sl_res = exchange.order(
                coin,
                tpsl_is_buy,
                float(sz),
                0.0,
                {"trigger": {"isMarket": True, "triggerPx": round_trigger_px_float(sl_trigger), "tpsl": "sl"}},
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
