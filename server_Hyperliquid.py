import os
import json
import traceback
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_FLOOR
from time import time, sleep
from typing import Optional, Tuple, Union, Dict, Any

import requests
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
HL_BASE_URL = os.getenv("HL_BASE_URL", constants.MAINNET_API_URL).rstrip("/")

# LIVE / SAFE MODE
HL_LIVE_TRADING = os.getenv("HL_LIVE_TRADING", "false").lower() == "true"

# Market slippage tolerance (used for market-style IOC pricing)
HL_SLIPPAGE = Decimal(os.getenv("HL_SLIPPAGE", "0.01"))  # 1% default

# Skip rules
TV_SKIP_ORDER_IDS = {"Exit Long", "Exit Short"}

# ===============================
# HARD-CODED STEPS (to avoid meta/info calls)
# ===============================
# BTC rules:
# - Minimal size increment: 0.00001 BTC
# - Price increment: 0.5 (common on HL)
ASSET_STEPS = {
    "BTC": {"sz_step": Decimal("0.00001"), "px_step": Decimal("0.5")},
}

# ===============================
# Lazy init Exchange (SDK still calls /info internally on init => retry)
# ===============================
_exchange: Optional[Exchange] = None

# ===============================
# Idempotency (prevents TV retry duplicates)
# ===============================
_seen = {}  # key -> ts
DEDUP_TTL = 60  # seconds


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


def log_exception(prefix: str, e: Exception):
    print(f"\n{prefix}")
    print(str(e))
    print("\n--- TRACEBACK ---")
    print(traceback.format_exc())
    print("--- END TRACEBACK ---\n")


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


