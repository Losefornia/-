# db.py
import sqlite3
import os
import threading
from pathlib import Path
from datetime import datetime, timedelta

try:
    from astrbot.api.star import StarTools
    DATA_DIR = StarTools.get_data_dir() / "astrbot_plugin_dianfei"
except Exception:
    DATA_DIR = Path("data/plugin_data/astrbot_plugin_dianfei")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "meters_data.db"

KEEP_DAYS = 35       # 保留 35 天（14天查询 + 10:00 日界余量）
CLEAN_DAYS = 7        # 每 7 天清一次

_local = threading.local()


def get_db():
    """线程本地常驻连接，只建一次。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=20)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA cache_size=-8000;")      # 8MB
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def close_db():
    """线程退出时调用，关闭当前线程的连接。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


def init_tables():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meter_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_time TEXT,
            user_no TEXT,
            user_name TEXT,
            user_addr TEXT,
            balance REAL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_record
        ON meter_records (record_time, user_addr)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_addr_time
        ON meter_records (user_addr, record_time)
    """)
    # 清理专用：纯时间索引
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_time
        ON meter_records (record_time)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_bind (
            openid TEXT PRIMARY KEY,
            user_addr TEXT NOT NULL,
            bind_time TEXT,
            umo TEXT
        )
    """)
    # 清理 unbound 时的 join 加速
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bind_addr
        ON user_bind (user_addr)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_ignore (
            user_addr TEXT PRIMARY KEY,
            ignore_since TEXT
        )
    """)
    # 元数据表：持久化清理日期等
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE user_bind ADD COLUMN umo TEXT")
    except Exception:
        pass
    conn.commit()


def query_one(sql, params=None):
    return get_db().execute(sql, params or ()).fetchone()


def query_all(sql, params=None):
    return get_db().execute(sql, params or ()).fetchall()


def execute(sql, params=None):
    conn = get_db()
    conn.execute(sql, params or ())
    conn.commit()


# ===== 元数据 =====

def get_meta(key):
    row = query_one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else None


def set_meta(key, value):
    execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# ===== 绑定相关 =====

def get_bind_addr(openid):
    row = query_one("SELECT user_addr FROM user_bind WHERE openid=?", (openid,))
    return row["user_addr"] if row else None


def get_bind_users(addr):
    rows = query_all("SELECT openid FROM user_bind WHERE user_addr=?", (addr,))
    return [r["openid"] for r in rows]


def get_bind_umos_and_openids(addr):
    rows = query_all(
        "SELECT umo, openid FROM user_bind WHERE user_addr=? AND umo IS NOT NULL",
        (addr,),
    )
    return [(r["umo"], r["openid"]) for r in rows]


def bind_user(openid, addr, umo=None):
    execute(
        "INSERT OR REPLACE INTO user_bind (openid, user_addr, bind_time, umo) VALUES (?, ?, ?, ?)",
        (openid, addr, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), umo),
    )


def unbind_user(openid):
    execute("DELETE FROM user_bind WHERE openid=?", (openid,))


# ===== 余额 / 历史 =====

def get_user_balance(addr):
    return query_one(
        "SELECT user_name, balance, record_time FROM meter_records "
        "WHERE user_addr=? ORDER BY id DESC LIMIT 1",
        (addr,),
    )


def get_balance_history(addr, hours=24):
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return query_all(
        "SELECT record_time, balance FROM meter_records "
        "WHERE user_addr=? AND record_time>=? ORDER BY record_time ASC",
        (addr, cutoff),
    )


def get_hourly_usage(addr, hours=24):
    from .utils import PRICE_PER_KWH
    rows = get_balance_history(addr, hours)
    if len(rows) < 2:
        return {}

    hourly_data = {}
    for i in range(1, len(rows)):
        prev_time = datetime.strptime(rows[i - 1]["record_time"], "%Y-%m-%d %H:%M:%S")
        curr_time = datetime.strptime(rows[i]["record_time"], "%Y-%m-%d %H:%M:%S")
        prev_balance = rows[i - 1]["balance"]
        curr_balance = rows[i]["balance"]

        diff = prev_balance - curr_balance
        if diff <= 0:
            continue

        # 间隔 >2 小时视为数据断层，不摊到小时图，避免虚高
        hours_span = (curr_time - prev_time).total_seconds() / 3600
        if hours_span > 2:
            continue

        key = prev_time.strftime("%m-%d %H:00")
        hourly_data[key] = hourly_data.get(key, 0) + (diff / PRICE_PER_KWH)

    return hourly_data


# ===== 批量写 =====

def save_records(records):
    """批量写入，显式事务，同时间同地址自动去重。"""
    if not records:
        return
    conn = get_db()
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT OR IGNORE INTO meter_records "
        "(record_time, user_no, user_name, user_addr, balance) "
        "VALUES (?, ?, ?, ?, ?)",
        records,
    )
    conn.execute("COMMIT")


def get_bound_addrs():
    rows = query_all("SELECT DISTINCT user_addr FROM user_bind")
    return [r["user_addr"] for r in rows]


# ===== 清理（低频，独立协程调用） =====

def clean_old_data():
    """按 10:00 日界对齐，删除 KEEP_DAYS 天前的数据。"""
    from .utils import get_today_start
    cutoff = (get_today_start() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cursor = conn.execute("DELETE FROM meter_records WHERE record_time < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    return deleted


def maybe_checkpoint():
    """回收 WAL，只在清理后调用。"""
    conn = get_db()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception:
        pass


def maintenance():
    """一次 to_thread 内跑完清理 + checkpoint。"""
    deleted = clean_old_data()
    maybe_checkpoint()
    return deleted


# ===== 忽略预警 =====

def set_ignore(addr):
    execute(
        "INSERT OR REPLACE INTO alert_ignore (user_addr, ignore_since) VALUES (?, ?)",
        (addr, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )


def remove_ignore(addr):
    execute("DELETE FROM alert_ignore WHERE user_addr=?", (addr,))


def is_ignored(addr):
    row = query_one("SELECT 1 FROM alert_ignore WHERE user_addr=?", (addr,))
    return row is not None
