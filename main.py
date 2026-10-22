import sqlite3
import asyncio
import json
import os
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiohttp import web
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent

# 考研日期设定 (2026年12月21日)
EXAM_DATE = datetime(2026, 12, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
TZ = ZoneInfo("Asia/Shanghai")
MEMO_DB_FILE = "/www/wwwroot/astrbot/data/plugin_data/astrbot_plugin_eink_api/eink_memos.db"

@register("eink_api", "你的名字", "墨水屏全能看板", "11.0.0")
class EinkApiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 三个核心数据库路径
        self.fee_db_path = "/www/wwwroot/astrbot/data/plugin_data/astrbot_plugin_dianfei/meters_data.db"
        self.news_db_path = "/www/wwwroot/astrbot/data/plugin_data/astrbot_plugin_ytunews/news.db"
        self.study_db_path = "/www/wwwroot/astrbot/data/plugin_data/astrbot_plugin_eink_api/study_data.db"
        
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
        logger.info("✅ 墨水屏全能看板 V11.0 已加载，启动 Web 服务...")
        asyncio.create_task(self.start_web_server())

    # ================= QQ 备忘录指令 =================
    @filter.command("备忘录")
    async def qq_add_memo(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("❌ 请附加备忘录内容，例如：/备忘录 明天交高数作业")
            return
        msg_text = parts[1].strip()[:60] # 限制60字
        now_str = datetime.now(tz=TZ).strftime("%m-%d %H:%M")
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
                conn.execute("INSERT INTO memos (content, time) VALUES (?, ?)", (msg_text, now_str))
                conn.commit()
            yield event.plain_result(f"✅ 已加入备忘录！墨水屏下次刷新即可看到。\n内容：{msg_text}")
        except Exception as e:
            logger.error(f"添加备忘录失败: {e}")
            yield event.plain_result("❌ 保存失败。")

    @filter.command("查备忘录")
    async def qq_list_memo(self, event: AstrMessageEvent):
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT content, time FROM memos ORDER BY id DESC LIMIT 5")
                rows = cursor.fetchall()
                if not rows:
                    yield event.plain_result("📭 目前备忘录是空的。")
                    return
                msg = "📝 **当前备忘录**\n━━━━━━━━━━━━\n"
                for i, r in enumerate(rows):
                    msg += f"{i+1}. [{r[1]}] {r[0]}\n"
                yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result("❌ 查询失败。")

    @filter.command("清备忘录")
    async def qq_clear_memo(self, event: AstrMessageEvent):
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
                conn.execute("DELETE FROM memos")
                conn.commit()
            yield event.plain_result("✅ 备忘录已全部清空，墨水屏将恢复日常看板。")
        except Exception as e:
            yield event.plain_result("❌ 清空失败。")

    # ================= 墨水屏 Web API =================
    async def handle_record(self, request):
        try:
            data = await request.json()
            subject = data.get("subject", "其他")
            minutes = int(data.get("minutes", 0))
            today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
            with sqlite3.connect(self.study_db_path, check_same_thread=False) as conn:
                conn.execute("INSERT INTO focus_records (date, subject, minutes) VALUES (?, ?, ?)", (today, subject, minutes))
                conn.commit()
            logger.info(f"⏱️ 收到墨水屏打卡: {subject} {minutes}分钟")
            return web.json_response({"status": "success"})
        except Exception as e:
            logger.error(f"打卡失败: {e}")
            return web.json_response({"status": "error", "msg": str(e)}, status=500)

    async def handle_all(self, request):
        addr_raw = request.query.get('addr', 'NS07N0422')
        today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
        days_to_exam = max(0, (EXAM_DATE - datetime.now(tz=TZ)).days)

        result_data = {
            "status": "success", "weather": "--", "days_to_exam": days_to_exam,
            "balance": 0.0, "news_list": [], "subject_times": {"政治":0, "英语":0, "数学":0, "专业课":0, "其他":0},
            "memos": []
        }

        # 1. 读取备忘录
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT content, time FROM memos ORDER BY id DESC LIMIT 5")
                for row in cursor.fetchall():
                    result_data["memos"].append({"content": row[0], "time": row[1]})
        except Exception as e:
            logger.error(f"查备忘录失败: {e}")

        # 2. 天气 (使用 wttr.in 莱山天气)
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
                    # 简单的英文转中文
                    desc_map = {"Sunny": "晴", "Clear": "晴", "Partly cloudy": "多云", "Cloudy": "多云", "Overcast": "阴", "Mist": "薄雾", "Light rain": "小雨"}
                    w_desc_zh = desc_map.get(w_desc, w_desc)
                    self._weather_cache = f"{w_desc_zh} {temp}°C ({low}~{high}°C)"
                    self._weather_cache_time = now_time
            if self._weather_cache:
                result_data["weather"] = self._weather_cache
        except Exception as e:
            logger.error(f"查天气失败: {e}")

        # 3. 电费 (⭐️调用原插件的地址转换函数查库)
        try:
            import importlib
            # 动态导入电费插件的 utils，把 "NS07N0422" 转成 "南校07N-422"
            fee_utils = importlib.import_module("data.plugins.astrbot_plugin_dianfei.utils")
            real_db_addr = fee_utils.convert_dorm_format(addr_raw.upper())
            
            with sqlite3.connect(self.fee_db_path, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT balance FROM meter_records WHERE user_addr=? ORDER BY id DESC LIMIT 1", (real_db_addr,))
                row = cursor.fetchone()
                if row: 
                    result_data["balance"] = float(row[0])
                else:
                    logger.warning(f"⚠️ 查不到电费，转换后的地址为: {real_db_addr}")
        except Exception as e:
            logger.error(f"查电费失败: {e}")

        # 4. 新闻 (⭐️直接按入库 ID 倒序抓最新 2 条)
        try:
            with sqlite3.connect(self.news_db_path, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT title, date FROM news ORDER BY id DESC LIMIT 2")
                rows = cursor.fetchall()
                for r in rows:
                    t = r[0]
                    d = r[1] if r[1] else "未知日期"
                    result_data["news_list"].append({"title": t[:22] + ".." if len(t) > 22 else t})
                    logger.info(f"📰 抓到新闻: [{d}] {t}")
        except Exception as e:
            logger.error(f"查新闻失败: {e}")

        # 5. 专注记录
        try:
            with sqlite3.connect(self.study_db_path, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT subject, SUM(minutes) FROM focus_records WHERE date=? GROUP BY subject", (today,))
                for sub, m in cursor.fetchall():
                    if sub in result_data["subject_times"]: result_data["subject_times"][sub] = int(m)
        except Exception as e:
            logger.error(f"查专注记录失败: {e}")

        return web.json_response(result_data, headers={"Access-Control-Allow-Origin": "*"})

    async def start_web_server(self):
        app = web.Application()
        app.router.add_get('/api/all', self.handle_all)
        app.router.add_post('/api/record', self.handle_record)
        # 跨域预请求
        app.router.add_options('/{tail:.*}', lambda r: web.Response(headers={"Access-Control-Allow-Origin": "*"}))
        
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', 8080)
        await site.start()
        logger.info("📺 墨水屏 API 已启动: http://0.0.0.0:8080")

    async def terminate(self):
        if self.runner: await self.runner.cleanup()
        if self.http_client: await self.http_client.aclose()