def round_to_step_floor(x: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return x
    n = (x / step).to_integral_value(rounding=ROUND_FLOOR)
    return n * step


def round_to_step_nearest(x: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return x
    n = (x / step).to_integral_value(rounding=ROUND_HALF_UP)
    return n * step


def fmt_px_for_hl(px: Decimal, px_step: Decimal) -> Tuple[Decimal, Union[int, float]]:
    """
    Returns:
      - rounded Decimal px_rounded
      - numeric value for SDK:
          * int when px_step == 1
          * float otherwise
    """
    px_rounded = round_to_step_nearest(px, px_step)
    if px_step == Decimal("1"):
        return px_rounded, int(px_rounded)
    return px_rounded, float(px_rounded)


def is_429(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "rate" in msg.lower()


def get_exchange(max_attempts: int = 5) -> Exchange:
    """
    SDK Exchange() constructor can 429 because it fetches /info internally.
    So we retry Exchange init with backoff.
    """
    global _exchange

    if not HL_ACCOUNT_ADDRESS or not HL_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Missing HL_ACCOUNT_ADDRESS or HL_SECRET_KEY in env vars",
        )

    if _exchange is not None:
        return _exchange

    backoff = 1
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
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

        except Exception as e:
            last_err = e
            if is_429(e):
                print(f"⚠️ Exchange init hit 429 (attempt {attempt}/{max_attempts}). Backing off {backoff}s...")
                sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            print(f"⚠️ Exchange init failed (attempt {attempt}/{max_attempts}): {e}. Backing off {backoff}s...")
            sleep(backoff)
            backoff = min(backoff * 2, 10)

    raise HTTPException(status_code=503, detail=f"Failed to init Exchange after retries: {last_err}")


def fetch_all_mids_with_retry(max_attempts: int = 5):
    url = f"{HL_BASE_URL}/info"
    payload = {"type": "allMids"}

    backoff = 1
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(url, json=payload, timeout=8)
            if r.status_code == 429:
                print(f"⚠️ allMids hit 429 (attempt {attempt}/{max_attempts}). Backing off {backoff}s...")
                sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            print(f"⚠️ allMids fetch failed (attempt {attempt}/{max_attempts}): {e}. Backing off {backoff}s...")
            sleep(backoff)
            backoff = min(backoff * 2, 10)

    raise HTTPException(status_code=503, detail=f"Failed to fetch allMids after retries: {last_err}")


def hl_order_with_retry(
    exchange: Exchange,
    *,
    coin: str,
    is_buy: bool,
    sz: Decimal,
    px_num: Union[int, float],
    tif: str,  # kept for compatibility (not used directly here)
    reduce_only: bool,
    order_type_wire: dict,
    max_attempts: int = 5,
) -> dict:
    """
    Generic order wrapper with 429 retry.
    order_type_wire is the dict you pass as the 5th argument to exchange.order
    (e.g. {"limit":{"tif":"Ioc"}} or {"trigger":{...}})
    """
    backoff = 1
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            return exchange.order(
                coin,
                is_buy,
                float(sz),
                px_num,
                order_type_wire,
                reduce_only=reduce_only,
            )
        except Exception as e:
            last_err = e
            if is_429(e):
                print(f"⚠️ order hit 429 (attempt {attempt}/{max_attempts}). Backing off {backoff}s...")
                sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            raise

    raise HTTPException(status_code=503, detail=f"Order failed after retries (last_err={last_err})")


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

    msg_type = str(data.get("type", "")).strip().lower()
    if msg_type not in ("order", "tpsl"):
        return {"ok": True, "mode": "ignored"}

    extra = data.get("extra") or {}

    # DEDUP KEY
    tv_order_id = str(data.get("tv_order_id", "")).strip()
    if tv_order_id and tv_order_id not in ("Long", "Short") and len(tv_order_id) > 3:
        dedup_key = tv_order_id
    else:
        dedup_key = hashlib.sha256(text.encode("utf-8")).hexdigest()

    if seen_recently(dedup_key):
        print(f"⏭️ DUPLICATE webhook ignored: {dedup_key}")
        return {"ok": True, "mode": "deduped", "dedup_key": dedup_key, "tv_order_id": tv_order_id}

    if tv_order_id in TV_SKIP_ORDER_IDS:
        print(f"⏭️ SKIPPED: {tv_order_id}")
        return {"ok": True, "mode": "skipped", "tv_order_id": tv_order_id}

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

    # order_type (entry only)
    order_type = str(extra.get("order_type") or data.get("order_type") or "market").lower()
    reduce_only_bool = parse_bool(extra.get("reduce_only", data.get("reduce_only", False)))

    # TP/SL values (used only when msg_type=="tpsl")
    tp_trigger = to_decimal(extra.get("tp_trigger") or data.get("tp_trigger"))
    sl_trigger = to_decimal(extra.get("sl") or extra.get("sl_trigger") or data.get("sl") or data.get("sl_trigger"))

    # limit entry price
    limit_price = to_decimal(extra.get("price") or data.get("price"))

    print("\n=== Parsed ===")
    print(
        f"type={msg_type} coin={coin} is_buy={is_buy} sz={sz} order_type={order_type} reduce_only={reduce_only_bool} "
        f"limit_price={limit_price} tp_trigger={tp_trigger} sl_trigger={sl_trigger}"
    )

    if not HL_LIVE_TRADING:
        print("⚠️ SAFE MODE — not sent")
        return {
            "ok": True,
            "mode": "safe",
            "type": msg_type,
            "coin": coin,
            "is_buy": is_buy,
            "sz": str(sz),
            "order_type": order_type,
            "reduce_only": reduce_only_bool,
            "limit_price": str(limit_price) if limit_price else None,
            "tp_trigger": str(tp_trigger) if tp_trigger else None,
            "sl_trigger": str(sl_trigger) if sl_trigger else None,
        }

    steps = ASSET_STEPS.get(coin)
    if not steps:
        raise HTTPException(status_code=400, detail=f"No hardcoded steps for coin={coin}. Add it to ASSET_STEPS.")

    sz_step = steps["sz_step"]
    px_step = steps["px_step"]

    # round size (floor)
    sz_rounded = round_to_step_floor(sz, sz_step)
    if sz_rounded <= 0:
        raise HTTPException(status_code=400, detail=f"Qty too small after rounding to sz_step={sz_step}")
    if sz_rounded != sz:
        print(f"ℹ️ Size rounded: raw_sz={sz} sz_step={sz_step} sz_rounded={sz_rounded}")
    sz = sz_rounded

    exchange = get_exchange()

    # =====================================================================
    # TPSL PATH: TP = LIMIT reduce-only, SL = TRIGGER stop-market reduce-only
    # =====================================================================
    if msg_type == "tpsl":
        if tp_trigger is None and sl_trigger is None:
            raise HTTPException(status_code=400, detail="type=tpsl requires tp_trigger and/or sl")

        try:
            close_is_buy = not is_buy  # long->sell, short->buy
            results = []

            # --- TP as LIMIT reduce-only (GTC) ---
            if tp_trigger is not None:
                tp_rounded, tp_num = fmt_px_for_hl(Decimal(str(tp_trigger)), px_step)
                print(f"=== TP LIMIT (reduce-only) === raw={tp_trigger} rounded={tp_rounded} px_num={tp_num}")
                res_tp = hl_order_with_retry(
                    exchange,
                    coin=coin,
                    is_buy=close_is_buy,
                    sz=sz,
                    px_num=tp_num,
                    tif="Gtc",
                    reduce_only=True,
                    order_type_wire={"limit": {"tif": "Gtc"}},
                )
                results.append({"tp_limit": res_tp, "px": str(tp_rounded)})

            # --- SL as STOP-MARKET TRIGGER reduce-only ---
            if sl_trigger is not None:
                sl_rounded, sl_num = fmt_px_for_hl(Decimal(str(sl_trigger)), px_step)

                # Validate trigger side vs current mid to avoid HL rejecting it
                mids = fetch_all_mids_with_retry()
                if coin not in mids:
                    print(f"⚠️ SL skipped: coin not found in allMids: {coin}")
                else:
                    mid = Decimal(str(mids[coin]))

                    # For SELL close (long): sl must be below mid
                    # For BUY close (short): sl must be above mid
                    wrong_side = (close_is_buy is False and sl_rounded >= mid) or (close_is_buy is True and sl_rounded <= mid)
                    if wrong_side:
                        print(f"⚠️ SL skipped: trigger on wrong side. mid={mid} sl={sl_rounded} close_is_buy={close_is_buy}")
                    else:
                        print(
                            f"=== SL STOP-MARKET (reduce-only) === raw={sl_trigger} rounded={sl_rounded} "
                            f"mid={mid} triggerPx={float(sl_num)}"
                        )

                        # IMPORTANT for your SDK: triggerPx must be present in the trigger dict.
                        # Use px_num=1 (dummy) instead of 0 to avoid 'invalid price' edge cases.
                        res_sl = hl_order_with_retry(
                            exchange,
                            coin=coin,
                            is_buy=close_is_buy,
                            sz=sz,
                            px_num=1,
                            tif="Gtc",
                            reduce_only=True,
                            order_type_wire={
                                "trigger": {
                                    "isMarket": True,
                                    "triggerPx": float(sl_num),
                                    "tpsl": "sl",
                                }
                            },
                        )
                        results.append({"sl_stop_market": res_sl, "triggerPx": str(sl_rounded)})

            print("\n=== HL TPSL RESPONSE (TP limit + SL stop-market) ===")
            print(results)

            return {
                "ok": True,
                "mode": "live_tpsl_tp_limit_sl_stop_market",
                "tpsl": results,
                "tv_order_id": tv_order_id,
                "dedup_key": dedup_key,
            }

        except Exception as e:
            log_exception("❌❌❌ TPSL FAILED ❌❌❌", e)
            if is_429(e):
                raise HTTPException(status_code=503, detail="Hyperliquid rate limited (429) on tpsl.")
            raise HTTPException(status_code=500, detail=f"Hyperliquid tpsl failed: {e}")

    # =====================================================================
    # ENTRY PATH: type="order" (market IOC with slippage, or limit GTC)
    # =====================================================================
    try:
        if order_type == "limit":
            if limit_price is None:
                raise HTTPException(status_code=400, detail="limit order requires price (extra.price or price)")

            lp_rounded, lp_num = fmt_px_for_hl(Decimal(str(limit_price)), px_step)
            print(f"=== LIMIT ENTRY DEBUG === raw={limit_price} rounded={lp_rounded} px_num={lp_num}")

            main_result = hl_order_with_retry(
                exchange,
                coin=coin,
                is_buy=is_buy,
                sz=sz,
                px_num=lp_num,
                tif="Gtc",
                reduce_only=reduce_only_bool,
                order_type_wire={"limit": {"tif": "Gtc"}},
            )

        elif order_type == "market":
            mids = fetch_all_mids_with_retry()
            if coin not in mids:
                raise HTTPException(status_code=400, detail=f"Coin not found in allMids: {coin}")

            mid = Decimal(str(mids[coin]))
            px_raw = mid * (Decimal("1") + HL_SLIPPAGE) if is_buy else mid * (Decimal("1") - HL_SLIPPAGE)
            px_rounded, px_num = fmt_px_for_hl(px_raw, px_step)

            print(f"=== MARKET IOC DEBUG === mid={mid} px_raw={px_raw} rounded={px_rounded} px_num={px_num}")

            main_result = hl_order_with_retry(
                exchange,
                coin=coin,
                is_buy=is_buy,
                sz=sz,
                px_num=px_num,
                tif="Ioc",
                reduce_only=reduce_only_bool,
                order_type_wire={"limit": {"tif": "Ioc"}},
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported order_type={order_type}")

    except HTTPException:
        raise
    except Exception as e:
        log_exception("❌❌❌ HYPERLIQUID ENTRY ORDER FAILED ❌❌❌", e)
        if is_429(e):
            raise HTTPException(status_code=503, detail="Hyperliquid rate limited (429) on entry order.")
        raise HTTPException(status_code=500, detail=f"Hyperliquid entry order failed: {e}")

    print("\n=== HL ENTRY ORDER RESPONSE ===")
    print(main_result)

    return {
        "ok": True,
        "mode": "live_entry",
        "main": main_result,
        "tv_order_id": tv_order_id,
        "dedup_key": dedup_key,
    }
