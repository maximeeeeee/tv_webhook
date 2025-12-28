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
# Bitget credentials
# ===============================
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.getenv("BITGET_API_PASSPHRASE", "")
BITGET_BASE_URL = "https://api.bitget.com"

# ===============================
# SAFE MODE
# ===============================
BITGET_LIVE_TRADING = os.getenv("BITGET_LIVE_TRADING", "false").lower() == "true"

# ===============================
# Futures defaults
# ===============================
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_MODE = "crossed"
MARGIN_COIN = "USDT"

# ===============================
# Skip rules
# ===============================
TV_SKIP_ORDER_IDS = {"Exit Long", "Exit Short"}

# ===============================
# Helpers
# ===============================
def now_ms():
    return str(int(time.time() * 1000))

def compact_json(obj):
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)

def sign_bitget(ts, method, path, body):
    msg = f"{ts}{method}{path}{body}"
    mac = hmac.new(
        BITGET_API_SECRET.encode(),
        msg.encode(),
        hashlib.sha256
    ).digest()
    return base64.b64encode(mac).decode()

def normalize_symbol(symbol):
    for suffix in ["_UMCBL", "_DMCBL", "_CMCBL"]:
        if symbol.endswith(suffix):
            return symbol.replace(suffix, "")
    return symbol

def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s != "" else None

# ===============================
# Webhook
# ===============================
@app.post("/tv")
async def tv_webhook(req: Request):
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

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

    symbol = normalize_symbol(data["symbol"])
    action = data["action"].lower()
    qty = data["qty"]
    order_type = data.get("order_type", "market").lower()

    reduce_only = "YES" if data.get("reduce_only") else "NO"
    trade_side = "close" if reduce_only == "YES" else "open"

    price = clean(data.get("price"))
    tp_trigger = clean(data.get("tp_trigger"))
    tp_exec = clean(data.get("tp_exec"))
    sl = clean(data.get("sl"))

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
        body["force"] = "gtc"

    # 🔥 Trigger + Exec TP
    if tp_trigger and tp_exec:
        body["presetStopSurplusPrice"] = tp_trigger
        body["presetStopSurplusExecutePrice"] = tp_exec

    if sl:
        body["presetStopLossPrice"] = sl

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

    print("\n=== BITGET BODY ===")
    print(body_str)

    if not BITGET_LIVE_TRADING:
        print("⚠️ SAFE MODE — not sent")
        return {"ok": True, "mode": "safe", "body": body}

    r = requests.post(url, headers=headers, data=body_str, timeout=15)
    return {"ok": r.ok, "status": r.status_code, "response": r.text}

