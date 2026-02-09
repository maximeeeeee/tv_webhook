import os
import json
import traceback
from decimal import Decimal, InvalidOperation
from time import time, sleep

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
# HARD-CODED STEPS (avoid meta/info calls for steps)
# ===============================
ASSET_STEPS = {
    "BTC": {"sz_step": Decimal("0.00001"), "px_step": Decimal("1")},
}

# ===============================
# Lazy init Exchange (but Exchange() may still call Info() internally)
# We add retry/backoff to survive 429 during init.
# ===============================
_exchange = None

# ===============================
# Simple idempotency (prevents TV retry duplicates)
# ===============================
_seen = {}  # tv_order_id -> ts
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


def round_to_step(x: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return x
    n = (x / step).to_integral_value(rounding="ROUND_FLOOR")
    return n * step


def get_exchange_with_retry(max_attempts: int = 6) -> Exchange:
    """
    Build Exchange with retry/backoff because the SDK may call Info()/spot_meta() on init,
    which can return 429 (rate limit), especially from Render IP ranges.
    """
    global _exchange

    if _exchange is not None:
        return _exchange

    if not HL_ACCOUNT_ADDRESS or not HL_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Missing HL_ACCOUNT_ADDRESS or HL_SECRET_KEY in env vars",
        )

    wallet = Account.from_key(HL_SECRET_KEY)

    backoff = 1
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            # Support multiple SDK signatures
            try:
                ex = Exchange(wallet=wallet, base_url=HL_BASE_URL, account_address=HL_ACCOUNT_ADDRESS)
            except TypeError:
                try:
                    ex = Exchange(wallet, HL_BASE_URL, HL_ACCOUNT_ADDRESS)
                except TypeError:
                    ex = Exchange(HL_ACCOUNT_ADDRESS, HL_SECRET_KEY, base_url=HL_BASE_URL)

            _exchange = ex
            print("✅ Exchange initialized successfully")
            return _exchange

        except Exception as e:
            last_err = e
            msg = str(e)

            # detect rate limiting
            if "429" in msg:
                print(f"⚠️ Exchange init hit 429 (attempt {attempt}/{max_attempts}). Backing off {backoff}s...")
                sleep(backoff)
                backoff = min(backoff * 2, 12)
                continue

            # not a 429 -> fail fast
            log_exception("❌ Exchange init failed (non-429) ❌", e)
            raise HTTPException(status_code=500, detail=f"Exchange init failed: {e}")

    raise HTTPException(status_code=503, detail=f"Exchange init failed after retries (last_err={last_err})")


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
    if seen_recently(tv_order_id):
        print(f"⏭️ DUPLICATE webhook ignored: {tv_order_id}")
        return {"ok": True, "mode": "deduped", "tv_order_id": tv_order_id}

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

    order_type = str(data.get("order_type", "market")).lower()
    reduce_only_bool = parse_bool(data.get("reduce_only", False))

    print("\n=== Parsed ===")
    print(f"coin={coin} is_buy={is_buy} sz={sz} order_type={order_type} reduce_only={reduce_only_bool}")

    if not HL_LIVE_TRADING:
        print("⚠️ SAFE MODE — not sent")
        return {"ok": True, "mode": "safe", "coin": coin, "is_buy": is_buy, "sz": str(sz), "order_type": order_type}

    steps = ASSET_STEPS.get(coin)
    if not steps:
        raise HTTPException(
            status_code=400,
            detail=f"No hardcoded steps for coin={coin}. Add it to ASSET_STEPS.",
        )

    sz_step = steps["sz_step"]
    px_step = steps["px_step"]

    # round size to lot step
    sz_rounded = round_to_step(sz, sz_step)
    if sz_rounded <= 0:
        raise HTTPException(status_code=400, detail=f"Qty too small after rounding to sz_step={sz_step}")
    if sz_rounded != sz:
        print(f"ℹ️ Size rounded: raw_sz={sz} sz_step={sz_step} sz_rounded={sz_rounded}")
    sz = sz_rounded

    if order_type != "market":
        raise HTTPException(status_code=400, detail="This lightweight version supports only market orders for now.")

    # Build Exchange with retry/backoff (prevents random 500 on 429 during init)
    exchange = get_exchange_with_retry()

    # Get mid price (one /info call) then create IOC limit px with slippage
    mids = fetch_all_mids_with_retry()
    if coin not in mids:
        raise HTTPException(status_code=400, detail=f"Coin not found in allMids: {coin}")

    mid = Decimal(str(mids[coin]))

    px_raw = mid * (Decimal("1") + HL_SLIPPAGE) if is_buy else mid * (Decimal("1") - HL_SLIPPAGE)
    px = round_to_step(px_raw, px_step)

    print(f"\n=== PRICE DEBUG === coin={coin} mid={mid} px_raw={px_raw} px_step={px_step} px_rounded={px}")

    try:
        main_result = exchange.order(
            coin,
            is_buy,
            float(sz),
            float(px),
            {"limit": {"tif": "Ioc"}},  # order_type positional arg
            reduce_only=reduce_only_bool,
        )
    except Exception as e:
        msg = str(e)
        log_exception("❌❌❌ HYPERLIQUID LIVE ORDER FAILED ❌❌❌", e)
        if "429" in msg:
            raise HTTPException(status_code=503, detail="Hyperliquid rate limited (429). TradingView will retry.")
        raise HTTPException(status_code=500, detail=f"Hyperliquid order failed: {e}")

    print("\n=== HL MAIN ORDER RESPONSE ===")
    print(main_result)

    return {"ok": True, "mode": "live", "main": main_result, "tv_order_id": tv_order_id}
