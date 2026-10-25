import sqlite3
import asyncio
import os
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from aiohttp import web
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent

try:
    import psutil
except ImportError:
    psutil = None

TZ = ZoneInfo("Asia/Shanghai")

MEMO_DB_FILE   = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/eink_memos.db"
STUDY_DB_FILE  = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/study_data.db"
FEE_DB_FILE    = "/AstrBot/data/plugin_data/astrbot_plugin_dianfei/meters_data.db"
NEWS_DB_FILE   = "/AstrBot/data/plugin_data/astrbot_plugin_ytunews/news.db"
CONFIG_DB_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_eink_api/eink_config.db"

PRICE_PER_KWH = 0.55
SUBJECT_LIST  = ["英语", "数学", "政治", "专业课", "其他"]
WEEK_CN       = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
METER_ADDR    = "N-422"
WEB_PORT      = 8080

WEATHER_TTL   = timedelta(minutes=10)
DSH_TTL       = timedelta(hours=1)

# CPU 滑动均值：5s * 24 = 120s 窗口，适合墨水屏分钟级刷新
CPU_SAMPLE_INTERVAL = 5
CPU_SAMPLE_WINDOW   = 24


# ============ 通用工具 ============

@contextmanager
def _db(path: str):
    """自动 commit / rollback / close 的 sqlite 连接。"""
    conn = sqlite3.connect(path, check_same_thread=False, timeout=2.0)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now():
    return datetime.now(tz=TZ)


# ============ 插件主体 ============

