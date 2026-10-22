# main.py
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import re
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

from . import db
from . import spider
from .utils import (
    convert_dorm_format, draw_bar_chart_text,
    PRICE_PER_KWH, calc_14day_usage,
)

ALERT_THRESHOLD = 5
WARN_THRESHOLD = 15
FETCH_INTERVAL_MIN = 60

# ===== 允许使用的群聊白名单 =====
ALLOWED_GROUPS = [
    "default_1905605993:GroupMessage:8136D3D9580E7AD9FB0B516C4E5FDDF1",
    "default_1905605993:GroupMessage:E13B720565366E510BCBCA5E16BA84E0",
]


def is_allowed(event: AstrMessageEvent) -> bool:
    if not ALLOWED_GROUPS:
        return True
    return event.unified_msg_origin in ALLOWED_GROUPS


@register("astrbot_plugin_dianfei", "你的名字", "电费查询插件", "1.0.0", "")
class DianFeiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        db.init_tables()
        self._fetch_task = None

    async def initialize(self):
        logger.info("✅ 电管Doro 插件已加载")
        self._fetch_task = asyncio.create_task(self._fetch_loop())
        self._fetch_task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"抓取任务异常退出: {exc}", exc_info=exc)

    async def _fetch_loop(self):
        await asyncio.sleep(60)
        await self._safe_fetch()
        backoff = FETCH_INTERVAL_MIN * 60
        while True:
            await asyncio.sleep(backoff)
            ok = await self._safe_fetch()
            if ok:
                backoff = FETCH_INTERVAL_MIN * 60
            else:
                backoff = min(backoff * 2, 3600)

    async def _safe_fetch(self):
        try:
            await spider.fetch_all_bound()
            await self._check_alerts()
            return True
        except Exception as e:
            logger.warning(f"抓取异常: {e}")
            return False

    async def _check_alerts(self):
        addrs = db.get_bound_addrs()
        for addr in addrs:
            result = db.get_user_balance(addr)
            if not result:
                continue

            balance = result["balance"]

            if balance >= ALERT_THRESHOLD:
                if db.is_ignored(addr):
                    db.remove_ignore(addr)
                    logger.info(f"✅ {addr} 余额回升，自动解除忽略")
                continue

            if db.is_ignored(addr):
                continue

            users = db.get_bind_umos_and_openids(addr)
            if not users:
                continue

            text = (
                f"\n⚠️ 余额不足提醒\n"
                f"🏠 宿舍：{addr}\n"
                f"💰 余额：{balance} 元\n"
                f"💡 低于 {ALERT_THRESHOLD} 元，请及时充值！\n"
                f"📖 回复「忽略」可屏蔽提醒"
            )

            async def _send(umo, openid, _text=text):
                if ALLOWED_GROUPS and umo not in ALLOWED_GROUPS:
                    return
                try:
                    at_tag = f'<qqbot-at-user id="{openid}" />'
                    chain = MessageChain(chain=[Plain(f"{at_tag}\n{_text}")])
                    await self.context.send_message(umo, chain)
                    logger.debug(f"🔔 已推送预警：{addr} → {openid}")
                except Exception as e:
                    logger.warning(f"⚠️ 预警推送失败 {openid}: {e}")

            await asyncio.gather(*[_send(u, o) for u, o in users])

    # ===== 绑定 =====
    @filter.command("绑定")
    async def bind(self, event: AstrMessageEvent):
        if not is_allowed(event):
            return
        openid = event.get_sender_id()
        umo = event.unified_msg_origin
        parts = event.message_str.split()

        def at_reply(text):
            at_tag = f'<qqbot-at-user id="{openid}" />'
            return event.chain_result([Plain(f"{at_tag}\n{text}")])

        if len(parts) < 2:
            yield at_reply("\n📖 格式：绑定 宿舍号\n示例：绑定 NS07N0488")
            return

        addr_raw = parts[1].upper()
        if not re.match(r'^(NS|BS)\d{2}[A-Z]\d{4}$', addr_raw):
            yield at_reply(
                f"\n❌ 宿舍号格式错误：{addr_raw}\n"
                "💡 正确格式：NS07N0488（9位）\n"
                "💡 NS=南校，BS=北校"
            )
            return

        if db.get_bind_addr(openid):
            yield at_reply("\n❌ 你已经绑定过了，先发「解绑」")
            return

        db_addr = convert_dorm_format(addr_raw)
        db.bind_user(openid, db_addr, umo)
        yield at_reply(f"\n✅ 绑定成功\n🏠 宿舍：{db_addr}\n💡 发送「查」查询电量")

        try:
            await spider.fetch_specific([db_addr])
        except Exception as e:
            logger.warning(f"绑定后抓取失败: {e}")

    # ===== 解绑 =====
    @filter.command("解绑")
    async def unbind(self, event: AstrMessageEvent):
        if not is_allowed(event):
            return
        openid = event.get_sender_id()
        addr = db.get_bind_addr(openid)

        def at_reply(text):
            at_tag = f'<qqbot-at-user id="{openid}" />'
            return event.chain_result([Plain(f"{at_tag}\n{text}")])

        if not addr:
            yield at_reply("\n❌ 你还没有绑定宿舍")
            return
        db.unbind_user(openid)
        yield at_reply(f"\n✅ 已解绑宿舍 {addr}")

    # ===== 忽略 =====
    @filter.command("忽略")
    async def ignore(self, event: AstrMessageEvent):
        if not is_allowed(event):
            return
        openid = event.get_sender_id()
        addr = db.get_bind_addr(openid)

        def at_reply(text):
            at_tag = f'<qqbot-at-user id="{openid}" />'
            return event.chain_result([Plain(f"{at_tag}\n{text}")])

        if not addr:
            yield at_reply("\n❌ 你还没有绑定宿舍")
            return

        db.set_ignore(addr)
        yield at_reply(
            f"\n✅ 已屏蔽宿舍 {addr} 的低余额提醒\n"
            f"💡 当余额回升至 {ALERT_THRESHOLD} 元以上时自动恢复"
        )

    # ===== 查询 =====
    @filter.command("查")
    async def query(self, event: AstrMessageEvent):
        if not is_allowed(event):
            return
        openid = event.get_sender_id()
        addr = db.get_bind_addr(openid)

        def at_reply(text):
            at_tag = f'<qqbot-at-user id="{openid}" />'
            return event.chain_result([Plain(f"{at_tag}\n{text}")])

        if not addr:
            yield at_reply("\n❌ 你还没有绑定宿舍，请发送「绑定 宿舍号」")
            return

        result = db.get_user_balance(addr)
        if not result:
            yield at_reply(f"\n❌ 暂无数据，请等待爬虫采集\n🏠 宿舍：{addr}")
            return

        name = result["user_name"]
        balance = result["balance"]
        record_time = result["record_time"]
        display_name = name[0] + "**" if name and len(name) >= 2 else name or addr

        now = datetime.now()
        usage_map = calc_14day_usage(addr, now)
        today_usage = usage_map.get(0)
        yesterday_usage = usage_map.get(1)

        days_detail = []
        valid_usages = []
        for i in range(14):
            d = now - timedelta(days=i)
            usage = usage_map.get(i)
            label = "今天" if i == 0 else f"{d.month}月{d.day}日"
            if usage is None:
                days_detail.append(f"{label} 无数据")
            else:
                bar = "█" * max(1, min(int(usage * 0.8), 8))
                cost = round(usage * PRICE_PER_KWH, 2)
                days_detail.append(f"{label} {bar} {usage:.3f}度（{cost}元）")
                valid_usages.append(usage)

        if valid_usages:
            avg_usage = round(sum(valid_usages) / len(valid_usages), 2)
            avg_cost = round(avg_usage * PRICE_PER_KWH, 2)
            days_left = int(balance / (avg_usage * PRICE_PER_KWH)) if avg_usage > 0 else None
            days_left_text = f"约 {days_left} 天" if days_left and days_left > 0 else "不足1天"
        else:
            avg_usage = 0.0
            avg_cost = 0.0
            days_left_text = "--"

        status = "🔴 欠费" if balance < 0 else "🟠 预警" if balance < WARN_THRESHOLD else "🟢 正常"
        warn = f"\n⚠️ 余额不足{ALERT_THRESHOLD}元，请及时充值！" if balance < ALERT_THRESHOLD else ""

        detail_text = "\n".join(days_detail)
        today_text = f"{today_usage:.3f}" if today_usage is not None else "--"
        yesterday_text = f"{yesterday_usage:.3f}" if yesterday_usage is not None else "--"

        yield at_reply(
            f"\n🏠 {addr} {display_name}\n"
            f"💰 余额：{balance} 元\n"
            f"📊 状态：{status}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⚡ 今日用电：{today_text} 度\n"
            f"⚡ 昨日用电：{yesterday_text} 度\n"
            f"📊 近14天日均：{avg_usage:.3f} 度/天\n"
            f"💰 日均电费：{avg_cost} 元/天\n"
            f"📅 预计可用：{days_left_text}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📊 近14天每日用电（10:00-次日10:00）：\n{detail_text}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🕐 更新时间：{record_time}{warn}"
        )

    # ===== 详情（文字小时图） =====
    @filter.command("详情")
    async def detail(self, event: AstrMessageEvent):
        if not is_allowed(event):
            return
        openid = event.get_sender_id()
        addr = db.get_bind_addr(openid)

        def at_reply(text):
            at_tag = f'<qqbot-at-user id="{openid}" />'
            return event.chain_result([Plain(f"{at_tag}\n{text}")])

        if not addr:
            yield at_reply("\n❌ 你还没有绑定宿舍")
            return

        result = db.get_user_balance(addr)
        if not result:
            yield at_reply("\n❌ 暂无数据")
            return

        hourly_data = db.get_hourly_usage(addr, 24)
        chart = draw_bar_chart_text(hourly_data, 12)

        yield at_reply(
            f"\n⚡ {addr} 小时详情\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{chart}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🕐 更新：{result['record_time']}\n"
            f"📌 小时用电受学校网站数据更新延迟影响，仅供参考\n"
            f"📌 建议以「查」里的 14 天日用电为准"
        )

    # ===== 电费图（matplotlib 图片） =====
    @filter.command("电费图")
    async def cmd_electric_img(self, event: AstrMessageEvent):
        if not is_allowed(event):
            return
        openid = event.get_sender_id()
        addr = db.get_bind_addr(openid)

        def at_reply(text):
            at_tag = f'<qqbot-at-user id="{openid}" />'
            return event.chain_result([Plain(f"{at_tag}\n{text}")])

        if not addr:
            yield at_reply("\n❌ 你还没有绑定宿舍，请发送「绑定 宿舍号」")
            return

        usage_map = calc_14day_usage(addr)
        if not usage_map:
            yield at_reply("\n❌ 14天用电数据不足，无法绘图")
            return

        try:
            from .utils import draw_bar_chart_img
            img_buf = draw_bar_chart_img(usage_map, addr)
        except ImportError as e:
            logger.error(f"[电费图] 缺少 matplotlib: {e}")
            yield at_reply(f"\n❌ 缺少 matplotlib：{e}\n请执行 pip install matplotlib")
            return
        except Exception as e:
            logger.error(f"[电费图] 绘图异常: {e}", exc_info=e)
            yield at_reply(f"\n❌ 绘图失败：{e}")
            return

        if not img_buf:
            yield at_reply("\n❌ 绘图失败")
            return

        # 存成文件再发（image_result 需要路径字符串）
        try:
            tmp_dir = Path(tempfile.gettempdir()) / "dianfei_charts"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            img_path = tmp_dir / f"{openid}.png"
            img_path.write_bytes(img_buf.getvalue())
        except Exception as e:
            logger.error(f"[电费图] 保存图片失败: {e}", exc_info=e)
            yield at_reply(f"\n❌ 保存图片失败：{e}")
            return

        at_tag = f'<qqbot-at-user id="{openid}" />'
        yield event.chain_result([
            Plain(f"{at_tag}\n📊 {convert_dorm_format(addr)} 近14天用电量"),
        ])
        yield event.image_result(str(img_path))

    # ===== 帮助 =====
    @filter.command("帮助")
    async def help(self, event: AstrMessageEvent):
        if not is_allowed(event):
            return
        yield event.plain_result(
            "📖 使用指南\n"
            "━━━━━━━━━━━━━━━━\n"
            "📝 绑定：/绑定 NS07N0488\n"
            "⚡ 查询：/查（近14天日用电）\n"
            "📊 详情：/详情（小时图，仅供参考）\n"
            "📈 电费图：/电费图（近14天彩色柱状图）\n"
            "🔕 忽略：/忽略（屏蔽低余额提醒）\n"
            "🔓 解绑：/解绑\n"
            "━━━━━━━━━━━━━━━━\n"
            "💡 NS=南校，BS=北校\n"
            "💡 日用电按 10:00 - 次日 10:00 统计"
        )

    async def terminate(self):
        if self._fetch_task:
            self._fetch_task.cancel()
            try:
                await self._fetch_task
            except asyncio.CancelledError:
                pass
        await spider.close_client()
        logger.info("👋 电管Doro 插件已卸载")
