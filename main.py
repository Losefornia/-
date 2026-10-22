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

# 考研日期设定
EXAM_DATE = datetime(2026, 12, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
TZ = ZoneInfo("Asia/Shanghai")
MEMO_DB_FILE = "/www/wwwroot/astrbot/data/plugin_data/astrbot_plugin_eink_api/eink_memos.db"

@register("eink_api", "你的名字", "墨水屏与QQ备忘录", "8.0.0")
class EinkApiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.fee_db_path = "/www/wwwroot/astrbot/data/plugin_data/astrbot_plugin_dianfei/meters_data.db"
        self.news_db_path = "/www/wwwroot/astrbot/data/plugin_data/astrbot_plugin_ytunews/news.db"
        self.study_db_path = "/www/wwwroot/astrbot/data/plugin_data/astrbot_plugin_eink_api/study_data.db"
        self.http_client: httpx.AsyncClient | None = None
        self.runner: web.AppRunner | None = None
        
        # 天气缓存（10分钟）
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
        self.http_client = httpx.AsyncClient(timeout=5.0)
        logger.info("✅ 墨水屏备忘录 V8.0 已加载！")
        asyncio.create_task(self.start_web_server())

    # ================= QQ 备忘录指令 =================
    
    @filter.command("备忘录")
    async def qq_add_memo(self, event: AstrMessageEvent):
        # 【修复】：安全提取内容，防止内容包含"备忘录"被误删
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("❌ 请附加备忘录内容，例如：/备忘录 明天交高数作业")
            return
        
        msg_text = parts[1].strip()[:60] # 【修复】：限制长度60字，防止墨水屏溢出
        
        now_str = datetime.now(tz=TZ).strftime("%m-%d %H:%M")
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
                conn.execute("INSERT INTO memos (content, time) VALUES (?, ?)", (msg_text, now_str))
                conn.commit()
            logger.info(f"📝 新增备忘录: {msg_text}")
            yield event.plain_result(f"✅ 已加入备忘录！墨水屏下次刷新即可看到。\n内容：{msg_text}")
        except Exception as e:
            logger.error(f"添加备忘录失败: {e}")
            yield event.plain_result("❌ 保存备忘录失败，请查看后台日志。")

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
            logger.error(f"查询备忘录失败: {e}")
            yield event.plain_result("❌ 查询失败，请查看后台日志。")

    @filter.command("清备忘录")
    async def qq_clear_memo(self, event: AstrMessageEvent):
        # 按你的要求，不做管理员限制，谁都能清
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False) as conn:
                conn.execute("DELETE FROM memos")
                conn.commit()
            yield event.plain_result("✅ 备忘录已全部清空，墨水屏将恢复日常看板。")
        except Exception as e:
            logger.error(f"清空备忘录失败: {e}")
            yield event.plain_result("❌ 清空失败，请查看后台日志。")

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
            logger.info(f"⏱️ 收到墨水屏打卡: {subject} {minutes}分钟") # 【修复】：增加打卡日志
            return web.json_response({"status": "success"})
        except Exception as e:
            logger.error(f"打卡失败: {e}")
            return web.json_response({"status": "error", "msg": str(e)}, status=500)

    async def handle_all(self, request):
        logger.info("📺 墨水屏请求数据...") # 【修复】：增加访问日志
        addr_raw = request.query.get('addr', 'NS07N0422')
        today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
        days_to_exam = max(0, (EXAM_DATE - datetime.now(tz=TZ)).days)

        result_data = {
            "status": "success", "weather": "莱山 --°C", "days_to_exam": days_to_exam,
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

        # 2. 天气（带 10 分钟缓存）
        try:
            now_time = datetime.now()
            if self._weather_cache is None or (now_time - self._weather_cache_time) > timedelta(minutes=10):
                r = await self.http_client.get("http://t.weather.itboy.net/api/weather/city/101120513")
                if r.status_code == 200: # 【修复】：先判断 http 状态
                    w_data = r.json()
                    if w_data.get("status") == 200:
                        temp = w_data["data"]["wendu"]
                        w_type = w_data["data"]["forecast"][0]["type"]
                        high = w_data["data"]["forecast"][0]["high"].split()[-1]
                        low = w_data["data"]["forecast"][0]["low"].split()[-1]
                        self._weather_cache = f"{w_type} {temp}°C ({low}~{high})"
                        self._weather_cache_time = now_time
                else:
                    logger.error(f"天气接口返回错误码: {r.status_code}")
            
            if self._weather_cache:
                result_data["weather"] = self._weather_cache
        except Exception as e:
            logger.error(f"查天气异常: {e}")

        # 3. 电费
        try:
            with sqlite3.connect(self.fee_db_path, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT balance FROM meter_records WHERE user_addr=? ORDER BY id DESC LIMIT 1", (addr_raw,))
                row = cursor.fetchone()
                if row: result_data["balance"] = float(row[0])
        except Exception as e:
            logger.error(f"查电费失败: {e}")

        # 4. 新闻
        try:
            with sqlite3.connect(self.news_db_path, check_same_thread=False) as conn:
                cursor = conn.execute("SELECT title FROM news ORDER BY date DESC, id DESC LIMIT 2")
                for r in cursor.fetchall():
                    t = r[0]
                    result_data["news_list"].append({"title": t[:22] + ".." if len(t) > 22 else t})
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
        
        # 【修复】：增加 OPTIONS 预请求跨域支持
        app.router.add_options('/{tail:.*}', lambda r: web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        }))

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', 8080)
        await site.start()
        logger.info("📺 墨水屏 API 已启动: http://0.0.0.0:8080")

    async def terminate(self):
        if self.runner: await self.runner.cleanup()
        if self.http_client: await self.http_client.aclose()
