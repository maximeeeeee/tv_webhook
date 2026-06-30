import os
import json
import traceback
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_FLOOR
from time import time, sleep
from typing import Optional, Tuple, Union

import requests
from fastapi import FastAPI, Request, HTTPException

from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

app = FastAPI()

TV_WEBHOOK_TOKEN = os.getenv("TV_WEBHOOK_TOKEN", "CHANGE_ME")

HL_ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS", "")
HL_SECRET_KEY = os.getenv("HL_SECRET_KEY", "")

HL_BASE_URL = os.getenv("HL_BASE_URL", constants.MAINNET_API_URL).rstrip("/")
HL_LIVE_TRADING = os.getenv("HL_LIVE_TRADING", "false").lower() == "true"

HL_MARKET_SLIPPAGE = float(os.getenv("HL_MARKET_SLIPPAGE", "0.02"))
HL_SLIPPAGE = Decimal(os.getenv("HL_SLIPPAGE", "0.01"))

TV_SKIP_ORDER_IDS = {"Exit Long", "Exit Short"}

ASSET_STEPS = {
    "BTC": {"sz_step": Decimal("0.00001"), "px_step": Decimal("1")},
}

_exchange: Optional[Exchange] = None

_seen = {}
DEDUP_TTL = 60


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
    px_rounded = round_to_step_nearest(px, px_step)
    if px_step == Decimal("1"):
        return px_rounded, int(px_rounded)
    return px_rounded, float(px_rounded)


def is_429(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "rate" in msg.lower()


def extract_filled_size(order_response) -> Optional[Decimal]:
    try:
        statuses = order_response.get("response", {}).get("data", {}).get("statuses", [])
        for status in statuses:
            if "filled" in status:
                return Decimal(str(status["filled"]["totalSz"]))
    except Exception:
        return None
    return None


def get_exchange(max_attempts: int = 10) -> Exchange:
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
            print(f"⚠️ Exchange init failed (attempt {attempt}/{max_attempts}): {e}. Backing off {backoff}s...")
            if attempt < max_attempts:
                sleep(backoff)
                backoff = min(backoff * 2, 10)

    raise HTTPException(status_code=503, detail=f"Failed to init Exchange after retries: {last_err}")


def fetch_all_mids_with_retry(max_attempts: int = 10):
    url = f"{HL_BASE_URL}/info"
    payload = {"type": "allMids"}

    backoff = 1
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(url, json=payload, timeout=8)
            if r.status_code == 429:
                print(f"⚠️ allMids hit 429 (attempt {attempt}/{max_attempts}). Backing off {backoff}s...")
                if attempt < max_attempts:
                    sleep(backoff)
                    backoff = min(backoff * 2, 10)
                continue

            r.raise_for_status()
            return r.json()

        except Exception as e:
            last_err = e
            print(f"⚠️ allMids fetch failed (attempt {attempt}/{max_attempts}): {e}. Backing off {backoff}s...")
            if attempt < max_attempts:
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
    tif: str,
    reduce_only: bool,
    order_type_wire: dict,
    max_attempts: int = 10,
) -> dict:
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
                if attempt < max_attempts:
                    sleep(backoff)
                    backoff = min(backoff * 2, 10)
                continue
            raise

    raise HTTPException(status_code=503, detail=f"Order failed after retries (last_err={last_err})")


def hl_market_open_with_retry(
    exchange: Exchange,
    *,
    coin: str,
    is_buy: bool,
    sz: Decimal,
    slippage: float,
    max_attempts: int = 10,
) -> dict:
    backoff = 1
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            return exchange.market_open(coin, is_buy, float(sz), None, float(slippage))
        except Exception as e:
            last_err = e
            if is_429(e):
                print(f"⚠️ market_open hit 429 (attempt {attempt}/{max_attempts}). Backing off {backoff}s...")
                if attempt < max_attempts:
                    sleep(backoff)
                    backoff = min(backoff * 2, 10)
                continue
            raise

    raise HTTPException(status_code=503, detail=f"market_open failed after retries (last_err={last_err})")


