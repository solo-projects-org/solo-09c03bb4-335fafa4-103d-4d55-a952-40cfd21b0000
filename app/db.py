"""SQLite 存取层。

设计要点：
- 报价明细（prices）与指数结果（index_months）分表存放。
- 每个报价批次（upload）保留两条月份信息：`month` 是原报月（CSV 里填写、
  永不改动），`effective_month` 是生效月（这批报价当前计入的月份）。
  月份对调只改 `effective_month`，原始上传记录因此完整保留。
- 同一生效月只保留最新批次的报价，且指数结果表保存了算该结果时所用的
  报价版本（source_upload_id），因此「查指数」只读结果表，不碰报价明细。
- 任何一次整月替换都在单个事务内完成：先写入新报价、再重算受影响的
  指数、最后才提交。任何一步失败回滚，旧报价与旧结果都保留。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    month           TEXT    NOT NULL,        -- 原报月：CSV 里填写的月份
    effective_month TEXT    NOT NULL DEFAULT '',  -- 生效月：该批报价当前所属月份
    filename        TEXT    NOT NULL,
    row_count       INTEGER NOT NULL,
    uploaded_at     TEXT    NOT NULL,        -- UTC ISO8601
    is_base_month   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_uploads_month ON uploads(month, id);

-- 当前生效的报价：同月同摊同品种唯一。
CREATE TABLE IF NOT EXISTS prices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id  INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    month      TEXT    NOT NULL,
    stall      TEXT    NOT NULL,
    item       TEXT    NOT NULL,
    price      REAL    NOT NULL CHECK (price > 0),
    UNIQUE (month, stall, item)
);

CREATE INDEX IF NOT EXISTS idx_prices_month ON prices(month);
CREATE INDEX IF NOT EXISTS idx_prices_key ON prices(month, stall, item);

-- 指数结果缓存。查询指数只读此表。
CREATE TABLE IF NOT EXISTS index_months (
    month              TEXT PRIMARY KEY,
    is_base_month      INTEGER NOT NULL DEFAULT 0,
    has_upload         INTEGER NOT NULL DEFAULT 0,  -- 该月是否有已提交报价
    pair_count         INTEGER,              -- NULL = 断链或无报价
    mom_factor         REAL,                 -- 环比系数
    index_value        REAL,                 -- NULL = 留空
    source_upload_id   INTEGER REFERENCES uploads(id) ON DELETE SET NULL,
    computed_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(database: str | Path) -> sqlite3.Connection:
    database = str(database)
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(database: str | Path) -> None:
    conn = connect(database)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """把旧库补到当前结构：补齐 uploads.effective_month 并回填。

    旧库里没有「生效月」概念，批次月份就是生效月；回填一次即可。
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(uploads)")}
    with transaction(conn):
        if "effective_month" not in columns:
            conn.execute(
                "ALTER TABLE uploads ADD COLUMN effective_month TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "UPDATE uploads SET effective_month = month WHERE effective_month = '' "
            "OR effective_month IS NULL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_uploads_effective ON uploads(effective_month, id)"
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务；异常时回滚，正常时提交。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def all_months(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM index_months ORDER BY month").fetchall()


def max_effective_month(conn: sqlite3.Connection) -> str | None:
    """当前有报价明细的最大生效月（历史批次不算）。"""
    months = list(current_batches(conn))
    return months[-1] if months else None


def previous_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    if m == 1:
        return f"{y - 1:04d}-12"
    return f"{y:04d}-{m - 1:02d}"


def next_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


def month_range(start: str, end: str) -> list[str]:
    """闭区间 [start, end] 的月份序列。"""
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = next_month(cur)
    return out


def month_prices(conn: sqlite3.Connection, month: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        "SELECT stall, item, price FROM prices WHERE month = ?", (month,)
    ).fetchall()
    return {(r["stall"], r["item"]): r["price"] for r in rows}


def upsert_index_rows(
    conn: sqlite3.Connection, rows: Sequence[dict]
) -> None:
    conn.executemany(
        """
        INSERT INTO index_months
            (month, is_base_month, has_upload, pair_count, mom_factor, index_value,
             source_upload_id, computed_at)
        VALUES (:month, :is_base_month, :has_upload, :pair_count, :mom_factor,
                :index_value, :source_upload_id, :computed_at)
        ON CONFLICT(month) DO UPDATE SET
            is_base_month    = excluded.is_base_month,
            has_upload       = excluded.has_upload,
            pair_count       = excluded.pair_count,
            mom_factor       = excluded.mom_factor,
            index_value      = excluded.index_value,
            source_upload_id = excluded.source_upload_id,
            computed_at      = excluded.computed_at
        """,
        list(rows),
    )


def recent_uploads(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT u.*, "
        "  (SELECT COUNT(*) FROM prices p WHERE p.upload_id = u.id) AS price_count "
        "FROM uploads u ORDER BY u.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def current_batches(conn: sqlite3.Connection) -> dict[str, dict]:
    """每个生效月当前使用的批次（以实际仍有报价明细的最新上传为准）。

    历史批次（被替换或对调后已无报价）仍保留在 uploads 表里，但不算「现行」。
    返回 {生效月: 批次字典}，按生效月升序。
    """
    rows = conn.execute(
        "SELECT u.id, u.month, u.effective_month, u.filename, u.row_count, "
        "       u.uploaded_at, "
        "       (SELECT COUNT(*) FROM prices p WHERE p.upload_id = u.id) AS price_count "
        "FROM uploads u ORDER BY u.id"
    ).fetchall()

    latest: dict[str, dict] = {}
    for row in rows:
        data = dict(row)
        month = data["effective_month"]
        if not month or data["price_count"] <= 0:
            continue
        current = latest.get(month)
        if current is None or data["id"] > current["id"]:
            latest[month] = data
    return {month: latest[month] for month in sorted(latest)}