@register("eink_api", "你的名字", "墨水屏看板｜多考试倒计时，直接设置当前周", "35.6.0")
class EinkApiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.http_client = None
        self.runner = None
        self._web_task = None
        self._sampler_task = None

        self._weather_cache = None
        self._weather_cache_time = None
        self._ds_cache = 0.0
        self._ds_cache_time = None

        # CPU 滑动窗口
        self._cpu_samples = deque(maxlen=CPU_SAMPLE_WINDOW)

        self.current_subject = "英语"
        self.active_exam_name = "考研"
        self.week_num = 1
        self.meter_addr = METER_ADDR

        self.init_dbs()
        self.load_config()

    # ---------- DB 初始化 ----------
    def init_dbs(self):
        schemas = [
            (STUDY_DB_FILE,
             "CREATE TABLE IF NOT EXISTS focus_records ("
             "id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, subject TEXT, minutes INTEGER)"),
            (MEMO_DB_FILE,
             "CREATE TABLE IF NOT EXISTS memos ("
             "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, time TEXT)"),
            (CONFIG_DB_FILE,
             "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)"),
        ]
        for path, sql in schemas:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with _db(path) as conn:
                    conn.execute(sql)
            except Exception as e:
                logger.error(f"数据库初始化失败 {path}: {e}")

    # ---------- 配置读写 ----------
    def _get_config(self, key: str, default=None):
        try:
            with _db(CONFIG_DB_FILE) as conn:
                row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
                return row[0] if row else default
        except Exception as e:
            logger.warning(f"读取配置 {key} 失败: {e}")
            return default

    def _set_config(self, key: str, value: str):
        with _db(CONFIG_DB_FILE) as conn:
            conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES (?,?)", (key, value))

    def load_config(self):
        v = self._get_config("active_exam")
        if v is not None:
            self.active_exam_name = v
        v = self._get_config("curr_subject")
        if v in SUBJECT_LIST:
            self.current_subject = v
        v = self._get_config("week_num")
        if v and v.isdigit():
            self.week_num = int(v)
        v = self._get_config("meter_addr")
        if v:
            self.meter_addr = v

    # ---------- 生命周期 ----------
    async def initialize(self):
        self.http_client = httpx.AsyncClient(timeout=10.0)
        if psutil:
            psutil.cpu_percent(interval=0.1)   # 预热，让首个样本有效
            self._sampler_task = asyncio.create_task(self._cpu_sampler())
        logger.info("✅ 墨水屏看板 V35.6｜并行聚合 + CPU 滑动均值")
        self._web_task = asyncio.create_task(self.start_web_server())

    async def terminate(self):
        if self._sampler_task and not self._sampler_task.done():
            self._sampler_task.cancel()
            try:
                await self._sampler_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._web_task and not self._web_task.done():
            self._web_task.cancel()
            try:
                await self._web_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.runner:
            await self.runner.cleanup()
        if self.http_client:
            await self.http_client.aclose()

    # ---------- CPU 采样器 ----------
    async def _cpu_sampler(self):
        """后台持续采样 CPU，供墨水屏读取窗口均值。"""
        while True:
            try:
                v = await asyncio.to_thread(psutil.cpu_percent, None)
                self._cpu_samples.append(v)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"CPU 采样失败: {e}")
            await asyncio.sleep(CPU_SAMPLE_INTERVAL)

    # ================= QQ 命令 =================
    @filter.command("添加考试")
    async def add_exam(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("格式：添加考试 六级 2026-12-13")
            return
        name, datestr = parts[1].strip(), parts[2].strip()
        try:
            datetime.strptime(datestr, "%Y-%m-%d")
        except ValueError:
            yield event.plain_result("❌ 日期格式错误，使用 YYYY-MM-DD")
            return
        try:
            self._set_config(f"exam:{name}", datestr)
            yield event.plain_result(f"✅ 添加成功：{name} {datestr}")
        except Exception as e:
            logger.error(f"添加考试失败: {e}")
            yield event.plain_result("❌ 添加失败")

    @filter.command("删除考试")
    async def del_exam(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("格式：删除考试 六级")
            return
        name = parts[1].strip()
        try:
            with _db(CONFIG_DB_FILE) as conn:
                conn.execute("DELETE FROM config WHERE key=?", (f"exam:{name}",))
                if self.active_exam_name == name:
                    self.active_exam_name = ""
                    conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES ('active_exam','')")
            yield event.plain_result(f"✅ 已删除考试：{name}")
        except Exception as e:
            logger.error(f"删除考试失败: {e}")
            yield event.plain_result("❌ 删除失败")

    @filter.command("切换倒计时")
    async def switch_exam(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("格式：切换倒计时 六级")
            return
        name = parts[1].strip()
        try:
            with _db(CONFIG_DB_FILE) as conn:
                exists = conn.execute("SELECT 1 FROM config WHERE key=?", (f"exam:{name}",)).fetchone()
            if not exists:
                yield event.plain_result("❌ 没有这个考试，先用【添加考试】加上")
                return
            self.active_exam_name = name
            self._set_config("active_exam", name)
            yield event.plain_result(f"✅ 当前倒计时切换为：{name}")
        except Exception as e:
            logger.error(f"切换考试失败: {e}")
            yield event.plain_result("❌ 切换失败")

    @filter.command("考试列表")
    async def list_exam(self, event: AstrMessageEvent):
        exams = self._get_all_exams()
        if not exams:
            yield event.plain_result("暂无考试记录")
            return
        msg = "📋 考试列表：\n"
        for e in exams:
            msg += f"{e['name']}：剩余 {e['days']} 天\n" if e["days"] >= 0 \
                   else f"{e['name']}：已结束\n"
        yield event.plain_result(msg)

    @filter.command("设置当前周")
    async def set_week(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            yield event.plain_result("格式：设置当前周 8")
            return
        w = int(parts[1])
        try:
            self.week_num = w
            self._set_config("week_num", str(w))
            yield event.plain_result(f"✅ 当前教学周设置为：第{w}周")
        except Exception as e:
            logger.error(f"设置周失败: {e}")
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
        try:
            self.current_subject = sub
            self._set_config("curr_subject", sub)
            yield event.plain_result(f"✅ 当前专注科目切换为：{sub}")
        except Exception as e:
            logger.error(f"选科目失败: {e}")
            yield event.plain_result("❌ 保存失败")

    @filter.command("查专注")
    async def qq_check_focus(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        days = 0
        if len(parts) >= 2 and parts[1].isdigit():
            days = int(parts[1])
        try:
            with _db(STUDY_DB_FILE) as conn:
                if days == 0:
                    today = _now().strftime("%Y-%m-%d")
                    rows = conn.execute(
                        "SELECT subject, SUM(minutes) FROM focus_records "
                        "WHERE date=? GROUP BY subject", (today,)
                    ).fetchall()
                    title = f"📊 **今日专注账本** ({today})"
                else:
                    start = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")
                    rows = conn.execute(
                        "SELECT date, SUM(minutes) FROM focus_records "
                        "WHERE date >= ? GROUP BY date ORDER BY date DESC", (start,)
                    ).fetchall()
                    title = f"📊 **最近 {days} 天专注趋势**"
        except Exception as e:
            logger.error(f"查专注失败: {e}")
            yield event.plain_result("❌ 查询失败")
            return

        if not rows:
            yield event.plain_result(f"{title}：暂无专注记录哦")
            return

        msg = f"{title}\n━━━━━━━━━━━━━━━━\n"
        if days == 0:
            total = sum(r[1] for r in rows)
            for sub, mins in rows:
                msg += f"📖 {sub}：{mins} 分钟\n"
            msg += f"━━━━━━━━━━━━━━━━\n⏱️ 总计：{total} 分钟 ({round(total/60, 1)} 小时)"
        else:
            total_all = sum(m for _, m in rows)
            for d, m in rows:
                msg += f"📅 {d}：{m} 分钟 ({round(m/60, 1)}h)\n"
            msg += f"━━━━━━━━━━━━━━━━\n⏱️ {days}天总计：{total_all} 分钟 ({round(total_all/60, 1)} 小时)"
        yield event.plain_result(msg)

    @filter.command("设key")
    async def qq_set_key(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            return
        try:
            self._set_config("deepseek_key", parts[1].strip())
            yield event.plain_result("✅ DeepSeek Key 已保存！")
        except Exception as e:
            logger.error(f"设置 key 失败: {e}")
            yield event.plain_result("❌ 保存失败。")

    @filter.command("备忘录")
    async def qq_add_memo(self, event: AstrMessageEvent):
        parts = event.message_str.split(maxsplit=1)
        if len(parts) < 2:
            return
        text = parts[1].strip()[:60]
        now_str = _now().strftime("%m-%d %H:%M")
        try:
            with _db(MEMO_DB_FILE) as conn:
                conn.execute("INSERT INTO memos (content, time) VALUES (?, ?)", (text, now_str))
            yield event.plain_result(f"✅ 备忘录已存！\n内容：{text}")
        except Exception as e:
            logger.error(f"备忘录失败: {e}")
            yield event.plain_result("❌ 保存失败。")

    @filter.command("清屏")
    async def qq_clear_memo(self, event: AstrMessageEvent):
        try:
            with _db(MEMO_DB_FILE) as conn:
                conn.execute("DELETE FROM memos")
            yield event.plain_result("✅ 备忘录已全部清空。")
        except Exception as e:
            logger.error(f"清屏失败: {e}")
            yield event.plain_result("❌ 清空失败。")

    # ================= 数据采集 =================
    def _get_all_exams(self):
        """返回按「未结束优先、剩余天数升序」排序的考试列表。"""
        today = _now().date()
        result = []
        try:
            with _db(CONFIG_DB_FILE) as conn:
                rows = conn.execute("SELECT key, value FROM config WHERE key LIKE 'exam:%'").fetchall()
            for k, v in rows:
                name = k[len("exam:"):]
                try:
                    d = datetime.strptime(v, "%Y-%m-%d").date()
                except ValueError:
                    continue
                result.append({"name": name, "date": d, "days": (d - today).days})
        except Exception as e:
            logger.warning(f"读取考试失败: {e}")
        result.sort(key=lambda x: (x["days"] < 0, x["days"]))
        return result

    def _fetch_memos(self):
        out = []
        try:
            with _db(MEMO_DB_FILE) as conn:
                for content, t in conn.execute(
                    "SELECT content, time FROM memos ORDER BY id DESC LIMIT 5"
                ).fetchall():
                    out.append({"content": content, "time": t})
        except Exception as e:
            logger.warning(f"备忘录 {e}")
        return out

    def _fetch_focus(self, today: str):
        subject_times = {"政治": 0, "英语": 0, "数学": 0, "专业课": 0, "其他": 0}
        total = 0
        try:
            with _db(STUDY_DB_FILE) as conn:
                for sub, m in conn.execute(
                    "SELECT subject, SUM(minutes) FROM focus_records "
                    "WHERE date=? GROUP BY subject", (today,)
                ).fetchall():
                    if sub in subject_times:
                        subject_times[sub] = int(m)
                        total += int(m)
        except Exception as e:
            logger.warning(f"专注统计 {e}")
        return subject_times, total

    def _fetch_news(self):
        out = []
        try:
            with _db(NEWS_DB_FILE) as conn:
                for (title,) in conn.execute(
                    "SELECT title FROM news WHERE title IS NOT NULL AND title != '' "
                    "ORDER BY id DESC LIMIT 2"
                ).fetchall():
                    out.append({"title": title[:22] + ".." if len(title) > 22 else title})
        except Exception as e:
            logger.warning(f"新闻 {e}")
        return out

    def _fetch_fee(self):
        balance, u3d, havg, dleft = 0.0, [0.0, 0.0, 0.0], 0.0, "--"
        try:
            with _db(FEE_DB_FILE) as conn:
                row = conn.execute(
                    "SELECT balance FROM meter_records WHERE user_addr LIKE ? "
                    "ORDER BY id DESC LIMIT 1", (f"%{self.meter_addr}%",)
                ).fetchone()
            if row:
                balance = float(row[0])
            u3d, havg, dleft = self.calc_usage(self.meter_addr)
        except Exception as e:
            logger.warning(f"电费 {e}")
        return balance, u3d, havg, dleft

    def calc_usage(self, addr: str):
        usage_3d = [0.0, 0.0, 0.0]
        hourly_avg = 0.0
        days_left = "--"
        try:
            now = _now()
            four_days_ago = (now - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
            with _db(FEE_DB_FILE) as conn:
                rows = conn.execute(
                    "SELECT record_time, balance FROM meter_records "
                    "WHERE user_addr LIKE ? AND record_time >= ? ORDER BY record_time ASC",
                    (f"%{addr}%", four_days_ago)
                ).fetchall()
            if not rows:
                return usage_3d, hourly_avg, days_left

            daily = {}
            for rt_str, bal in rows:
                daily.setdefault(rt_str.split(" ")[0], []).append(bal)
            sorted_days = sorted(daily.keys(), reverse=True)

            for i in range(3):
                if i >= len(sorted_days):
                    break
                bals = daily[sorted_days[i]]
                if len(bals) < 2:
                    continue
                diff = bals[0] - bals[-1]
                usage_3d[i] = round(diff / PRICE_PER_KWH, 2) if diff > 0 else 0.0
                if i == 0 and usage_3d[0] > 0:
                    start_t = datetime.strptime(
                        sorted_days[0] + " 00:00:00", "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=TZ)
                    hours_span = max(1.0, (now - start_t).total_seconds() / 3600)
                    hourly_avg = round(usage_3d[0] / hours_span, 2)

            valid = [u for u in usage_3d if u > 0]
            if valid:
                avg_daily = sum(valid) / len(valid)
                if avg_daily > 0:
                    current_bal = rows[-1][1]
                    left = max(0.0, current_bal / avg_daily)
                    days_left = "不足1天" if left < 1 else str(int(left))
        except Exception as e:
            logger.warning(f"[电费] 计算失败: {e}")
        return usage_3d, hourly_avg, days_left

    def _fetch_server(self):
        """CPU 用滑动均值，内存/磁盘用实时值（对墨水屏更合适）。"""
        st = {"cpu": 0, "mem": 0, "disk": 0}
        if not psutil:
            return st
        try:
            if self._cpu_samples:
                st["cpu"] = round(sum(self._cpu_samples) / len(self._cpu_samples))
            st["mem"]  = round(psutil.virtual_memory().percent)
            st["disk"] = round(psutil.disk_usage("/").percent)
        except Exception as e:
            logger.warning(f"服务器状态 {e}")
        return st

    async def _fetch_weather(self):
        now = _now()
        if self._weather_cache and self._weather_cache_time \
                and (now - self._weather_cache_time) < WEATHER_TTL:
            return self._weather_cache
        try:
            r = await self.http_client.get("https://wttr.in/烟台莱山?format=j1", timeout=10)
            if r.status_code == 200:
                w = r.json()
                cur = w["current_condition"][0]
                desc_map = {"Sunny": "晴", "Clear": "晴", "Partly cloudy": "多云",
                            "Cloudy": "多云", "Overcast": "阴", "Mist": "薄雾",
                            "Light rain": "小雨"}
                desc = desc_map.get(cur["weatherDesc"][0]["value"].strip(),
                                    cur["weatherDesc"][0]["value"].strip())
                self._weather_cache = (
                    f"{desc} {cur['temp_C']}°C "
                    f"({w['weather'][0]['mintempC']}~{w['weather'][0]['maxtempC']}°C)"
                )
        except Exception as e:
            logger.warning(f"天气 {e}")
        finally:
            self._weather_cache_time = now
        return self._weather_cache or "--"

    async def _fetch_dsh(self):
        now = _now()
        if self._ds_cache_time and (now - self._ds_cache_time) < DSH_TTL:
            return self._ds_cache
        try:
            api_key = self._get_config("deepseek_key", "")
            if api_key:
                r = await self.http_client.get(
                    "https://api.deepseek.com/user/balance",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                    timeout=5,
                )
                if r.status_code == 200:
                    for info in r.json().get("balance_infos", []):
                        if info.get("currency") == "CNY":
                            self._ds_cache = float(info["total_balance"])
                            break
        except Exception as e:
            logger.warning(f"DSH 余额 {e}")
        finally:
            self._ds_cache_time = now
        return self._ds_cache

    # ================= HTTP API =================
    async def handle_record(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"status": "error", "msg": "invalid json"}, status=400)

        subject = data.get("subject", "其他")
        if subject not in SUBJECT_LIST:
            subject = "其他"
        try:
            minutes = int(data.get("minutes", 0))
        except (TypeError, ValueError):
            return web.json_response({"status": "error", "msg": "minutes 非法"}, status=400)
        if not (0 < minutes <= 24 * 60):
            return web.json_response({"status": "error", "msg": "minutes 超出范围"}, status=400)

        try:
            today = _now().strftime("%Y-%m-%d")
            with _db(STUDY_DB_FILE) as conn:
                conn.execute(
                    "INSERT INTO focus_records (date, subject, minutes) VALUES (?, ?, ?)",
                    (today, subject, minutes),
                )
            return web.json_response({"status": "success"})
        except Exception as e:
            logger.error(f"打卡记录失败: {e}")
            return web.json_response({"status": "error", "msg": str(e)}, status=500)

    async def handle_all(self, request: web.Request):
        now = _now()
        today = now.strftime("%Y-%m-%d")

        (weather, fee, news, focus, memos, server, dsh, exams) = await asyncio.gather(
            self._fetch_weather(),
            asyncio.to_thread(self._fetch_fee),
            asyncio.to_thread(self._fetch_news),
            asyncio.to_thread(self._fetch_focus, today),
            asyncio.to_thread(self._fetch_memos),
            asyncio.to_thread(self._fetch_server),
            self._fetch_dsh(),
            asyncio.to_thread(self._get_all_exams),
        )
        balance, usage_3d, hourly_avg, days_left = fee
        subject_times, total_minutes = focus

        current_exam = {"name": "", "days": 0}
        exam_list = []
        for e in exams:
            exam_list.append({"name": e["name"], "days": e["days"]})
            if e["name"] == self.active_exam_name:
                current_exam = {"name": e["name"], "days": e["days"]}

        data = {
            "status": "success",
            "weather": weather,
            "exam_list": exam_list,
            "current_exam": current_exam,
            "current_subject": self.current_subject,
            "week_num": self.week_num,
            "week_day": WEEK_CN[now.weekday()],

            "balance": balance,
            "usage_3d": usage_3d,
            "hourly_avg": hourly_avg,
            "days_left": days_left,

            "news_list": news,
            "subject_times": subject_times,
            "memos": memos,
            "server_status": server,
            "deepseek_balance": dsh,
            "total_minutes": total_minutes,
        }
        return web.json_response(data)

    # ---------- web 服务 ----------
    async def start_web_server(self):
        @web.middleware
        async def cors_middleware(request, handler):
            if request.method == "OPTIONS":
                resp = web.Response()
            else:
                try:
                    resp = await handler(request)
                except web.HTTPException as e:
                    resp = e
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return resp

        app = web.Application(middlewares=[cors_middleware])
        app.router.add_get("/api/all", self.handle_all)
        app.router.add_post("/api/record", self.handle_record)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", WEB_PORT)
        await site.start()
        logger.info(f"📺 墨水屏 API 已启动: http://0.0.0.0:{WEB_PORT}")
