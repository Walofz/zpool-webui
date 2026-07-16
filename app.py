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

# Helper: แปลงค่าเป็น float แบบปลอดภัย
def safe_float(val, default=0.0):
    try:
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            return float(val)
        if isinstance(val, dict):
            return sum(safe_float(v) for v in val.values())
        if isinstance(val, list):
            return sum(safe_float(v) for v in val)
        return default
    except (ValueError, TypeError):
        return default

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
        "hashrate_drop_percent": get_env("HASHRATE_DROP_PERCENT", 50, float),
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
            "user": get_env("NTFY_USER", ""),
            "pass": get_env("NTFY_PASS", ""),
        }
    }
}

if not config["wallet"]:
    logger.error("ZPOOL_WALLET is not set!")
    raise ValueError("ZPOOL_WALLET environment variable is required")

logger.info("Configuration loaded:")
logger.info(f"   Wallet: {config['wallet'][:10]}...")
logger.info(f"   Refresh: {config['refresh_interval']}s")
logger.info(f"   Discord: {'enabled' if config['notifications']['discord']['enabled'] else 'disabled'}")
logger.info(f"   ntfy: {'enabled' if config['notifications']['ntfy']['enabled'] else 'disabled'}")

app = FastAPI(title="zpool Monitor", version="2.0.1")
templates = Jinja2Templates(directory="templates")

# State
state = {
    "stats": None,
    "workers": {},
    "payments": [],
    "max_hashrate": 0,
    "last_alert_sent": {},
    "history": [],
    "start_time": datetime.now()
}

ZPOOL_API = "https://www.zpool.ca/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
}


async def fetch_zpool_stats() -> dict:
    async with httpx.AsyncClient(timeout=10, headers=HEADERS) as client:
        url = f"{ZPOOL_API}/walletEX"
        params = {"address": config["wallet"]}
        r = await client.get(url, params=params)
        
        content_type = r.headers.get("content-type", "")
        if "application/json" not in content_type:
            logger.error(f"API did not return JSON! Content-Type: {content_type}")
            raise ValueError(f"Expected JSON, got {content_type}")
            
        r.raise_for_status()
        data = r.json()
        
        real_hashrate = safe_float(data.get("total_hashrates", 0))
        real_total_paid = safe_float(data.get("paidtotal", 0))
        
        miners = data.get("miners", [])
        
        total_spm = 0.0
        workers_dict = {}
        
        if isinstance(miners, list):
            for m in miners:
                algo = m.get("algo", "unknown").upper()
                if algo not in workers_dict:
                    workers_dict[algo] = []
                
                spm = safe_float(m.get("spm", 0))
                total_spm += spm
                
                workers_dict[algo].append({
                    "worker": m.get("ID", "Unknown"),
                    "spm": spm,
                    "accepted": safe_float(m.get("accepted", 0)),
                    "alive": spm > 0
                })
        
        currency = (data.get("currency") or "BTC").upper()
        payouts = data.get("payouts", [])
        worker_count = sum(len(v) for v in workers_dict.values())
        
        return {
            "currency": currency,
            "address": config["wallet"],
            "unsold": safe_float(data.get("unsold", 0)),
            "balance": safe_float(data.get("balance", 0)),
            "unpaid": safe_float(data.get("unpaid", 0)),
            "paid24h": safe_float(data.get("paid24h", 0)),
            "total_paid": real_total_paid,
            "hashrate": real_hashrate,
            "spm": total_spm,
            "worker": worker_count,
            "estimate": safe_float(data.get("paid24h", 0)),
            "minpay": safe_float(data.get("minpay", 0)),
            "workers_raw": workers_dict,
            "payouts": payouts
        }