def hl_market_close_with_retry(
    exchange: Exchange,
    *,
    coin: str,
    slippage: float,
    max_attempts: int = 10,
) -> dict:
    backoff = 1
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"🚨 EMERGENCY MARKET CLOSE === coin={coin} slippage={slippage}")
            try:
                return exchange.market_close(coin, slippage=float(slippage))
            except TypeError:
                return exchange.market_close(coin)

        except Exception as e:
            last_err = e
            if is_429(e):
                print(f"⚠️ market_close hit 429 (attempt {attempt}/{max_attempts}). Backing off {backoff}s...")
                if attempt < max_attempts:
                    sleep(backoff)
                    backoff = min(backoff * 2, 10)
                continue
            raise

    raise HTTPException(status_code=503, detail=f"market_close failed after retries (last_err={last_err})")


def place_tp_sl_triggers(
    exchange: Exchange,
    *,
    coin: str,
    entry_is_buy: bool,
    sz: Decimal,
    px_step: Decimal,
    tp_trigger: Optional[Decimal],
    tp_limit: Optional[Decimal],
    sl_trigger: Optional[Decimal],
) -> list:
    results = []

    close_is_buy = not entry_is_buy

    mids = fetch_all_mids_with_retry()
    if coin not in mids:
        mid = None
        mid_rounded = None
        mid_num = None
        print(f"⚠️ TPSL: coin not found in allMids: {coin}")
    else:
        mid = Decimal(str(mids[coin]))
        mid_rounded, mid_num = fmt_px_for_hl(mid, px_step)

    if tp_trigger is not None:
        tp_limit = tp_limit if tp_limit is not None else tp_trigger

        tpTrig_rounded, tpTrig_num = fmt_px_for_hl(Decimal(str(tp_trigger)), px_step)
        tpLim_rounded, tpLim_num = fmt_px_for_hl(Decimal(str(tp_limit)), px_step)

        if mid is not None:
            print(
                f"=== TP TRIGGER TAKE-LIMIT (reduce-only) === "
                f"triggerPx={tpTrig_rounded} limitPx={tpLim_rounded} refPx={mid_rounded}"
            )
        else:
            print(
                f"=== TP TRIGGER TAKE-LIMIT (reduce-only) === "
                f"triggerPx={tpTrig_rounded} limitPx={tpLim_rounded}"
            )

        res_tp = hl_order_with_retry(
            exchange,
            coin=coin,
            is_buy=close_is_buy,
            sz=sz,
            px_num=tpLim_num,
            tif="Gtc",
            reduce_only=True,
            order_type_wire={
                "trigger": {
                    "isMarket": False,
                    "triggerPx": float(tpTrig_num),
                    "tpsl": "tp",
                }
            },
        )

        results.append(
            {
                "tp_trigger_take_limit": res_tp,
                "triggerPx": str(tpTrig_rounded),
                "limitPx": str(tpLim_rounded),
            }
        )

    if sl_trigger is not None:
        sl_rounded, sl_num = fmt_px_for_hl(Decimal(str(sl_trigger)), px_step)

        if mid is None:
            print(f"⚠️ SL skipped: coin not found in allMids: {coin}")
        else:
            print(
                f"=== SL STOP-MARKET (reduce-only) === "
                f"triggerPx={sl_rounded} refPx={mid_rounded}"
            )

            res_sl = hl_order_with_retry(
                exchange,
                coin=coin,
                is_buy=close_is_buy,
                sz=sz,
                px_num=mid_num,
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

            results.append(
                {
                    "sl_stop_market": res_sl,
                    "triggerPx": str(sl_rounded),
                    "refPx": str(mid_rounded),
                }
            )

    return results


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

    order_type = str(extra.get("order_type") or data.get("order_type") or "market").lower()
    reduce_only_bool = parse_bool(extra.get("reduce_only", data.get("reduce_only", False)))

    tp_trigger = to_decimal(extra.get("tp_trigger") or data.get("tp_trigger"))
    tp_limit = to_decimal(extra.get("tp_limit") or data.get("tp_limit"))
    sl_trigger = to_decimal(extra.get("sl") or extra.get("sl_trigger") or data.get("sl") or data.get("sl_trigger"))

    limit_price = to_decimal(extra.get("price") or data.get("price"))

    print("\n=== Parsed ===")
    print(
        f"type={msg_type} coin={coin} is_buy={is_buy} sz={sz} order_type={order_type} reduce_only={reduce_only_bool} "
        f"limit_price={limit_price} tp_trigger={tp_trigger} tp_limit={tp_limit} sl_trigger={sl_trigger}"
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
            "tp_limit": str(tp_limit) if tp_limit else None,
            "sl_trigger": str(sl_trigger) if sl_trigger else None,
        }

    steps = ASSET_STEPS.get(coin)
    if not steps:
        raise HTTPException(status_code=400, detail=f"No hardcoded steps for coin={coin}. Add it to ASSET_STEPS.")

    sz_step = steps["sz_step"]
    px_step = steps["px_step"]

    sz_rounded = round_to_step_floor(sz, sz_step)
    if sz_rounded <= 0:
        raise HTTPException(status_code=400, detail=f"Qty too small after rounding to sz_step={sz_step}")
    if sz_rounded != sz:
        print(f"ℹ️ Size rounded: raw_sz={sz} sz_step={sz_step} sz_rounded={sz_rounded}")
    sz = sz_rounded

    exchange = get_exchange()

    if msg_type == "tpsl":
        if tp_trigger is None and sl_trigger is None:
            raise HTTPException(status_code=400, detail="type=tpsl requires tp_trigger and/or sl")

        try:
            results = place_tp_sl_triggers(
                exchange,
                coin=coin,
                entry_is_buy=is_buy,
                sz=sz,
                px_step=px_step,
                tp_trigger=tp_trigger,
                tp_limit=tp_limit,
                sl_trigger=sl_trigger,
            )

            print("\n=== HL TPSL RESPONSE ===")
            print(results)

            return {
                "ok": True,
                "mode": "live_tpsl_trigger_tp_limit_sl_stop_market",
                "tpsl": results,
                "tv_order_id": tv_order_id,
                "dedup_key": dedup_key,
            }

        except Exception as e:
            log_exception("❌❌❌ TPSL FAILED ❌❌❌", e)
            if is_429(e):
                raise HTTPException(status_code=503, detail="Hyperliquid rate limited (429) on tpsl.")
            raise HTTPException(status_code=500, detail=f"Hyperliquid tpsl failed: {e}")

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
            print(f"=== MARKET OPEN (SDK) === slippage={HL_MARKET_SLIPPAGE}")

            main_result = hl_market_open_with_retry(
                exchange,
                coin=coin,
                is_buy=is_buy,
                sz=sz,
                slippage=HL_MARKET_SLIPPAGE,
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

    filled_size = extract_filled_size(main_result)

    if filled_size is None:
        print("⚠️ ENTRY NOT FILLED — no TP/SL will be placed.")
        return {
            "ok": True,
            "mode": "live_entry_not_filled",
            "main": main_result,
            "tpsl": None,
            "tv_order_id": tv_order_id,
            "dedup_key": dedup_key,
        }

    print(f"✅ ENTRY FILLED SIZE === {filled_size}")

    one_shot_results = []

    if tp_trigger is not None or sl_trigger is not None:
        try:
            one_shot_results = place_tp_sl_triggers(
                exchange,
                coin=coin,
                entry_is_buy=is_buy,
                sz=filled_size,
                px_step=px_step,
                tp_trigger=tp_trigger,
                tp_limit=tp_limit,
                sl_trigger=sl_trigger,
            )

            print("=== HL ONE-SHOT TPSL RESPONSE ===")
            print(one_shot_results)

        except Exception as e:
            log_exception("❌❌❌ ONE-SHOT TPSL FAILED — EMERGENCY CLOSE REQUIRED ❌❌❌", e)

            emergency_close_result = None

            try:
                emergency_close_result = hl_market_close_with_retry(
                    exchange,
                    coin=coin,
                    slippage=HL_MARKET_SLIPPAGE,
                )

                print("🚨🚨🚨 EMERGENCY MARKET CLOSE RESPONSE 🚨🚨🚨")
                print(emergency_close_result)

            except Exception as close_e:
                log_exception("🚨🚨🚨 EMERGENCY MARKET CLOSE FAILED 🚨🚨🚨", close_e)
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": f"TP/SL failed after filled entry: {e}",
                        "emergency_close_error": str(close_e),
                        "main": main_result,
                    },
                )

            raise HTTPException(
                status_code=500,
                detail={
                    "error": f"TP/SL failed after filled entry: {e}",
                    "emergency_close": emergency_close_result,
                    "main": main_result,
                },
            )

    return {
        "ok": True,
        "mode": "live_entry" if not one_shot_results else "live_entry_one_shot_tpsl",
        "main": main_result,
        "filled_size": str(filled_size),
        "tpsl": one_shot_results if one_shot_results else None,
        "tv_order_id": tv_order_id,
        "dedup_key": dedup_key,
    }