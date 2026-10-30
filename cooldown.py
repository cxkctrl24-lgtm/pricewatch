# -*- coding: utf-8 -*-
"""平台级限流冷却的状态机（持久化到 data/cooldown.json）。

为什么需要它：京东一被限流就锁很久，而进程重启/重新点「开始采集」
会把内存里的状态清零，于是马上又去撞墙，越撞越久。
这里把"这个平台什么时候能再搜"写进磁盘，跨会话生效。

阶梯式退避：连续被限流就翻倍冷却（30 分钟 → 60 → 120 → 封顶 4 小时），
成功采集一次就清零回正常。
"""
import json
import os
import time

from . import config


def _path():
    return os.path.join(config.DATA_DIR, "cooldown.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path())
    except Exception:
        pass


def remaining(platform):
    """还要等多少秒才能再搜这个平台。0 = 现在就可以。"""
    rec = _load().get(platform) or {}
    until = float(rec.get("until") or 0)
    left = until - time.time()
    return max(0.0, left)


def is_cooling(platform):
    left = remaining(platform)
    if left <= 0:
        return None
    return left


def record_limit(platform, base_cooldown=None):
    """记一次限流。冷却按连续次数翻倍，封顶 4 小时。返回本次冷却秒数。"""
    data = _load()
    rec = dict(data.get(platform) or {})
    base = float(base_cooldown if base_cooldown is not None
                 else config.pacing(platform)["cooldown"])
    hits = int(rec.get("hits") or 0) + 1
    wait = min(base * (2 ** (hits - 1)), 4 * 3600)
    rec["hits"] = hits
    rec["until"] = time.time() + wait
    rec["last"] = time.time()
    data[platform] = rec
    _save(data)
    return wait


def clear(platform):
    """采集成功，清掉该平台的限流记录。"""
    data = _load()
    if platform in data:
        data.pop(platform, None)
        _save(data)


def hits(platform):
    rec = _load().get(platform) or {}
    return int(rec.get("hits") or 0)


def describe(platform):
    """给界面/日志看的一句话状态。"""
    left = remaining(platform)
    if left <= 0:
        return ""
    m = int(left // 60) + 1
    return f"冷却中，约 {m} 分钟后可再搜"
