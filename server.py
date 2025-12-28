import os
import json
import time
import hmac
import base64
import hashlib
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# ===============================
# TradingView security
# ===============================
TV_WEBHOOK_TOKEN = os.getenv("TV_WEBHOOK_TOKEN", "CHANGE_ME")

# ===============================
# Bitget credentials (env vars)
# ===============================
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.getenv("BITGET_API_PASSPHRASE", "")
BITGET_BASE_URL = "https://api.bitget.com"

# ===============================
# LIVE SWITCH
# ===============================
BITGET_LIVE_TRADING = os.getenv("BITGET_LIVE_TRADING", "false").lower() == "true"

# ===============================
# Futures defaults (USDT Perps)
# ===============================
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_MODE = "crossed"
MARGIN_COIN = "USDT"

# ===============================
# TV rules
# ===============================
TV_SKIP_ORDER_IDS = {"Exit Long", "Exit Short"}
TV_EXECUTE_COMMENTS = {"NY Session Close", "Daily Close"}

# ===============================
# Helpers
# ===============================
def now_ms() -> str:
    return str(int(time.time() * 1000))


def compact_json(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def sign_bitget(ts: str, method: str, path: str, body: str) -> str:
    msg = f"{ts}{method}{path}{body}"
    mac = hmac.new(
        BITGET_API_SECRET.encode(),
        msg.encode(),
        hashlib.sha256
    ).digest()
    return base64.b64encode(mac).decode()


def normalize_symbol(symbol: str) -> str:
    for suffix in ["_UMCBL", "_DMCBL", "_CMCBL", "_SUMCBL", "_SDMCBL", "_SCMCBL"]:
        if symbol.endswith(suffix):
            return symbol.replace(suffix, "")
    return symbol


def as_str_or_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s != "" else None


# ===============================
# Webhook
# ===============================
@app.post("/tv")
async def tv_webhook(req: Request):
    data = await req.json()

    # ---- Security
    if data.get("token") != TV_WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")

    print("\n=== TradingView payload received ===")
    print(data)

    if data.get("type") != "order":
        return {"ok": True, "mode": "ignored"}

    # ---- Required fields
    required = ["symbol", "action", "qty"]
    missing = [k for k in required if k not in data]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {missing}")

    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        raise HTTPException(status_code=500, detail="Missing Bitget API credentials")

    # ---- Parse basic fields
    symbol = normalize_symbol(str(data["symbol"]))
    action = str(data["action"]).lower()   # buy / sell
    qty = str(data["qty"])
    order_type = str(data.get("order_type", "market")).lower()
    reduce_only_bool = bool(data.get("reduce_only", False))

    is_long = action == "buy"

    # ---- TV metadata
    tv_order_id = str(data.get("tv_order_id", "")).strip()
    tv_comment = str(data.get("tv_comment", "")).strip()

    # ===============================
    # ROUTING LOGIC
    # ===============================
    if tv_order_id in TV_SKIP_ORDER_IDS:
        print(f"⏭️ SKIP: {tv_order_id}")
        return {"ok": True, "mode": "skipped"}

    if tv_comment in TV_EXECUTE_COMMENTS:
        print(f"✅ EXECUTE: {tv_comment}")

    # ---- Hedge mode
    trade_side = "close" if reduce_only_bool else "open"
    reduce_only = "YES" if reduce_only_bool else "NO"

    # ---- LIMIT fields
    price = as_str_or_none(data.get("price"))
    force = str(data.get("force", "gtc")).lower()

    if order_type == "limit":
        if not price:
            raise HTTPException(status_code=400, detail="Missing price for limit order")
    else:
        price = None

    # ===============================
    # TP / SL ROUTING (LONG vs SHORT)
    # ===============================
    if is_long:
        tp_trigger = as_str_or_none(data.get("tp_long"))
        tp_exec    = as_str_or_none(data.get("tp_exec_long"))
        sl_trigger = as_str_or_none(data.get("sl_long"))
    else:
        tp_trigger = as_str_or_none(data.get("tp_short"))
        tp_exec    = as_str_or_none(data.get("tp_exec_short"))
        sl_trigger = as_str_or_none(data.get("sl_short"))

    # ===============================
    # Build Bitget order
    # ===============================
    path = "/api/v2/mix/order/place-order"
    url = BITGET_BASE_URL + path

    body = {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "marginMode": MARGIN_MODE,
        "marginCoin": MARGIN_COIN,
        "size": qty,
        "side": action,
        "tradeSide": trade_side,
        "orderType": order_type,
        "reduceOnly": reduce_only,
    }

    if order_type == "limit":
        body["price"] = price
        body["force"] = force

    # ---- TP / SL (trigger)
    if tp_trigger:
        body["presetStopSurplusPrice"] = tp_trigger
    if sl_trigger:
        body["presetStopLossPrice"] = sl_trigger

    # ---- TP execution as LIMIT
    if tp_exec:
        body["presetStopSurplusExecutePrice"] = tp_exec

    # ===============================
    # Send request
    # ===============================
    body_str = compact_json(body)
    ts = now_ms()
    sign = sign_bitget(ts, "POST", path, body_str)

    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

    print("\n=== BITGET REQUEST ===")
    print(body_str)

    if not BITGET_LIVE_TRADING:
        print("⚠️ SAFE MODE — order not sent")
        return {"ok": True, "mode": "safe", "would_send": body}

    try:
        r = requests.post(url, headers=headers, data=body_str, timeout=15)
        print("\n=== BITGET RESPONSE ===")
        print(r.status_code, r.text)
        return {"ok": r.ok, "status": r.status_code, "response": r.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
