# -*- coding: utf-8 -*-
"""出口 IP 管理：指定代理 + 出口校验。

为什么需要它（实测依据，2026-10-31）：
  本机直连出口是固定美国 IP 142.249.36.59（Back Waves Limited, AS153371），
  Clash 进程在跑但**没有任何端口监听**，等于没生效。
  结果：淘宝搜索能用，但京东 search.jd.com 直接 302 跳风控、
  拼多多搜索返回空壳页 —— 都是境外 IP 被挡。

所以要做两件事：
  1. **让爬虫能走指定代理**（config.PROXY_URL），换成国内出口就能解锁
  2. **采集前校验出口 IP**，在日志/界面上把这个事实说清楚，
     免得每次点采集都白等冷却

设计原则：
  · 不硬编码任何代理地址，全部走 config；PROXY_URL 为空就是直连
  · 校验失败不阻断采集（探测只是提示，不做准入）
  · 校验结果缓存几分钟，避免每次采集都多打一次网络请求
"""
import json
import re
import time
import urllib.request

from . import config
from .db import log

# 多个探测端点，按顺序试，任一可用即可（国内站/国外站都留一条）
PROBE_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "http://ip-api.com/line/?fields=query,country,regionName,isp",
)

_cache = {"ts": 0, "info": None}
CACHE_TTL = 300          # 5 分钟内不重复探测


def proxy_url(platform=None):
    """取该平台该用的代理地址。空字符串 = 直连。

    支持按平台区分（config.PLATFORM_PROXY），例如只让京东走国内代理。
    """
    per = getattr(config, "PLATFORM_PROXY", None) or {}
    if platform and platform in per:
        return per[platform] or ""
    return getattr(config, "PROXY_URL", "") or ""


def playwright_proxy(platform=None):
    """给 Playwright 用的 proxy dict，没有就返回 None。"""
    u = proxy_url(platform)
    if not u:
        return None
    return {"server": u}


def _opener(proxy):
    """构造带代理的 opener。proxy 为空则直连。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 明确禁用环境代理，保证"直连"真的是直连
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _parse_ip_info(text):
    """把各种探测端点的返回统一成 {ip, country, region, isp}。"""
    t = (text or "").strip()
    info = {"ip": None, "country": None, "region": None, "isp": None}

    # JSON 格式（ipinfo.io / ipapi.co / api.ip.sb / ip-api.com/json）——
    # 实测这台机器上 ip-api 的 line 接口被墙，但 JSON 版本可用
    if t.startswith("{"):
        try:
            import json as _json
            d = _json.loads(t)
            info["ip"] = d.get("ip") or d.get("query") or d.get("ip_address")
            info["country"] = d.get("country") or d.get("country_name")
            info["region"] = d.get("region") or d.get("regionName") or d.get("city")
            info["isp"] = (d.get("org") or d.get("isp")
                           or d.get("organization") or d.get("asn_organization")
                           or d.get("as"))
            return info
        except Exception:
            pass

    m = re.search(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", t)
    if m:
        info["ip"] = m.group(1)

    # cloudflare/cdn-cgi/trace 的 kv 行格式：ip=... loc=US
    kv = dict()
    for line in t.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    if kv:
        info["ip"] = info["ip"] or kv.get("ip")
        info["country"] = kv.get("loc")

    # ip-api.com/line 的逐行格式
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    if lines and re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", lines[0] or ""):
        info["ip"] = lines[0]
        info["country"] = lines[1] if len(lines) > 1 else None
        info["region"] = lines[2] if len(lines) > 2 else None
        info["isp"] = lines[-1] if lines else None
    return info


def probe(force=False):
    """探测当前出口 IP 与地区。返回 dict 或 None（全失败）。

    结果缓存 CACHE_TTL 秒。探测走 config.PROXY_URL（如果设了）。

    先用 ip-api.com/line 拿「IP + 国家 + 地区 + ISP」一条龙（国内可访问），
    失败再退回纯 IP 端点（只有 IP、没有地区）。
    """
    now = time.time()
    if not force and _cache["info"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["info"]

    proxy = getattr(config, "PROXY_URL", "") or ""
    op = _opener(proxy)

    # 顺序：优先能返回「IP + 地区」的 JSON 端点（实测这台机器都可访问），
    # 最后才是只有纯 IP 的端点。
    ordered = (
        "https://ipinfo.io/json",
        "https://ipapi.co/json/",
        "https://api.ip.sb/geoip",
        "https://www.cloudflare.com/cdn-cgi/trace",
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
    )
    info = None
    for u in ordered:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "pricewatch/1.0"})
            with op.open(req, timeout=10) as r:
                raw = r.read().decode("utf-8", "replace")
            parsed = _parse_ip_info(raw)
            if parsed.get("ip"):
                info = parsed
                info["via"] = "proxy" if proxy else "direct"
                info["probe_url"] = u
                # 有地区就够好，直接收工；只有 IP 的话继续试下一条
                if parsed.get("country"):
                    break
        except Exception:
            continue

    if info:
        _cache.update(ts=now, info=info)
    return info


def describe(info=None):
    """给人看的出口描述。"""
    info = info if info is not None else probe()
    if not info:
        return "出口未知（探测失败）"
    via = "代理" if info.get("via") == "proxy" else "直连"
    parts = [info.get("ip") or "?", info.get("country") or "?", via]
    if info.get("region"):
        parts.append(info["region"])
    return " · ".join(str(p) for p in parts if p)


def is_domestic(info=None):
    """出口是否为中国大陆。判断不了返回 None。"""
    info = info if info is not None else probe()
    if not info:
        return None
    c = (info.get("country") or "").lower()
    if not c:
        return None
    return ("china" in c) or ("中国" in c) or c in ("cn",)


def check_on_startup():
    """采集前调用一次：探测出口并在日志里说明。不做准入阻断。"""
    try:
        info = probe()
        if not info:
            log("warn", "[出口] 探测失败，按直连继续")
            return None
        dom = is_domestic(info)
        line = f"[出口] {describe(info)}"
        if dom is False:
            line += "　⚠ 非中国大陆出口：京东/拼多多搜索大概率被风控挡"
            log("warn", line)
        else:
            log("info", line)
        return info
    except Exception as e:
        log("warn", f"[出口] 校验异常: {str(e)[:80]}")
        return None
