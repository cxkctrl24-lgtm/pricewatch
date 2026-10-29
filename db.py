# -*- coding: utf-8 -*-
"""SQLite 存储：快照 / 商品 / 价格历史 / 日志。"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

_LOCK = threading.RLock()
_CONN = None


def _conn():
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA synchronous=NORMAL")
    return _CONN


@contextmanager
def transaction():
    with _LOCK:
        c = _conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    keyword TEXT NOT NULL,
    item_key TEXT NOT NULL,
    title TEXT,
    price REAL NOT NULL,
    shop TEXT,
    url TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    last_price REAL NOT NULL,
    min_price REAL NOT NULL,
    min_price_at REAL,
    spec_key TEXT DEFAULT '',
    condition TEXT DEFAULT 'new',           -- new / used
    price_level TEXT DEFAULT 'ok',          -- ok / review
    price_note TEXT DEFAULT '',
    UNIQUE(platform, item_key)
);
CREATE INDEX IF NOT EXISTS idx_products_platform ON products(platform, keyword);
CREATE INDEX IF NOT EXISTS idx_products_price ON products(price);
CREATE INDEX IF NOT EXISTS idx_products_spec ON products(spec_key);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    price REAL NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_product ON price_history(product_id, ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    keyword TEXT,
    status TEXT NOT NULL,
    message TEXT,
    item_count INTEGER DEFAULT 0,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    keyword TEXT,
    title TEXT,
    price REAL,
    shop TEXT,
    url TEXT,
    reason TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejected_ts ON rejected(ts);

CREATE TABLE IF NOT EXISTS custom_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    keyword TEXT NOT NULL UNIQUE,
    enabled INTEGER DEFAULT 1
);
"""


def init():
    with transaction() as c:
        c.executescript(SCHEMA)
    # 第一次启动：把内置关键词写进可编辑表
    with transaction() as c:
        n = c.execute("SELECT COUNT(*) FROM custom_keywords").fetchone()[0]
        if n == 0:
            for g in config.KEYWORD_GROUPS:
                for kw in g["keywords"]:
                    c.execute(
                        "INSERT OR IGNORE INTO custom_keywords(group_id, keyword, enabled) VALUES(?,?,1)",
                        (g["id"], kw),
                    )


# ---------------- 关键词 ----------------
def keyword_groups():
    """合并内置组信息 + 数据库里可编辑的关键词。"""
    with transaction() as c:
        rows = c.execute("SELECT group_id, keyword, enabled FROM custom_keywords ORDER BY id").fetchall()
    by_group = {}
    for r in rows:
        by_group.setdefault(r["group_id"], []).append({"keyword": r["keyword"], "enabled": bool(r["enabled"])})
    out = []
    for g in config.KEYWORD_GROUPS:
        kws = by_group.get(g["id"], [])
        out.append({
            "id": g["id"],
            "name": g["name"],
            "price": g["price"],
            "keywords": kws,
            "enabled_keywords": [k["keyword"] for k in kws if k["enabled"]],
        })
    return out


def add_keyword(group_id, keyword):
    keyword = keyword.strip()
    if not keyword:
        return False, "关键词为空"
    with transaction() as c:
        try:
            c.execute("INSERT INTO custom_keywords(group_id, keyword, enabled) VALUES(?,?,1)", (group_id, keyword))
        except sqlite3.IntegrityError:
            return False, "这个词已经存在了"
    return True, "已添加"


def toggle_keyword(keyword, enabled):
    with transaction() as c:
        c.execute("UPDATE custom_keywords SET enabled=? WHERE keyword=?", (1 if enabled else 0, keyword))
    return True, "已更新"


def delete_keyword(keyword):
    with transaction() as c:
        c.execute("DELETE FROM custom_keywords WHERE keyword=?", (keyword,))
    return True, "已删除"


