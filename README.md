# zpool Monitor

Web UI ติดตามการขุดบน zpool.ca + แจ้งเตือน Discord/ntfy

## 🚀 Quick Start

```bash
# 1. Clone repo
git clone <your-repo> && cd zpool-monitor

# 2. ตั้งค่า .env
cp .env.example .env
nano .env   # แก้ ZPOOL_WALLET + เปิด Discord/ntfy

# 3. รัน
docker-compose up -d

# 4. เปิด http://localhost:8000