import asyncio
import logging
import os
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Helper: get env with type casting
def get_env(key: str, default=None, cast_type=str):
    value = os.getenv(key, default)
    if value is None:
        return default
    if cast_type == bool:
        return value.lower() in ('true', '1', 'yes', 'on')
    elif cast_type == int:
        return int(value)
    elif cast_type == float:
        return float(value)
    return value

# Configuration
config = {
    "wallet": get_env("ZPOOL_WALLET", ""),
    "refresh_interval": get_env("REFRESH_INTERVAL", 60, int),
    "alerts": {
        "worker_offline": get_env("ALERT_WORKER_OFFLINE", True, bool),
        "min_balance": get_env("ALERT_MIN_BALANCE", 0.01, float),
        "hashrate_drop_percent": get_env("ALERT_HASHRATE_DROP_PERCENT", 50, float),
    },
    "notifications": {
        "discord": {
            "enabled": get_env("DISCORD_ENABLED", False, bool),
            "webhook_url": get_env("DISCORD_WEBHOOK_URL", ""),
        },
        "ntfy": {
            "enabled": get_env("NTFY_ENABLED", False, bool),
            "topic": get_env("NTFY_TOPIC", ""),
            "server": get_env("NTFY_SERVER", "https://ntfy.sh"),
        }
    }
}

if not config["wallet"]:
    logger.error("❌ ZPOOL_WALLET is not set!")
    raise ValueError("ZPOOL_WALLET environment variable is required")

logger.info(f"✅ Configuration loaded:")
logger.info(f"   Wallet: {config['wallet'][:10]}...")
logger.info(f"   Refresh: {config['refresh_interval']}s")
logger.info(f"   Discord: {'enabled' if config['notifications']['discord']['enabled'] else 'disabled'}")
logger.info(f"   ntfy: {'enabled' if config['notifications']['ntfy']['enabled'] else 'disabled'}")

app = FastAPI(title="zpool Monitor", version="1.0.0")
templates = Jinja2Templates(directory="templates")

# State
state = {
    "stats": None,
    "workers": {},
    "payments": [],
    "blocks": [],
    "max_hashrate": 0,
    "last_alert_sent": {},
    "history": [],
    "start_time": datetime.utcnow()
}

ZPOOL_API = "https://zpool.ca/api"


async def fetch_zpool_stats() -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ZPOOL_API}/public", params={"address": config["wallet"]})
        r.raise_for_status()
        return r.json()


async def fetch_worker_details() -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ZPOOL_API}/user", params={"address": config["wallet"]})
        r.raise_for_status()
        return r.json()


async def fetch_payment_history() -> list:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ZPOOL_API}/wallet", params={"address": config["wallet"]})
        r.raise_for_status()
        data = r.json()
        return data.get("payments", [])[:10]


async def fetch_blocks_found() -> list:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ZPOOL_API}/blocks", params={"address": config["wallet"]})
        r.raise_for_status()
        data = r.json()
        # API อาจ return dict หรือ list แล้วแต่ endpoint
        if isinstance(data, dict):
            return data.get("blocks", [])[:20]
        return data[:20] if isinstance(data, list) else []


async def send_discord(title: str, message: str, color: int = 15158332):
    if not config["notifications"]["discord"]["enabled"]:
        return
    webhook_url = config["notifications"]["discord"]["webhook_url"]
    if not webhook_url:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "zpool Monitor"}
        }]
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(webhook_url, json=payload, timeout=10)
        logger.info(f"Discord sent: {title}")
    except Exception as e:
        logger.error(f"Discord error: {e}")


