# -*- coding: utf-8 -*-
"""详情页 SKU 解析结果的短期缓存。

为什么需要它：
  淘宝详情页在批量访问下会挂起（page.goto 超时都不按设定返回，实测卡 430 秒），
  所以 SKU 校正默认关闭。但同一件商品的价格 1 小时内基本不变 ——
  如果把它第一次读到的 SKU 表缓存起来，第 2 次以后就完全不用再开页面，
  这样既能拿到精度，又不会反复触发风控。

  缓存命中时直接返回上次的 sku_prices，调用方对「是否真的开了详情页」无感。

设计：
  · 按 (platform, item_key) 存，item_key 优先用商品 id，退化成 URL hash
  · TTL 默认 45 分钟；超时自动失效
  · 存的是**解析后的 sku_prices**（不是 HTML），体积很小，几十 KB 级别
  · 原子写（临时文件 + replace），避免半写坏掉
  · 任何一个文件损坏都当作 miss，不影响采集
"""
import hashlib
import json
import os
import time

from . import config

CACHE_DIR = os.path.join(config.DATA_DIR, "sku_cache")
DEFAULT_TTL = 45 * 60          # 45 分钟


def _key(platform, item_key):
    raw = f"{platform}|{item_key}".encode("utf-8", "replace")
    return hashlib.md5(raw).hexdigest()[:20]


def _path(platform, item_key):
    return os.path.join(CACHE_DIR, f"{platform}_{_key(platform, item_key)}.json")


def item_key_of(url):
    """从商品 URL 里抠出稳定的 key。取不到就整串 hash。"""
    import re
    m = re.search(r"(?:id=|goods_id=|/)(\d{6,})", url or "")
    if m:
        return "id:" + m.group(1)
    return "h:" + hashlib.md5((url or "").encode("utf-8", "replace")).hexdigest()[:16]


def get(platform, item_key):
    """读缓存。命中返回 sku_prices 列表，否则 None。"""
    p = _path(platform, item_key)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("expire", 0) < time.time():
            return None
        skus = d.get("sku_prices") or []
        return skus or None
    except Exception:
        return None


def put(platform, item_key, skus, title="", ttl=None, blocked=False):
    """写缓存。blocked=True（页面被风控）时**不写** —— 免得把不完整的
    SKU 表缓存下来，之后一直用脏数据。"""
    if not skus or blocked:
        return False
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        d = {
            "platform": platform,
            "item_key": item_key,
            "title": (title or "")[:120],
            "sku_count": len(skus),
            "sku_prices": skus[:40],
            "cached_at": time.time(),
            "expire": time.time() + float(ttl or DEFAULT_TTL),
        }
        p = _path(platform, item_key)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def stats():
    """缓存数量与总大小，给界面/日志看。"""
    n = 0
    size = 0
    try:
        for fn in os.listdir(CACHE_DIR):
            if fn.endswith(".json"):
                n += 1
                size += os.path.getsize(os.path.join(CACHE_DIR, fn))
    except Exception:
        pass
    return {"files": n, "kb": round(size / 1024, 1)}


def clear():
    """清空缓存（改规则后想强制重读时用）。"""
    n = 0
    try:
        for fn in os.listdir(CACHE_DIR):
            if fn.endswith(".json"):
                os.remove(os.path.join(CACHE_DIR, fn))
                n += 1
    except Exception:
        pass
    return n
