import os
import json
import traceback
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_FLOOR
from time import time, sleep
from typing import Optional, Tuple, Union, List, Dict, Any

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
# Your confirmed BTC rules:
# - Minimal size increment: 0.00001 BTC
# - Price increment: 1 (no decimals)
ASSET_STEPS = {
    "BTC": {"sz_step": Decimal("0.00001"), "px_step": Decimal("1")},
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


def hl_grouped_orders_with_retry(
    exchange: Exchange,
    *,
    order_requests: List[Dict[str, Any]],
    grouping: str,
    max_attempts: int = 5,
) -> dict:
    """
    Sends ALL orders in one exchange request via bulk_orders(..., grouping=...).

    grouping examples:
      - "normalTpsl"    entry + TP/SL bracket
      - "positionTpsl"  TP/SL attached to current position
      - "na"            no grouping
    """
    backoff = 1
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            return exchange.bulk_orders(order_requests, grouping=grouping)
        except TypeError as e:
            last_err = e
            print("⚠️ Your installed hyperliquid SDK bulk_orders() does not accept grouping=. Upgrade the SDK.")
            raise
        except Exception as e:
            last_err = e
            if is_429(e):
                print(f"⚠️ bulk_orders hit 429 (attempt {attempt}/{max_attempts}). Backing off {backoff}s...")
                sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            raise

    raise HTTPException(status_code=503, detail=f"bulk_orders failed after retries (last_err={last_err})")


def hl_order_with_retry(
    exchange: Exchange,
    *,
    coin: str,
    is_buy: bool,
    sz: Decimal,
    px_num: Union[int, float],
    tif: str,
    reduce_only: bool,
    max_attempts: int = 5,
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
                {"limit": {"tif": tif}},
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


def parse_entry_state(main_result: dict) -> Tuple[bool, bool]:
    try:
        statuses = main_result.get("response", {}).get("data", {}).get("statuses", [])
        is_resting = any(isinstance(s, dict) and "resting" in s for s in statuses)
        is_filled = any(isinstance(s, dict) and "filled" in s for s in statuses)
        return is_resting, is_filled
    except Exception:
        return False, False


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
    if tv_order_id and tv_order_id not in ("Long", "Short") and len(tv_order_id) > 6:
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

    # TP/SL + entry limit price
    tp_trigger = to_decimal(extra.get("tp_trigger") or data.get("tp_trigger"))
    sl_trigger = to_decimal(extra.get("sl") or data.get("sl"))
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
    # TPSL-ONLY PATH (FIXED): type="tpsl"
    #  - NO ENTRY order
    #  - Send TP/SL as position TP/SL: grouping="positionTpsl"
    #  - IMPORTANT FIX: send limit_px and triggerPx as STRINGS ("0", "67716", etc.)
    # =====================================================================
    if msg_type == "tpsl":
        if tp_trigger is None and sl_trigger is None:
            raise HTTPException(status_code=400, detail="type=tpsl requires tp_trigger and/or sl")

        try:
            orders: List[Dict[str, Any]] = []
            # For a long position: TP/SL are sells. For a short position: TP/SL are buys.
            tpsl_is_buy = not is_buy  # close direction

            if tp_trigger is not None:
                tp_dec = Decimal(str(tp_trigger))
                tp_rounded, tp_num = fmt_px_for_hl(tp_dec, px_step)
                tp_px_str = str(int(tp_rounded)) if px_step == Decimal("1") else str(tp_rounded)
                print(
                    f"=== TP DEBUG (tpsl-only) === coin={coin} tp_raw={tp_trigger} px_step={px_step} "
                    f"tp_rounded={tp_rounded} tp_num={tp_num} tp_str={tp_px_str}"
                )
                orders.append({
                    "coin": coin,
                    "is_buy": tpsl_is_buy,
                    "sz": float(sz),
                    "limit_px": "0",  # STRING (important)
                    "order_type": {"trigger": {"isMarket": True, "triggerPx": tp_px_str, "tpsl": "tp"}},
                    "reduce_only": True,  # safety
                })

            if sl_trigger is not None:
                sl_dec = Decimal(str(sl_trigger))
                sl_rounded, sl_num = fmt_px_for_hl(sl_dec, px_step)
                sl_px_str = str(int(sl_rounded)) if px_step == Decimal("1") else str(sl_rounded)
                print(
                    f"=== SL DEBUG (tpsl-only) === coin={coin} sl_raw={sl_trigger} px_step={px_step} "
                    f"sl_rounded={sl_rounded} sl_num={sl_num} sl_str={sl_px_str}"
                )
                orders.append({
                    "coin": coin,
                    "is_buy": tpsl_is_buy,
                    "sz": float(sz),
                    "limit_px": "0",  # STRING (important)
                    "order_type": {"trigger": {"isMarket": True, "triggerPx": sl_px_str, "tpsl": "sl"}},
                    "reduce_only": True,  # safety
                })

            print("\n=== POSITION TPSL DEBUG ===")
            print(f"grouping=positionTpsl orders={len(orders)}")

            res = hl_grouped_orders_with_retry(
                exchange,
                order_requests=orders,
                grouping="positionTpsl",
            )

            print("\n=== HL POSITION TPSL RESPONSE ===")
            print(res)

            return {
                "ok": True,
                "mode": "live_position_tpsl",
                "grouping": "positionTpsl",
                "result": res,
                "tv_order_id": tv_order_id,
                "dedup_key": dedup_key,
            }

        except HTTPException:
            raise
        except TypeError as e:
            log_exception("⚠️ POSITION TPSL GROUPING NOT SUPPORTED BY INSTALLED SDK ⚠️", e)
            raise HTTPException(
                status_code=500,
                detail="Your installed hyperliquid SDK does not support bulk_orders(grouping=...). Upgrade it.",
            )
        except Exception as e:
            log_exception("❌❌❌ HYPERLIQUID POSITION TPSL FAILED ❌❌❌", e)
            if is_429(e):
                raise HTTPException(status_code=503, detail="Hyperliquid rate limited (429) on position TP/SL.")
            raise HTTPException(status_code=500, detail=f"Hyperliquid position TP/SL failed: {e}")

    # ---------------------------------------------------------------------
    # ORDER PATH: type="order"
    #  - If TP/SL provided -> try grouped normalTpsl bracket
    #  - Else -> entry only
    # ---------------------------------------------------------------------
    has_tpsl = (tp_trigger is not None) or (sl_trigger is not None)

    if has_tpsl:
        try:
            orders: List[Dict[str, Any]] = []

            # ENTRY order request
            if order_type == "limit":
                if limit_price is None:
                    raise HTTPException(status_code=400, detail="limit order requires price (extra.price or price)")
                lp_dec = Decimal(str(limit_price))
                lp_rounded, lp_num = fmt_px_for_hl(lp_dec, px_step)
                print(
                    f"=== LIMIT DEBUG === coin={coin} limit_raw={limit_price} px_step={px_step} "
                    f"limit_rounded={lp_rounded} limit_num={lp_num}"
                )

                orders.append({
                    "coin": coin,
                    "is_buy": is_buy,
                    "sz": float(sz),
                    "limit_px": lp_num,
                    "order_type": {"limit": {"tif": "Gtc"}},
                    "reduce_only": reduce_only_bool,
                })

            elif order_type == "market":
                mids = fetch_all_mids_with_retry()
                if coin not in mids:
                    raise HTTPException(status_code=400, detail=f"Coin not found in allMids: {coin}")
                mid = Decimal(str(mids[coin]))
                px_raw = mid * (Decimal("1") + HL_SLIPPAGE) if is_buy else mid * (Decimal("1") - HL_SLIPPAGE)
                px_rounded, px_num = fmt_px_for_hl(px_raw, px_step)

                print(
                    f"\n=== PRICE DEBUG === coin={coin} mid={mid} px_raw={px_raw} px_step={px_step} "
                    f"px_rounded={px_rounded} px_num={px_num}"
                )

                orders.append({
                    "coin": coin,
                    "is_buy": is_buy,
                    "sz": float(sz),
                    "limit_px": px_num,  # IOC-style market with slippage
                    "order_type": {"limit": {"tif": "Ioc"}},
                    "reduce_only": reduce_only_bool,
                })

            else:
                raise HTTPException(status_code=400, detail=f"Unsupported order_type={order_type}")

            # TP/SL (grouped)
            tpsl_is_buy = not is_buy  # close direction

            if tp_trigger is not None:
                tp_dec = Decimal(str(tp_trigger))
                tp_rounded, tp_num = fmt_px_for_hl(tp_dec, px_step)
                tp_px_str = str(int(tp_rounded)) if px_step == Decimal("1") else str(tp_rounded)
                print(
                    f"=== TP DEBUG === coin={coin} tp_raw={tp_trigger} px_step={px_step} "
                    f"tp_rounded={tp_rounded} tp_num={tp_num} tp_str={tp_px_str}"
                )
                orders.append({
                    "coin": coin,
                    "is_buy": tpsl_is_buy,
                    "sz": float(sz),
                    "limit_px": "0",  # STRING is safer for triggers
                    "order_type": {"trigger": {"isMarket": True, "triggerPx": tp_px_str, "tpsl": "tp"}},
                    "reduce_only": False,  # grouped "normalTpsl"
                })

            if sl_trigger is not None:
                sl_dec = Decimal(str(sl_trigger))
                sl_rounded, sl_num = fmt_px_for_hl(sl_dec, px_step)
                sl_px_str = str(int(sl_rounded)) if px_step == Decimal("1") else str(sl_rounded)
                print(
                    f"=== SL DEBUG === coin={coin} sl_raw={sl_trigger} px_step={px_step} "
                    f"sl_rounded={sl_rounded} sl_num={sl_num} sl_str={sl_px_str}"
                )
                orders.append({
                    "coin": coin,
                    "is_buy": tpsl_is_buy,
                    "sz": float(sz),
                    "limit_px": "0",  # STRING is safer for triggers
                    "order_type": {"trigger": {"isMarket": True, "triggerPx": sl_px_str, "tpsl": "sl"}},
                    "reduce_only": False,  # grouped "normalTpsl"
                })

            print("\n=== GROUPED ORDER DEBUG ===")
            print(f"grouping=normalTpsl orders={len(orders)}")

            grouped_res = hl_grouped_orders_with_retry(
                exchange,
                order_requests=orders,
                grouping="normalTpsl",
            )

            print("\n=== HL GROUPED RESPONSE ===")
            print(grouped_res)

            return {
                "ok": True,
                "mode": "live_grouped",
                "grouping": "normalTpsl",
                "result": grouped_res,
                "tv_order_id": tv_order_id,
                "dedup_key": dedup_key,
            }

        except HTTPException:
            raise
        except TypeError as e:
            log_exception("⚠️ GROUPED ORDERS NOT SUPPORTED BY INSTALLED SDK ⚠️", e)
            raise HTTPException(
                status_code=500,
                detail="Your installed hyperliquid SDK does not support bulk_orders(grouping=...). Upgrade it.",
            )
        except Exception as e:
            log_exception("❌❌❌ HYPERLIQUID GROUPED ORDER FAILED ❌❌❌", e)
            if is_429(e):
                raise HTTPException(status_code=503, detail="Hyperliquid rate limited (429) on grouped orders.")
            raise HTTPException(status_code=500, detail=f"Hyperliquid grouped order failed: {e}")

    # ---------------------------------------------------------------------
    # ENTRY-ONLY PATH: no TP/SL provided
    # ---------------------------------------------------------------------
    try:
        if order_type == "limit":
            if limit_price is None:
                raise HTTPException(status_code=400, detail="limit order requires price (extra.price or price)")

            lp_dec = Decimal(str(limit_price))
            lp_rounded, lp_num = fmt_px_for_hl(lp_dec, px_step)
            print(
                f"=== LIMIT DEBUG === coin={coin} limit_raw={limit_price} px_step={px_step} "
                f"limit_rounded={lp_rounded} limit_num={lp_num}"
            )

            main_result = hl_order_with_retry(
                exchange,
                coin=coin,
                is_buy=is_buy,
                sz=sz,
                px_num=lp_num,
                tif="Gtc",
                reduce_only=reduce_only_bool,
            )

        elif order_type == "market":
            mids = fetch_all_mids_with_retry()
            if coin not in mids:
                raise HTTPException(status_code=400, detail=f"Coin not found in allMids: {coin}")

            mid = Decimal(str(mids[coin]))
            px_raw = mid * (Decimal("1") + HL_SLIPPAGE) if is_buy else mid * (Decimal("1") - HL_SLIPPAGE)
            px_rounded, px_num = fmt_px_for_hl(px_raw, px_step)

            print(
                f"\n=== PRICE DEBUG === coin={coin} mid={mid} px_raw={px_raw} px_step={px_step} "
                f"px_rounded={px_rounded} px_num={px_num}"
            )

            main_result = hl_order_with_retry(
                exchange,
                coin=coin,
                is_buy=is_buy,
                sz=sz,
                px_num=px_num,
                tif="Ioc",
                reduce_only=reduce_only_bool,
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

    print("\n=== HL MAIN ORDER RESPONSE ===")
    print(main_result)

    return {
        "ok": True,
        "mode": "live",
        "main": main_result,
        "tv_order_id": tv_order_id,
        "dedup_key": dedup_key,
    }
