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

EXAM_DATE = datetime(2026, 12, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
TZ = ZoneInfo("Asia/Shanghai")

MEMO_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/eink_memos.db"
STUDY_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/study_data.db"
FEE_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_dianfei/meters_data.db"
NEWS_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_ytunews/news.db"

@register("eink_api", "你的名字", "墨水屏全功能看板", "21.0.0")
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

    async def initialize(self):
        self.http_client = httpx.AsyncClient(timeout=10.0)
        logger.info("✅ 墨水屏全功能看板 V21.0 (锁定N422) 已加载！")
        asyncio.create_task(self.start_web_server())

    @filter.command("备忘录")
    async def qq_add_memo(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2: return
        msg_text = parts[1].strip()[:60]
        now_str = datetime.now(tz=TZ).strftime("%m-%d %H:%M")
        with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
            conn.execute("INSERT INTO memos (content, time) VALUES (?, ?)", (msg_text, now_str))
            conn.commit()
        yield event.plain_result(f"✅ 备忘录已存！墨水屏将全屏显示。\n内容：{msg_text}")

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

    async def handle_all(self, request):
        today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
        days_to_exam = max(0, (EXAM_DATE - datetime.now(tz=TZ)).days)

        result_data = {
            "status": "success", "weather": "--", "days_to_exam": days_to_exam,
            "balance": 0.0, "news_list": [], "subject_times": {"政治":0, "英语":0, "数学":0, "专业课":0, "其他":0},
            "memos": []
        }

        # 1. 备忘录
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT content, time FROM memos ORDER BY id DESC LIMIT 5")
                for row in cursor.fetchall():
                    result_data["memos"].append({"content": row[0], "time": row[1]})
        except: pass

        # 2. 天气
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

        # 3. 查电费 (⭐️精准模糊匹配 N422)
        try:
            with sqlite3.connect(FEE_DB_FILE, check_same_thread=False) as conn:
                # 使用 LIKE '%N422%' 只要有这三个字符就抓出来！
                cursor = conn.execute("SELECT user_addr, balance FROM meter_records WHERE user_addr LIKE '%N422%' ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if row: 
                    result_data["balance"] = float(row[1])
                    logger.info(f"💰 成功锁定你的宿舍: {row[0]}, 余额: {row[1]}")
                else:
                    logger.warning("⚠️ 数据库中找不到包含 N422 的宿舍号！")
        except Exception as e:
            logger.error(f"查电费报错: {e}")

        # 4. 查新闻
        try:
            with sqlite3.connect(NEWS_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT title FROM news WHERE title IS NOT NULL AND title != '' ORDER BY id DESC LIMIT 2")
                rows = cursor.fetchall()
                for r in rows:
                    result_data["news_list"].append({"title": r[0][:22] + ".." if len(r[0]) > 22 else r[0]})
        except: pass

        # 5. 查今日专注
        try:
            with sqlite3.connect(STUDY_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT subject, SUM(minutes) FROM focus_records WHERE date=? GROUP BY subject", (today,))
                for sub, m in cursor.fetchall():
                    if sub in result_data["subject_times"]: 
                        result_data["subject_times"][sub] = int(m)
        except: pass

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
