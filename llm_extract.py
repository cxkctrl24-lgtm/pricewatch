# -*- coding: utf-8 -*-
"""大模型页面解析兜底：**只在正则完全抽不到商品时**才调用。

为什么只能当兜底（实测数据，见 analysis/llm_page_analysis.md）：
  · qwen3.5:9b 抽 40 条要 25 秒、还会漏 20%；正则 0.1 秒抽 75 条全对
  · qwen3.5:2b 在 40 条时直接崩（价格全填 0）
  所以绝不能替代主提取路径。它的价值在「反正那一轮本来就要失败」的场景：
  正则 0 条 → 让模型尽力抽几条，总比什么都没有强。

三条硬约束：
  1. 只在 items == 0 时调用 —— 正常采集零开销
  2. 文本截断到 TEXT_LIMIT 字 —— 超过模型必崩
  3. 硬超时 —— 不能让它拖住采集（详情页挂起的教训）

依赖本机 Ollama（默认 http://127.0.0.1:11434）。没装就自动跳过，不影响采集。
"""
import json
import re
import urllib.request

from . import config
from .db import log

OLLAMA_URL = getattr(config, "LLM_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
MODEL = getattr(config, "LLM_MODEL", "qwen3.5:9b")
TEXT_LIMIT = 2000          # 超过这个长度模型准确率断崖下跌
TIMEOUT_S = 20


def available():
    """Ollama 是否可用（探一下 tags 接口）。"""
    try:
        with urllib.request.urlopen(
                OLLAMA_URL.replace("/api/generate", "/api/tags"), timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _call(prompt):
    """调 Ollama，返回 (文本, 错误)。关掉思考模式 —— qwen3.5 默认把内容
    写进 thinking 字段，response 会是空的，这个坑踩过。"""
    body = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 700},
    }
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return (d.get("response") or "").strip(), None
    except Exception as e:
        return "", str(e)[:100]


def _parse_json_lines(text):
    """从模型输出里抠出 {title, price}。容错单引号、数组、markdown 包裹。"""
    items = []
    for line in (text or "").splitlines():
        s = line.strip().strip("`").strip()
        if not s:
            continue
        # 支持整行是数组的情况
        objs = None
        if s.startswith("["):
            try:
                objs = json.loads(s)
            except Exception:
                objs = None
        elif s.startswith("{"):
            try:
                objs = [json.loads(s)]
            except Exception:
                try:
                    objs = [json.loads(s.replace("'", '"'))]
                except Exception:
                    objs = None
        for d in (objs or []):
            if not isinstance(d, dict):
                continue
            title = str(d.get("title") or "").strip()
            price_raw = str(d.get("price") or "").replace("¥", "").replace(",", "").strip()
            try:
                price = float(price_raw)
            except Exception:
                continue
            if not title or len(title) < 5:
                continue
            if not (1 < price < 200000):
                continue
            items.append({"title": title[:180], "price": price})
    return items


def extract_page_text(page, platform, group_id=None, price_range=None):
    """正则抽不到商品时的兜底提取。

    返回与 collector._extract_items 相同结构的 items 列表（可能为空）。
    调用方应只在 items==0 时调用它。
    """
    if not getattr(config, "LLM_FALLBACK", True):
        return []
    if not available():
        log("info", "[LLM兜底] 本机没检测到 Ollama，跳过")
        return []

    try:
        body = page.inner_text("body") or ""
    except Exception as e:
        log("warn", f"[LLM兜底] 读页面文本失败: {str(e)[:80]}")
        return []
    body = body.strip()
    if len(body) < 80:
        return []
    # 截断：超过 TEXT_LIMIT 模型准确率断崖下跌（实测 40 条就崩）
    clipped = body[:TEXT_LIMIT]

    prompt = f"""/no_think
从下面的电商页面文本里提取所有商品，每行一个 JSON：
{{"title": "商品标题(去掉价格/销量/促销词如券后/已拼/仅剩/包邮)", "price": 价格数字}}

页面文本：
{clipped}

只输出 JSON 行，不要解释："""

    text, err = _call(prompt)
    if err:
        log("warn", f"[LLM兜底] 模型调用失败: {err[:80]}")
        return []
    items = _parse_json_lines(text)
    if not items:
        log("info", "[LLM兜底] 模型也没抽到商品")
        return []

    # 补上 collector 需要的字段，并把价格区间过滤掉
    cfg = config.PLATFORMS.get(platform, {})
    lo, hi = price_range if price_range else (0, 10 ** 9)
    import hashlib
    host = "mobile.yangkeduo.com" if platform == "pdd" else "item.taobao.com"
    out = []
    for it in items:
        if not (lo <= it["price"] <= hi):
            continue
        # 模型拿不到真实商品链接，用 (平台+标题) 的 hash 合成一个稳定占位 URL；
        # item_key 会走标题 hash，所以同一商品跨轮次能对上历史价。
        h = hashlib.md5((platform + it["title"]).encode("utf-8", "replace")).hexdigest()[:16]
        out.append({
            "title": it["title"],
            "price": it["price"],
            "shop": "",
            "url": f"https://{host}/#t={h}",
            "_llm": True,        # 标记来源，方便排查
        })

    if out:
        log("info", f"[LLM兜底] 正则没抽到，模型补抽 {len(out)} 件（{MODEL}）")
    return out