# ---------------- 写入采集结果 ----------------
def record_items(task_id, platform, keyword, items, min_ratio=0.01, min_interval_h=20.0):
    """写入一批商品，返回新增条数。同价（涨跌<1%）且20小时内不重复记历史。"""
    now = time.time()
    new_count = 0
    with transaction() as c:
        for it in items:
            key = it["item_key"]
            row = c.execute(
                "SELECT id, last_price, min_price, last_seen FROM products WHERE platform=? AND item_key=?",
                (platform, key),
            ).fetchone()
            if row:
                pid = row["id"]
                last_price = float(row["last_price"] or it["price"])
                min_price = float(row["min_price"] or it["price"])
                changed = abs(it["price"] - last_price) / max(last_price, 0.01) >= min_ratio
                stale = now - float(row["last_seen"] or 0) >= min_interval_h * 3600
                c.execute(
                    "UPDATE products SET title=?, price=?, shop=?, url=?, keyword=?, last_seen=?, "
                    "last_price=?, spec_key=?, condition=?, price_level=?, price_note=? WHERE id=?",
                    (it["title"], it["price"], it.get("shop") or "", it["url"], keyword,
                     now, it["price"], it.get("spec_key") or "",
                     it.get("condition") or "new", it.get("price_level") or "ok",
                     it.get("price_note") or "", pid),
                )
                if it["price"] < min_price - 0.001:
                    c.execute("UPDATE products SET min_price=?, min_price_at=? WHERE id=?", (it["price"], now, pid))
                    min_price = it["price"]
                if changed or stale:
                    c.execute("INSERT INTO price_history(product_id, price, ts) VALUES(?,?,?)", (pid, it["price"], now))
            else:
                cur = c.execute(
                    """INSERT INTO products
                       (platform, keyword, item_key, title, price, shop, url,
                        first_seen, last_seen, last_price, min_price, min_price_at, spec_key,
                        condition, price_level, price_note)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (platform, keyword, key, it["title"], it["price"], it.get("shop") or "",
                     it["url"], now, now, it["price"], it["price"], now,
                     it.get("spec_key") or "", it.get("condition") or "new",
                     it.get("price_level") or "ok", it.get("price_note") or ""),
                )
                pid = cur.lastrowid
                c.execute("INSERT INTO price_history(product_id, price, ts) VALUES(?,?,?)", (pid, it["price"], now))
                new_count += 1
    return new_count


def record_rejected(platform, keyword, item, reason):
    """可疑商品不入库，只留档，方便回看和调规则。"""
    with transaction() as c:
        c.execute(
            """INSERT INTO rejected(platform, keyword, title, price, shop, url, reason, ts)
               VALUES(?,?,?,?,?,?,?,?)""",
            (platform, keyword, item.get("title") or "", item.get("price") or 0,
             item.get("shop") or "", item.get("url") or "", reason or "", time.time()),
        )


def recent_rejected(limit=200):
    with transaction() as c:
        rows = c.execute("SELECT * FROM rejected ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def review_items(limit=500):
    """价格异常的条目，单独列出。"""
    with transaction() as c:
        rows = c.execute(
            "SELECT * FROM products WHERE price_level='review' ORDER BY price DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def record_snapshot(task_id, platform, keyword, status, message, item_count):
    with transaction() as c:
        c.execute(
            "INSERT INTO snapshots(task_id, platform, keyword, status, message, item_count, ts) VALUES(?,?,?,?,?,?,?)",
            (task_id, platform, keyword, status, message or "", item_count, time.time()),
        )


def log(level, text):
    ts = time.time()
    with transaction() as c:
        c.execute("INSERT INTO logs(level, text, ts) VALUES(?,?,?)", (level, text, ts))
        # 只留最近 3000 条
        c.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 3000)")


def recent_logs(limit=200):
    with transaction() as c:
        rows = c.execute("SELECT level, text, ts FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------------- 查询 ----------------
def latest_products(platform=None, group=None, limit=1500, only_ok=False, condition=None):
    """每个 (platform, item_key) 的最新一行，按价格升序。"""
    # 分组用 python 侧映射，避免 sqlite 子查询复杂度
    sql = "SELECT * FROM products WHERE 1=1"
    args = []
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    if only_ok:
        sql += " AND price_level='ok'"
    if condition:
        sql += " AND condition=?"
        args.append(condition)
    sql += " ORDER BY price ASC LIMIT ?"
    args.append(limit)
    with transaction() as c:
        rows = c.execute(sql, args).fetchall()
    out = [dict(r) for r in rows]
    kw2group = {}
    for g in keyword_groups():
        for kw in g["enabled_keywords"]:
            kw2group[kw] = (g["id"], g["name"])
    from .classify import category_key, detail_key as detail_label
    for r in out:
        gid, gname = kw2group.get(r["keyword"], ("", ""))
        r["group_id"] = gid
        r["group_name"] = gname
        r["is_low"] = abs(r["price"] - r["min_price"]) < 0.01
        # 老数据没有 spec_key，这里实时补算，界面上一样能看到分类
        # 主分类（粗，用于比价）+ 细分标签（含品牌）
        try:
            r["spec_key"] = category_key(r, group_id=gid)
            r["spec_label"] = detail_label(r, group_id=gid)
            r["condition"] = r.get("condition") or "new"
            r["price_level"] = r.get("price_level") or "ok"
        except Exception:
            r["spec_key"] = r.get("spec_key") or "未分类"
            r["spec_label"] = "—"
    if group:
        out = [r for r in out if r["group_id"] == group]
    return out


def spec_summary(platform=None, group=None, only_ok=False, condition=None):
    """按「规格分类」聚合：每个分类下的最低价、最高价、商品数。
    这是真正有比价意义的东西 —— 不同标题但同规格的商品才能横向对比。"""
    rows = latest_products(platform=platform, group=group, limit=9999, only_ok=only_ok, condition=condition)
    buckets = {}
    for r in rows:
        key = r.get("spec_key") or "未分类"
        b = buckets.setdefault(key, {
            "spec_key": key, "spec_label": r.get("spec_label") or "—",
            "group_id": r.get("group_id") or "", "group_name": r.get("group_name") or "",
            "min": r["price"], "max": r["price"], "count": 0,
            "best_id": r["id"], "best_title": r["title"], "best_url": r["url"],
            "best_platform": r["platform"], "best_shop": r.get("shop") or "",
            "platforms": set(),
        })
        b["count"] += 1
        b["platforms"].add(r["platform"])
        if r["price"] < b["min"]:
            b["min"] = r["price"]
            b["best_id"] = r["id"]
            b["best_title"] = r["title"]
            b["best_url"] = r["url"]
            b["best_platform"] = r["platform"]
            b["best_shop"] = r.get("shop") or ""
        if r["price"] > b["max"]:
            b["max"] = r["price"]
    out = []
    for b in buckets.values():
        b["platforms"] = sorted(b["platforms"])
        b["spread"] = round(b["max"] - b["min"], 2)
        b["spread_pct"] = round((b["spread"] / b["min"] * 100), 1) if b["min"] else 0
        out.append(b)
    out.sort(key=lambda x: (x["group_id"], x["min"]))
    return out


def price_history(product_id, limit=200):
    with transaction() as c:
        rows = c.execute(
            "SELECT price, ts FROM price_history WHERE product_id=? ORDER BY ts ASC LIMIT ?", (product_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def last_snapshots():
    with transaction() as c:
        rows = c.execute("""
            SELECT s.* FROM snapshots s
            JOIN (SELECT platform, MAX(id) AS mid FROM snapshots GROUP BY platform) t
              ON s.id = t.mid
        """).fetchall()
    return {r["platform"]: dict(r) for r in rows}


def history_runs(limit=20):
    with transaction() as c:
        rows = c.execute("""
            SELECT task_id, MIN(ts) AS started, SUM(item_count) AS items,
                   GROUP_CONCAT(DISTINCT platform) AS platforms
            FROM snapshots GROUP BY task_id ORDER BY started DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats():
    with transaction() as c:
        p = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        h = c.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        s = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    return {"products": p, "history": h, "snapshots": s}