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

# ⚠️【修正】：全部改为 Docker 容器内部的真实路径
MEMO_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/eink_memos.db"

@register("eink_api", "你的名字", "墨水屏看板", "17.0.0")
class EinkApiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # ⚠️【修正】：全部改为 Docker 容器内部的真实路径
        self.fee_db_path = "/AstrBot/data/plugin_data/astrbot_plugin_dianfei/meters_data.db"
        self.news_db_path = "/AstrBot/data/plugin_data/astrbot_plugin_ytunews/news.db"
        self.study_db_path = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/study_data.db"
        
        self.http_client: httpx.AsyncClient | None = None
        self.runner: web.AppRunner | None = None
        self._weather_cache = None
        self._weather_cache_time = None
        
        self.init_study_db()
        self.init_memo_db()

    def init_study_db(self):
        os.makedirs(os.path.dirname(self.study_db_path), exist_ok=True)
        with sqlite3.connect(self.study_db_path, check_same_thread=False) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS focus_records (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, subject TEXT, minutes INTEGER)")
            conn.commit()

    def init_memo_db(self):
        os.makedirs(os.path.dirname(MEMO_DB_FILE), exist_ok=True)
        with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS memos (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, time TEXT)")
            conn.commit()

    async def initialize(self):
        self.http_client = httpx.AsyncClient(timeout=10.0)
        logger.info("✅ 墨水屏全能看板 V17.0 (Docker路径修复版) 已加载！")
        
        # 🚨 启动自检
        try:
            with sqlite3.connect(self.fee_db_path, check_same_thread=False) as conn:
                c = conn.execute("SELECT COUNT(*) FROM meter_records").fetchone()[0]
                logger.info(f"🔍 [自检] 服务器电费库总记录数: 【 {c} 】条")
        except Exception as e:
            logger.error(f"🔍 [自检] 电费库读取失败: {e}")

        try:
            with sqlite3.connect(self.news_db_path, check_same_thread=False) as conn:
                c = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
                logger.info(f"🔍 [自检] 服务器新闻库总记录数: 【 {c} 】条")
        except Exception as e:
            logger.error(f"🔍 [自检] 新闻库读取失败: {e}")

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
        yield event.plain_result(f"✅ 已加入备忘录！\n内容：{msg_text}")

    async def handle_record(self, request):
        try:
            data = await request.json()
            subject = data.get("subject", "其他")
            minutes = int(data.get("minutes", 0))
            today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
            with sqlite3.connect(self.study_db_path, check_same_thread=False) as conn:
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
                    current = w_data["current_condition"][0]
                    today_w = w_data["weather"][0]
                    temp = current["temp_C"]
                    w_desc = current["weatherDesc"][0]["value"].strip()
                    high = today_w["maxtempC"]
                    low = today_w["mintempC"]
                    desc_map = {"Sunny": "晴", "Clear": "晴", "Partly cloudy": "多云", "Cloudy": "多云", "Overcast": "阴", "Mist": "薄雾", "Light rain": "小雨"}
                    self._weather_cache = f"{desc_map.get(w_desc, w_desc)} {temp}°C ({low}~{high}°C)"
                    self._weather_cache_time = now_time
            if self._weather_cache:
                result_data["weather"] = self._weather_cache
        except: pass

        # 3. 查电费 (直接盲抓最新一条)
        try:
            with sqlite3.connect(self.fee_db_path, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT balance FROM meter_records WHERE balance IS NOT NULL ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if row: 
                    result_data["balance"] = float(row[0])
                    logger.info(f"💰 [接口] 成功抓到电费: {row[0]}")
        except Exception as e:
            logger.error(f"查电费报错: {e}")

        # 4. 查新闻 (直接盲抓最新两条)
        try:
            with sqlite3.connect(self.news_db_path, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT title FROM news WHERE title IS NOT NULL AND title != '' ORDER BY id DESC LIMIT 2")
                rows = cursor.fetchall()
                for r in rows:
                    result_data["news_list"].append({"title": r[0][:22] + ".." if len(r[0]) > 22 else r[0]})
                    logger.info(f"📰 [接口] 成功抓到新闻: {r[0]}")
        except Exception as e:
            logger.error(f"查新闻报错: {e}")

        # 5. 专注记录
        try:
            with sqlite3.connect(self.study_db_path, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT subject, SUM(minutes) FROM focus_records WHERE date=? GROUP BY subject", (today,))
                for sub, m in cursor.fetchall():
                    if sub in result_data["subject_times"]: result_data["subject_times"][sub] = int(m)
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