async def send_ntfy(title: str, message: str, priority: int = 3):
    if not config["notifications"]["ntfy"]["enabled"]:
        return
    server = config["notifications"]["ntfy"]["server"]
    topic = config["notifications"]["ntfy"]["topic"]
    if not topic:
        return
    headers = {
        "Title": title,
        "Priority": str(priority),
        "Tags": "warning" if priority >= 4 else "info"
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{server}/{topic}", data=message.encode('utf-8'), headers=headers, timeout=10)
        logger.info(f"ntfy sent: {title}")
    except Exception as e:
        logger.error(f"ntfy error: {e}")


async def send_alert(title: str, message: str, alert_type: str = "warning"):
    now = datetime.utcnow().timestamp()
    last = state["last_alert_sent"].get(alert_type, 0)
    if now - last < 300:  # anti-spam 5 นาที
        return
    state["last_alert_sent"][alert_type] = now
    
    color = 15158332 if alert_type == "warning" else 3066993
    await asyncio.gather(
        send_discord(title, message, color),
        send_ntfy(title, message, priority=4 if alert_type == "warning" else 3)
    )


async def check_alerts(stats: dict):
    alerts_cfg = config["alerts"]
    currency = stats.get("currency", "BTC")
    
    if alerts_cfg["worker_offline"] and stats.get("worker", 0) == 0:
        await send_alert(
            "⚠️ No Workers Online",
            f"ไม่มี worker ทำงานอยู่!\nWallet: {config['wallet'][:10]}...",
            "warning"
        )
    
    balance = stats.get("balance", 0)
    if balance >= alerts_cfg["min_balance"]:
        await send_alert(
            "🎉 Balance Goal Reached",
            f"Balance: {balance:.8f} {currency}\nเป้าหมาย: {alerts_cfg['min_balance']} {currency}",
            "info"
        )
    
    hashrate = stats.get("hashrate", 0)
    if state["max_hashrate"] > 0:
        drop_percent = ((state["max_hashrate"] - hashrate) / state["max_hashrate"]) * 100
        if drop_percent >= alerts_cfg["hashrate_drop_percent"]:
            await send_alert(
                "📉 Hashrate Dropped",
                f"Hashrate ลดลง {drop_percent:.1f}%\n"
                f"ปัจจุบัน: {hashrate:,} H/s\n"
                f"สูงสุด: {state['max_hashrate']:,} H/s",
                "warning"
            )
    
    if hashrate > state["max_hashrate"]:
        state["max_hashrate"] = hashrate


async def background_poller():
    await asyncio.sleep(2)  # wait for startup
    while True:
        try:
            stats = await fetch_zpool_stats()
            state["stats"] = stats
            
            try:
                state["workers"] = await fetch_worker_details()
            except Exception as e:
                logger.error(f"Workers error: {e}")
            
            try:
                state["payments"] = await fetch_payment_history()
            except Exception as e:
                logger.error(f"Payments error: {e}")
            
            try:
                state["blocks"] = await fetch_blocks_found()
            except Exception as e:
                logger.error(f"Blocks error: {e}")
            
            state["history"].append({
                "time": datetime.utcnow().isoformat(),
                "hashrate": stats.get("hashrate", 0),
                "balance": stats.get("balance", 0),
                "workers": stats.get("worker", 0)
            })
            state["history"] = state["history"][-100:]
            
            await check_alerts(stats)
            logger.info(f"Updated: balance={stats.get('balance')}, workers={stats.get('worker')}")
            
        except Exception as e:
            logger.error(f"Polling error: {e}")
        
        await asyncio.sleep(config["refresh_interval"])


@app.on_event("startup")
async def startup():
    logger.info("🚀 Starting zpool Monitor...")
    asyncio.create_task(background_poller())


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "healthy", "uptime": (datetime.utcnow() - state["start_time"]).seconds}


@app.get("/api/stats")
async def api_stats():
    if state["stats"] is None:
        return {"error": "No data yet"}
    return {
        **state["stats"],
        "max_hashrate": state["max_hashrate"],
        "last_update": datetime.utcnow().isoformat()
    }


@app.get("/api/workers")
async def api_workers():
    return state.get("workers", {})


@app.get("/api/payments")
async def api_payments():
    return state.get("payments", [])


@app.get("/api/blocks")
async def api_blocks():
    return state.get("blocks", [])


@app.get("/api/history")
async def api_history():
    return state["history"]


@app.get("/api/config")
async def api_config():
    return {
        "wallet": config["wallet"][:10] + "...",
        "refresh_interval": config["refresh_interval"],
        "alerts": config["alerts"],
        "notifications": {
            "discord": {"enabled": config["notifications"]["discord"]["enabled"]},
            "ntfy": {"enabled": config["notifications"]["ntfy"]["enabled"]}
        }
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)