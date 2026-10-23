import sqlite3
import asyncio
import json
import os
import re
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiohttp import web
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent

try:
    import psutil
except ImportError:
    psutil = None

EXAM_DATE = datetime(2026, 12, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
TZ = ZoneInfo("Asia/Shanghai")

MEMO_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/eink_memos.db"
STUDY_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/study_data.db"
FEE_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_dianfei/meters_data.db"
NEWS_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_ytunews/news.db"
CONFIG_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/eink_config.db" # 新增：用来存密钥的库

PRICE_PER_KWH = 0.55

@register("eink_api", "你的名字", "墨水屏全功能看板", "27.0.0")
class EinkApiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.http_client: httpx.AsyncClient | None = None
        self.runner: web.AppRunner | None = None
        self._weather_cache = None
        self._weather_cache_time = None
        self.init_dbs()

    def init_dbs(self):
        os.makedirs(os.path.dirname(STUDY_DB_FILE), exist_ok=True)
        with sqlite3.connect(STUDY_DB_FILE, check_same_thread=False) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS focus_records (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, subject TEXT, minutes INTEGER)")
            conn.commit()

        os.makedirs(os.path.dirname(MEMO_DB_FILE), exist_ok=True)
        with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS memos (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, time TEXT)")
            conn.commit()
            
        # 初始化配置库
        os.makedirs(os.path.dirname(CONFIG_DB_FILE), exist_ok=True)
        with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()

    async def initialize(self):
        self.http_client = httpx.AsyncClient(timeout=10.0)
        logger.info("✅ 墨水屏全能看板 V27.0 (安全版) 已加载！")
        asyncio.create_task(self.start_web_server())

    # ================= QQ 设置密钥指令 =================
    @filter.command("设key")
    async def qq_set_key(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("❌ 请附加你的 DeepSeek API Key，例如：/设key sk-xxxxxx")
            return
        
        new_key = parts[1].strip()
        with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False) as conn:
            conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('deepseek_key', ?)", (new_key,))
            conn.commit()
        yield event.plain_result(f"✅ DeepSeek Key 已安全保存到服务器数据库！\n墨水屏下次刷新即可获取最新余额。")

    @filter.command("备忘录")
    async def qq_add_memo(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2: return
        msg_text = parts[1].strip()[:60]
        now_str = datetime.now(tz=TZ).strftime("%m-%d %H:%M")
        with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
            conn.execute("INSERT INTO memos (content, time) VALUES (?, ?)", (msg_text, now_str))
            conn.commit()
        yield event.plain_result(f"✅ 备忘录已存！\n内容：{msg_text}")

    async def handle_record(self, request):
        try:
            data = await request.json()
            subject = data.get("subject", "其他")
            minutes = int(data.get("minutes", 0))
            today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
            with sqlite3.connect(STUDY_DB_FILE, check_same_thread=False) as conn:
                conn.execute("INSERT INTO focus_records (date, subject, minutes) VALUES (?, ?, ?)", (today, subject, minutes))
                conn.commit()
            return web.json_response({"status": "success"})
        except Exception as e:
            return web.json_response({"status": "error", "msg": str(e)}, status=500)

    def calc_usage(self, addr):
        # （此处保留之前的电量计算逻辑，为了节省字数不重复贴，原样保留即可）
        usage_3d = [0.0, 0.0, 0.0]
        hourly_avg = 0.0
        try:
            with sqlite3.connect(FEE_DB_FILE, check_same_thread=False) as conn:
                now = datetime.now()
                four_days_ago = (now - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
                cursor = conn.execute("SELECT record_time, balance FROM meter_records WHERE user_addr LIKE '%N-422%' AND record_time >= ? ORDER BY record_time ASC", (four_days_ago,))
                rows = cursor.fetchall()
                if not rows: return usage_3d, hourly_avg
                daily_data = {}
                for rt_str, bal in rows:
                    day_str = rt_str.split(" ")[0]
                    if day_str not in daily_data: daily_data[day_str] = []
                    daily_data[day_str].append(bal)
                sorted_days = sorted(daily_data.keys(), reverse=True)
                for i in range(3):
                    if i < len(sorted_days):
                        day = sorted_days[i]
                        bals = daily_data[day]
                        if len(bals) >= 2:
                            diff = max(bals) - min(bals)
                            usage_3d[i] = round(max(0, diff) / PRICE_PER_KWH, 2)
                            if i == 0:
                                start_t = datetime.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S")
                                hours_span = max(1, (now - start_t).total_seconds() / 3600)
                                hourly_avg = round(usage_3d[0] / hours_span, 2)
            return usage_3d, hourly_avg
        except: return usage_3d, hourly_avg

    async def handle_all(self, request):
        today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
        days_to_exam = max(0, (EXAM_DATE - datetime.now(tz=TZ)).days)

        result_data = {
            "status": "success", "weather": "--", "days_to_exam": days_to_exam,
            "balance": 0.0, "usage_3d": [0.0, 0.0, 0.0], "hourly_avg": 0.0,
            "news_list": [], "subject_times": {"政治":0, "英语":0, "数学":0, "专业课":0, "其他":0},
            "memos": [], "server_status": {"cpu": 0, "mem": 0, "disk": 0},
            "deepseek_balance": 0.0
        }

        # ... (备忘录、天气、电费、新闻、专注等代码与之前一致，略过以省空间) ...
        # 如果你需要完整这部分，可以随时告诉我，我马上补全。
        
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT content, time FROM memos ORDER BY id DESC LIMIT 5")
                for row in cursor.fetchall():
                    result_data["memos"].append({"content": row[0], "time": row[1]})
        except: pass

        try:
            now_time = datetime.now()
            if self._weather_cache is None or (now_time - self._weather_cache_time) > timedelta(minutes=10):
                r = await self.http_client.get("https://wttr.in/烟台莱山?format=j1", timeout=10)
                if r.status_code == 200:
                    w_data = r.json()
                    temp = w_data["current_condition"][0]["temp_C"]
                    w_desc = w_data["current_condition"][0]["weatherDesc"][0]["value"].strip()
                    high = w_data["weather"][0]["maxtempC"]
                    low = w_data["weather"][0]["mintempC"]
                    desc_map = {"Sunny": "晴", "Clear": "晴", "Partly cloudy": "多云", "Cloudy": "多云", "Overcast": "阴", "Mist": "薄雾", "Light rain": "小雨"}
                    self._weather_cache = f"{desc_map.get(w_desc, w_desc)} {temp}°C ({low}~{high}°C)"
                    self._weather_cache_time = now_time
            if self._weather_cache:
                result_data["weather"] = self._weather_cache
        except: pass

        try:
            with sqlite3.connect(FEE_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT balance FROM meter_records WHERE user_addr LIKE '%N-422%' ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if row: result_data["balance"] = float(row[0])
            usage_3d, hourly_avg = self.calc_usage("N-422")
            result_data["usage_3d"] = usage_3d
            result_data["hourly_avg"] = hourly_avg
        except: pass

        try:
            with sqlite3.connect(NEWS_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT title FROM news WHERE title IS NOT NULL AND title != '' ORDER BY id DESC LIMIT 2")
                rows = cursor.fetchall()
                for r in rows:
                    result_data["news_list"].append({"title": r[0][:22] + ".." if len(r[0]) > 22 else r[0]})
        except: pass

        try:
            with sqlite3.connect(STUDY_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT subject, SUM(minutes) FROM focus_records WHERE date=? GROUP BY subject", (today,))
                for sub, m in cursor.fetchall():
                    if sub in result_data["subject_times"]: result_data["subject_times"][sub] = int(m)
        except: pass

        if psutil:
            try:
                result_data["server_status"]["cpu"] = psutil.cpu_percent(interval=None)
                result_data["server_status"]["mem"] = psutil.virtual_memory().percent
                result_data["server_status"]["disk"] = psutil.disk_usage('/').percent
            except: pass

        # ================= 【核心修改】从数据库读取Key查余额 =================
        try:
            # 1. 先从库里拿 Key
            api_key = ""
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT value FROM config WHERE key = 'deepseek_key'")
                row = cursor.fetchone()
                if row: api_key = row[0]
            
            # 2. 如果有 Key，就去查余额
            if api_key:
                headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
                r = await self.http_client.get("https://api.deepseek.com/user/balance", headers=headers, timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    for info in data.get("balance_infos", []):
                        if info.get("currency") == "CNY":
                            result_data["deepseek_balance"] = float(info["total_balance"])
                            break
            else:
                logger.warning("⚠️ 还没有设置 DeepSeek Key，请在QQ发送：/设key 你的密钥")
        except Exception as e:
            logger.error(f"查 DeepSeek 余额报错: {e}")

        return web.json_response(result_data, headers={"Access-Control-Allow-Origin": "*"})

    async def start_web_server(self):
        app = web.Application()
        app.router.add_get('/api/all', self.handle_all)
        app.router.add_post('/api/record', self.handle_record)
        app.router.add_options('/{tail:.*}', lambda r: web.Response(headers={"Access-Control-Allow-Origin": "*"}))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', 8080)
        await site.start()
        logger.info("📺 墨水屏 API 已启动: http://0.0.0.0:8080")

    async def terminate(self):
        if self.runner: await self.runner.cleanup()
        if self.http_client: await self.http_client.aclose()
