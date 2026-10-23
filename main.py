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

try:
    import psutil
except ImportError:
    psutil = None

TZ = ZoneInfo("Asia/Shanghai")

MEMO_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/eink_memos.db"
STUDY_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/study_data.db"
FEE_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_dianfei/meters_data.db"
NEWS_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_ytunews/news.db"
CONFIG_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/eink_config.db"

PRICE_PER_KWH = 0.55
SUBJECT_LIST = ["英语", "数学", "政治", "专业课", "其他"]

@register("eink_api", "你的名字", "墨水屏看板｜多考试倒计时，直接设置当前周", "35.5.0")
class EinkApiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.http_client = None
        self.runner = None

        self._weather_cache = None
        self._weather_cache_time = None
        self._ds_cache = 0.0
        self._ds_cache_time = None

        self.current_subject = "英语"
        self.active_exam_name = "考研"
        self.week_num = 1

        self.init_dbs()
        self.load_config()

    def init_dbs(self):
        try:
            os.makedirs(os.path.dirname(STUDY_DB_FILE), exist_ok=True)
            with sqlite3.connect(STUDY_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS focus_records (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, subject TEXT, minutes INTEGER)")
                conn.commit()

            os.makedirs(os.path.dirname(MEMO_DB_FILE), exist_ok=True)
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS memos (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, time TEXT)")
                conn.commit()

            os.makedirs(os.path.dirname(CONFIG_DB_FILE), exist_ok=True)
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
                conn.commit()
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    def load_config(self):
        try:
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                row = conn.execute("SELECT value FROM config WHERE key='active_exam'").fetchone()
                if row:
                    self.active_exam_name = row[0]
                row = conn.execute("SELECT value FROM config WHERE key='curr_subject'").fetchone()
                if row and row[0] in SUBJECT_LIST:
                    self.current_subject = row[0]
                row = conn.execute("SELECT value FROM config WHERE key='week_num'").fetchone()
                if row and row[0].isdigit():
                    self.week_num = int(row[0])
        except Exception as e:
            logger.warning(f"加载配置失败: {e}")

    async def initialize(self):
        self.http_client = httpx.AsyncClient(timeout=10.0)
        if psutil:
            psutil.cpu_percent(interval=0.1)
        logger.info("✅ 墨水屏看板 V35.5｜支持手动设置教学周，多考试倒计时")
        asyncio.create_task(self.start_web_server())

    # ===== QQ 命令 =====
    @filter.command("添加考试")
    async def add_exam(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("格式：添加考试 六级 2026-12-13")
            return
        name = parts[1].strip()
        datestr = parts[2].strip()
        try:
            datetime.strptime(datestr, "%Y-%m-%d")
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES (?,?)", (f"exam:{name}", datestr))
                conn.commit()
            yield event.plain_result(f"✅ 添加成功：{name} {datestr}")
        except:
            yield event.plain_result("❌ 日期格式错误，使用 YYYY-MM-DD")

    @filter.command("删除考试")
    async def del_exam(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("格式：删除考试 六级")
            return
        name = parts[1].strip()
        try:
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("DELETE FROM config WHERE key=?", (f"exam:{name}",))
                conn.commit()
            if self.active_exam_name == name:
                self.active_exam_name = ""
                conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES ('active_exam','')")
                conn.commit()
            yield event.plain_result(f"✅ 已删除考试：{name}")
        except:
            yield event.plain_result("❌ 删除失败")

    @filter.command("切换倒计时")
    async def switch_exam(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("格式：切换倒计时 六级")
            return
        name = parts[1].strip()
        try:
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                r = conn.execute("SELECT 1 FROM config WHERE key=?", (f"exam:{name}",)).fetchone()
                if not r:
                    yield event.plain_result("❌ 没有这个考试，先用【添加考试】加上")
                    return
                self.active_exam_name = name
                conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES ('active_exam',?)", (name,))
                conn.commit()
            yield event.plain_result(f"✅ 当前倒计时切换为：{name}")
        except:
            yield event.plain_result("❌ 切换失败")

    @filter.command("考试列表")
    async def list_exam(self, event: AstrMessageEvent):
        try:
            now = datetime.now(tz=TZ).date()
            msg = "📋 考试列表：\n"
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                rows = conn.execute("SELECT key,value FROM config WHERE key LIKE 'exam:%'").fetchall()
                if not rows:
                    yield event.plain_result("暂无考试记录")
                    return
                for k, v in rows:
                    name = k.replace("exam:","")
                    d = datetime.strptime(v, "%Y-%m-%d").date()
                    diff = (d - now).days
                    if diff >= 0:
                        msg += f"{name}：剩余 {diff} 天\n"
                    else:
                        msg += f"{name}：已结束\n"
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(e)
            yield event.plain_result("❌ 查询失败")

    @filter.command("设置当前周")
    async def set_week(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            yield event.plain_result("格式：设置当前周 8")
            return
        w = int(parts[1])
        self.week_num = w
        try:
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES ('week_num',?)", (str(w),))
                conn.commit()
            yield event.plain_result(f"✅ 当前教学周设置为：第{w}周")
        except:
            yield event.plain_result("❌ 设置失败")

    @filter.command("选科目")
    async def qq_select_subject(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(f"可用科目：{' / '.join(SUBJECT_LIST)}")
            return
        sub = parts[1].strip()
        if sub not in SUBJECT_LIST:
            yield event.plain_result(f"无效科目！可选：{' / '.join(SUBJECT_LIST)}")
            return
        self.current_subject = sub
        try:
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES ('curr_subject',?)", (sub,))
                conn.commit()
            yield event.plain_result(f"✅ 当前专注科目切换为：{sub}")
        except:
            yield event.plain_result("❌ 保存失败")

    @filter.command("查专注")
    async def qq_check_focus(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        days = 0
        if len(parts) >= 2 and parts[1].isdigit():
            days = int(parts[1])
        try:
            with sqlite3.connect(STUDY_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                if days == 0:
                    today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
                    cursor = conn.execute("SELECT subject, SUM(minutes) FROM focus_records WHERE date=? GROUP BY subject", (today,))
                    rows = cursor.fetchall()
                    title = f"📊 **今日专注账本** ({today})"
                else:
                    start_date = (datetime.now(tz=TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
                    cursor = conn.execute("SELECT date, SUM(minutes) FROM focus_records WHERE date >= ? GROUP BY date ORDER BY date DESC", (start_date,))
                    rows = cursor.fetchall()
                    title = f"📊 **最近 {days} 天专注趋势**"
                if not rows:
                    yield event.plain_result(f"📊 {title}：暂无专注记录哦")
                    return
                msg = f"{title}\n━━━━━━━━━━━━━━━━\n"
                if days == 0:
                    total = sum(r[1] for r in rows)
                    for sub, mins in rows:
                        msg += f"📖 {sub}：{mins} 分钟\n"
                    msg += f"━━━━━━━━━━━━━━━━\n⏱️ 总计：{total} 分钟 ({round(total/60, 1)} 小时)"
                else:
                    total_all = 0
                    for date_str, mins in rows:
                        total_all += mins
                        msg += f"📅 {date_str}：{mins} 分钟 ({round(mins/60, 1)}h)\n"
                    msg += f"━━━━━━━━━━━━━━━━\n⏱️ {days}天总计：{total_all} 分钟 ({round(total_all/60, 1)} 小时)"
                yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"查专注失败: {e}")
            yield event.plain_result("❌ 查询失败")

    @filter.command("设key")
    async def qq_set_key(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2: return
        new_key = parts[1].strip()
        try:
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('deepseek_key', ?)", (new_key,))
                conn.commit()
            yield event.plain_result(f"✅ DeepSeek Key 已保存！")
        except:
            yield event.plain_result("❌ 保存失败。")

    @filter.command("备忘录")
    async def qq_add_memo(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2: return
        msg_text = parts[1].strip()[:60]
        now_str = datetime.now(tz=TZ).strftime("%m-%d %H:%M")
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("INSERT INTO memos (content, time) VALUES (?, ?)", (msg_text, now_str))
                conn.commit()
            yield event.plain_result(f"✅ 备忘录已存！\n内容：{msg_text}")
        except:
            yield event.plain_result("❌ 保存失败。")

    @filter.command("清屏")
    async def qq_clear_memo(self, event: AstrMessageEvent):
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("DELETE FROM memos")
                conn.commit()
            yield event.plain_result("✅ 备忘录已全部清空。")
        except:
            yield event.plain_result("❌ 清空失败。")

    # ================= 核心API =================
    async def handle_record(self, request):
        try:
            data = await request.json()
            subject = data.get("subject", "其他")
            minutes = int(data.get("minutes", 0))
            today = datetime.now(tz=TZ).strftime("%Y-%m-%d")
            with sqlite3.connect(STUDY_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                conn.execute("INSERT INTO focus_records (date, subject, minutes) VALUES (?, ?, ?)", (today, subject, minutes))
                conn.commit()
            return web.json_response({"status": "success"})
        except Exception as e:
            logger.error(f"打卡记录失败: {e}")
            return web.json_response({"status": "error", "msg": str(e)}, status=500)

    def calc_usage(self, addr):
        usage_3d = [0.0, 0.0, 0.0]
        hourly_avg = 0.0
        days_left = "--"
        try:
            with sqlite3.connect(FEE_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                now = datetime.now(tz=TZ)
                four_days_ago = (now - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
                cursor = conn.execute("SELECT record_time, balance FROM meter_records WHERE user_addr LIKE '%N-422%' AND record_time >= ? ORDER BY record_time ASC", (four_days_ago,))
                rows = cursor.fetchall()
                if not rows: return usage_3d, hourly_avg, days_left
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
                            diff = bals[0] - bals[-1]
                            if diff > 0: usage_3d[i] = round(diff / PRICE_PER_KWH, 2)
                            else: usage_3d[i] = 0.0
                            if i == 0:
                                start_t = datetime.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                                hours_span = max(1, (now - start_t).total_seconds() / 3600)
                                if usage_3d[0] > 0: hourly_avg = round(usage_3d[0] / hours_span, 2)
                valid_usages = [u for u in usage_3d if u > 0]
                if valid_usages:
                    avg_daily = sum(valid_usages) / len(valid_usages)
                    if avg_daily > 0:
                        current_bal = rows[-1][1]
                        days_left_val = max(0, current_bal / avg_daily)
                        if days_left_val < 1:
                            days_left = "不足1天"
                        else:
                            days_left = str(int(days_left_val))
            return usage_3d, hourly_avg, days_left
        except Exception as e:
            logger.warning(f"[电费模块] 计算失败: {e}")
            return usage_3d, hourly_avg, days_left

    async def handle_all(self, request):
        now = datetime.now(tz=TZ)
        today = now.strftime("%Y-%m-%d")
        week_day_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

        # 读取所有考试、计算剩余天数
        exam_list = []
        current_exam_obj = {"name":"","days":0}
        try:
            today_date = now.date()
            with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                rows = conn.execute("SELECT key,value FROM config WHERE key LIKE 'exam:%'").fetchall()
                for k, v in rows:
                    name = k.replace("exam:","")
                    exam_date = datetime.strptime(v, "%Y-%m-%d").date()
                    days = (exam_date - today_date).days
                    exam_list.append({"name":name, "days":days})
                    if name == self.active_exam_name:
                        current_exam_obj = {"name":name, "days":days}
        except Exception as e:
            logger.warning(f"考试读取 {e}")

        result_data = {
            "status": "success",
            "weather": "--",
            "exam_list": exam_list,
            "current_exam": current_exam_obj,
            "current_subject": self.current_subject,
            "week_num": self.week_num,
            "week_day": week_day_cn,

            "balance": 0.0,
            "usage_3d": [0.0, 0.0, 0.0],
            "hourly_avg": 0.0,
            "days_left": "--",

            "news_list": [],
            "subject_times": {"政治":0, "英语":0, "数学":0, "专业课":0, "其他":0},
            "memos": [],
            "server_status": {"cpu": 0, "mem": 0, "disk": 0},
            "deepseek_balance": 0.0,
            "total_minutes": 0
        }

        # 备忘录
        try:
            with sqlite3.connect(MEMO_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                cursor = conn.execute("SELECT content, time FROM memos ORDER BY id DESC LIMIT 5")
                for row in cursor.fetchall():
                    result_data["memos"].append({"content": row[0], "time": row[1]})
        except Exception as e:
            logger.warning(f"备忘录读取 {e}")

        # 天气
        try:
            now_time = datetime.now(tz=TZ)
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
            if self._weather_cache: result_data["weather"] = self._weather_cache
        except Exception as e:
            logger.warning(f"天气 {e}")

        # 电费
        try:
            with sqlite3.connect(FEE_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                cursor = conn.execute("SELECT balance FROM meter_records WHERE user_addr LIKE '%N-422%' ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if row: result_data["balance"] = float(row[0])
            u3d, havg, dleft = self.calc_usage("N-422")
            result_data["usage_3d"] = u3d
            result_data["hourly_avg"] = havg
            result_data["days_left"] = dleft
        except Exception as e:
            logger.warning(f"电费 {e}")

        # 新闻
        try:
            with sqlite3.connect(NEWS_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                cursor = conn.execute("SELECT title FROM news WHERE title IS NOT NULL AND title != '' ORDER BY id DESC LIMIT 2")
                rows = cursor.fetchall()
                for r in rows:
                    t = r[0]
                    result_data["news_list"].append({"title": t[:22] + ".." if len(t) > 22 else t})
        except Exception as e:
            logger.warning(f"新闻 {e}")

        # 专注统计
        try:
            total_m = 0
            with sqlite3.connect(STUDY_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                cursor = conn.execute("SELECT subject, SUM(minutes) FROM focus_records WHERE date=? GROUP BY subject", (today,))
                for sub, m in cursor.fetchall():
                    if sub in result_data["subject_times"]:
                        result_data["subject_times"][sub] = int(m)
                        total_m += int(m)
            result_data["total_minutes"] = total_m
        except Exception as e:
            logger.warning(f"专注统计 {e}")

        # 服务器
        if psutil:
            try:
                result_data["server_status"]["cpu"] = psutil.cpu_percent(interval=0.1)
                mem = psutil.virtual_memory()
                result_data["server_status"]["mem"] = mem.percent
                result_data["server_status"]["disk"] = psutil.disk_usage('/').percent
            except Exception as e:
                logger.warning(f"服务器状态 {e}")

        # DSH余额
        try:
            now_time = datetime.now(tz=TZ)
            if self._ds_cache_time is None or (now_time - self._ds_cache_time) > timedelta(hours=1):
                api_key = ""
                if os.path.exists(CONFIG_DB_FILE):
                    with sqlite3.connect(CONFIG_DB_FILE, check_same_thread=False, timeout=2.0) as conn:
                        cursor = conn.execute("SELECT value FROM config WHERE key = 'deepseek_key'")
                        row = cursor.fetchone()
                        if row: api_key = row[0]
                if api_key:
                    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
                    r = await self.http_client.get("https://api.deepseek.com/user/balance", headers=headers, timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        for info in data.get("balance_infos", []):
                            if info.get("currency") == "CNY":
                                self._ds_cache = float(info["total_balance"])
                                self._ds_cache_time = now_time
                                break
            result_data["deepseek_balance"] = self._ds_cache
        except Exception as e:
            logger.warning(f"DSH余额 {e}")

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