async def send_discord(title: str, message: str, color: int = 15158332):
    if not config["notifications"]["discord"]["enabled"]:
        return
    webhook_url = config["notifications"]["discord"]["webhook_url"]
    if not webhook_url:
        return
    payload = {
        "embeds": [{
            "title": title.strip(),
            "description": message.strip(),
            "color": color,
            "timestamp": datetime.now().isoformat(),
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
    user = config["notifications"]["ntfy"]["user"]
    password = config["notifications"]["ntfy"]["pass"]
    
    if not topic:
        return

    safe_title = str(title).strip().encode('ascii', 'ignore').decode('ascii')
    headers = {
        "Title": safe_title,
        "Priority": str(priority),
        "Tags": "warning" if priority >= 4 else "info"
    }
    auth_tuple = (user, password) if user and password else None

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{server}/{topic}",
                content=str(message).encode('utf-8'),
                headers=headers,
                auth=auth_tuple,
                timeout=10
            )
        logger.info(f"ntfy sent: {safe_title}")
    except Exception as e:
        logger.error(f"ntfy error: {e}")


async def send_alert(title: str, message: str, alert_type: str = "warning"):
    now = datetime.now().timestamp()
    last = state["last_alert_sent"].get(alert_type, 0)
    if now - last < 300:
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
            "WARNING: No Workers Online",
            f"No workers are currently running.\nWallet: {config['wallet'][:10]}...",
            "warning"
        )
    
    balance = stats.get("balance", 0)
    if balance >= alerts_cfg["min_balance"]:
        await send_alert(
            "INFO: Balance Goal Reached",
            f"Balance: {balance:.8f} {currency}\nTarget: {alerts_cfg['min_balance']} {currency}",
            "info"
        )
    
    hr = stats.get("hashrate", 0)
    if state["max_hashrate"] > 0 and hr > 0:
        drop_percent = ((state["max_hashrate"] - hr) / state["max_hashrate"]) * 100
        if drop_percent >= alerts_cfg["hashrate_drop_percent"]:
            await send_alert(
                "WARNING: Hashrate Dropped",
                f"Hashrate dropped {drop_percent:.1f}%\nCurrent: {hr:.2f}\nMax: {state['max_hashrate']:.2f}",
                "warning"
            )


async def background_poller():
    await asyncio.sleep(2)
    while True:
        try:
            stats = await fetch_zpool_stats()
            state["stats"] = stats
            state["workers"] = stats.get("workers_raw", {})
            state["payments"] = stats.get("payouts", [])
            
            if stats["hashrate"] > state["max_hashrate"]:
                state["max_hashrate"] = stats["hashrate"]
            
            state["history"].append({
                "time": datetime.now().isoformat(),
                "hashrate": stats["hashrate"],
                "spm": stats["spm"],
                "balance": stats["balance"],
                "workers": stats["worker"]
            })
            state["history"] = state["history"][-100:]
            
            await check_alerts(stats)
            logger.info(f"Updated: HR={stats['hashrate']:.2f}, SPM={stats['spm']:.1f}, Bal={stats['balance']:.8f} {stats['currency']}")
            
        except Exception as e:
            logger.error(f"Polling error: {str(e)}")
        
        await asyncio.sleep(config["refresh_interval"])


@app.on_event("startup")
async def startup():
    logger.info("Starting zpool Monitor...")
    asyncio.create_task(background_poller())


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "healthy", "uptime": int((datetime.now() - state["start_time"]).total_seconds())}


@app.get("/api/stats")
async def api_stats():
    if state["stats"] is None:
        return {"error": "No data yet"}
    return {
        **state["stats"],
        "max_hashrate": state["max_hashrate"],
        "last_update": datetime.now().isoformat()
    }


@app.get("/api/workers")
async def api_workers():
    return state.get("workers", {})


@app.get("/api/payments")
async def api_payments():
    return state.get("payments", [])


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
            "ntfy": {
                "enabled": config["notifications"]["ntfy"]["enabled"],
                "has_auth": bool(config["notifications"]["ntfy"]["user"])
            }
        }
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)