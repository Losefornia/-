# spider.py
import asyncio
import re
import time
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from .utils import match_key
from . import db

URL = "https://ydny.ytu.edu.cn/pms/wechat/ytdx/payItemList"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": URL,
}
REQUEST_TIMEOUT = 30

MAX_BUFFER_SIZE = 1 * 1024 * 1024
PANEL_SLEEP = 0.05
BATCH_SIZE = 100
BATCH_SLEEP = 0.2

_client = None


def get_client():
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers=HEADERS,
            limits=httpx.Limits(
                max_connections=5,
                max_keepalive_connections=3,
                keepalive_expiry=60,
            ),
        )
    return _client


async def close_client():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


def parse_one_panel(panel_html: str):
    soup = BeautifulSoup(panel_html, "html.parser")
    panel = soup.find("div", class_="weui-panel")
    if not panel:
        return None

    user_no, user_name, user_addr, balance = "未知", "未知", "未知", 0.0
    for cell in panel.find_all("div", class_="weui-cell"):
        bd = cell.find("div", class_="weui-cell__bd")
        ft = cell.find("div", class_="weui-cell__ft")
        if bd and ft:
            title = bd.get_text(strip=True)
            val = ft.get_text(strip=True)
            if "用户编号" in title:
                user_no = val
            elif "用户姓名" in title:
                user_name = val
            elif "用户地址" in title:
                user_addr = val
            elif "实时余额" in title:
                try:
                    balance = float(val)
                except (ValueError, TypeError):
                    balance = 0.0

    if user_addr == "未知":
        return None

    return {
        "user_no": user_no,
        "user_name": user_name,
        "user_addr": user_addr,
        "balance": balance,
    }


async def stream_and_parse(client, target_addrs):
    target_map = {}
    for t in target_addrs:
        k = match_key(t)
        if k:
            target_map[k] = t
    if not target_map:
        return []

    records = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    buffer = ""
    panel_pattern = re.compile(
        r'<div class="weui-panel".*?</div>\s*(?=<div class="weui-panel"|</div>\s*<div class="weui-cells")',
        re.DOTALL,
    )

    async with client.stream("GET", URL) as resp:
        if resp.status_code != 200:
            print(f"请求失败: {resp.status_code}")
            return records
        resp.encoding = "utf-8"

        total_panels = 0
        async for chunk in resp.aiter_text():
            buffer += chunk

            while True:
                match = panel_pattern.search(buffer)
                if not match:
                    break
                panel_html = match.group(0)
                buffer = buffer[match.end():]
                total_panels += 1

                item = parse_one_panel(panel_html)
                if not item:
                    continue

                key = match_key(item["user_addr"])
                if key in target_map:
                    matched = target_map[key]
                    records.append((
                        now_str,
                        item["user_no"],
                        item["user_name"],
                        matched,
                        item["balance"],
                    ))

                if total_panels % 10 == 0:
                    await asyncio.sleep(PANEL_SLEEP)

            # 命中全部目标 → 提前结束，省网络 IO
            if len(records) >= len(target_map):
                break

            if len(buffer) > MAX_BUFFER_SIZE:
                buffer = buffer[-512 * 1024:]

    print(f"📄 解析 {total_panels} 个 panel，命中 {len(records)} 条")
    return records


def _save_all(records):
    """单次 to_thread 内跑完所有批次，减少线程切换。"""
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        db.save_records(batch)
        time.sleep(BATCH_SLEEP)


async def fetch_specific(addrs):
    if not addrs:
        return
    client = get_client()
    records = await stream_and_parse(client, addrs)
    if records:
        await asyncio.to_thread(_save_all, records)
        print(f"✅ 入库 {len(records)} 条")


def _should_clean():
    """检查是否到了清理周期（持久化，重启不重置）。"""
    last = db.get_meta("last_clean_date")
    today = datetime.now().date()

    if last is not None:
        try:
            last_date = datetime.strptime(last, "%Y-%m-%d").date()
            if (today - last_date).days < db.CLEAN_DAYS:
                return False
        except (ValueError, TypeError):
            pass

    # 只在凌晨 3:00 后清，避开用户活跃时段
    if datetime.now().hour < 3:
        return False

    return True


async def fetch_all_bound():
    addrs = db.get_bound_addrs()
    if not addrs:
        return
    await fetch_specific(addrs)

    # 清理：每 CLEAN_DAYS 天一次，持久化记录
    if not _should_clean():
        return

    try:
        deleted = await asyncio.to_thread(db.maintenance)
        db.set_meta("last_clean_date", datetime.now().strftime("%Y-%m-%d"))
        if deleted:
            print(f"🧹 清理 {deleted} 条")
    except Exception as e:
        print(f"⚠️ 清理失败: {e}")
