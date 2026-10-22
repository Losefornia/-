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

# 电费单价（度/元），如果你的电价不是 0.55，请在这里修改
PRICE_PER_KWH = 0.55

@register("eink_api", "你的名字", "墨水屏全功能看板", "24.0.0")
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
        logger.info("✅ 墨水屏全功能看板 V24.0 (电量计算版) 已加载！")
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
        """计算近三天用电量和今日小时均用电"""
        usage_3d = [0.0, 0.0, 0.0] # 0=今天, 1=昨天, 2=前天
        hourly_avg = 0.0
        
        try:
            with sqlite3.connect(FEE_DB_FILE, check_same_thread=False) as conn:
                # 取最近 4 天的所有记录（算差值需要前一天的结尾余额）
                now = datetime.now()
                four_days_ago = (now - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
                
                # 模糊匹配你的宿舍（比如 N-422），并且时间在最近4天内
                cursor = conn.execute(
                    "SELECT record_time, balance FROM meter_records WHERE user_addr LIKE '%N-422%' AND record_time >= ? ORDER BY record_time ASC",
                    (four_days_ago,)
                )
                rows = cursor.fetchall()
                
                if not rows: return usage_3d, hourly_avg

                # 按天分组处理
                daily_data = {} # {'2026-10-20': [bal1, bal2...], ...}
                for rt_str, bal in rows:
                    day_str = rt_str.split(" ")[0]
                    if day_str not in daily_data:
                        daily_data[day_str] = []
                    daily_data[day_str].append(bal)

                # 按日期倒序排，方便取今天、昨天、前天
                sorted_days = sorted(daily_data.keys(), reverse=True)
                
                for i in range(3):
                    if i < len(sorted_days):
                        day = sorted_days[i]
                        bals = daily_data[day]
                        if len(bals) >= 2:
                            # 一天内的最大余额减去最小余额，就是当天掉的钱
                            diff = max(bals) - min(bals)
                            # 如果是充值了，diff为负，算作0
                            usage_3d[i] = round(max(0, diff) / PRICE_PER_KWH, 2)
                            
                            # 如果是今天（i=0），计算小时均
                            if i == 0:
                                # 计算今天数据跨度了几个小时
                                start_t = datetime.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S")
                                end_t = now
                                hours_span = max(1, (end_t - start_t).total_seconds() / 3600)
                                hourly_avg = round(usage_3d[0] / hours_span, 2)
                                
            return usage_3d, hourly_avg
        except Exception as e:
            logger.error(f"计算出电量失败: {e}")
            return usage_3d, hourly_avg

    async def handle_all(self, request):
        today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
        days_to_exam = max(0, (EXAM_DATE - datetime.now(tz=TZ)).days)

        result_data = {
            "status": "success", "weather": "--", "days_to_exam": days_to_exam,
            "balance": 0.0, 
            "usage_3d": [0.0, 0.0, 0.0], # 近三天用电量
            "hourly_avg": 0.0,           # 今日小时均用电
            "news_list": [], 
            "subject_times": {"政治":0, "英语":0, "数学":0, "专业课":0, "其他":0},
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

        # 3. 查电费 & 计算
        try:
            with sqlite3.connect(FEE_DB_FILE, check_same_thread=False) as conn:
                # 拿最新的余额
                cursor = conn.execute("SELECT balance FROM meter_records WHERE user_addr LIKE '%N-422%' ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if row: 
                    result_data["balance"] = float(row[0])
            
            # 计算三天用量和小时均量
            usage_3d, hourly_avg = self.calc_usage("N-422")
            result_data["usage_3d"] = usage_3d
            result_data["hourly_avg"] = hourly_avg
            logger.info(f"📈 用电统计: 3天={usage_3d}, 小时均={hourly_avg}")
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
