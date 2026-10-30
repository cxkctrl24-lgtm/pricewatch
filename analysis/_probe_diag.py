# -*- coding: utf-8 -*-
"""探针 4：诊断 —— 连续请求后详情页返回了什么（风控页？降级页？），
并测「补一次 goto + 更长等待」能否恢复。跑完可删。
"""
import os
import sys
import time
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("TMP", r"E:\wb\pricewatch\tmp\skuprobe")
os.environ.setdefault("TEMP", r"E:\wb\pricewatch\tmp\skuprobe")

from pricewatch.browser import Session
from pricewatch.scrapers import detail as d

IID = sys.argv[1] if len(sys.argv) > 1 else "874277088694"
URL = f"https://detail.tmall.com/item.htm?id={IID}"


def diag(tag, html, extra=""):
    title = ""
    m = re.search(r"<title>([^<]{0,120})</title>", html)
    if m:
        title = m.group(1)
    flags = {
        "skuBase": '"skuBase"' in html,
        "skuCore": '"skuCore"' in html,
        "captcha": ("baxia" in html.lower() or "nocaptcha" in html.lower()
                    or "punish" in html.lower() or "滑块" in html),
        "login_redirect": "login.taobao.com" in html or "login.tmall.com" in html,
        "err": ("抱歉" in html or "错误" in html or "refreshing" in html.lower()),
    }
    print(f"[{tag}] len={len(html)//1024}KB title={title[:60]!r} {flags} {extra}", flush=True)


with Session("taobao", headless=True) as s:
    print("login:", s.has_login_cookie(), flush=True)

    # 1) 先像真人一样在首页停一会儿
    p = s.page()
    p.goto("https://www.taobao.com/", wait_until="domcontentloaded", timeout=40000)
    p.wait_for_timeout(3500)
    print("home ok:", p.title()[:40], flush=True)
    time.sleep(4)

    # 2) 打开详情页，每 1 秒采一次是否出现 skuBase，看它何时注入
    p.goto(URL, wait_until="domcontentloaded", timeout=45000)
    for k in range(12):
        p.wait_for_timeout(1000)
        h = p.content()
        has = '"skuBase"' in h
        print(f"  t={k+1}s len={len(h)//1024}KB skuBase={has}", flush=True)
        if has:
            break

    h = p.content()
    diag("final", h, "url=" + p.url[:70])
    props, skus = d.parse_taobao(h)
    print("skus:", len(skus), flush=True)
    for x in skus[:20]:
        print(f"   ¥{x['price']:>8} {x['spec']}", flush=True)

    # 3) 纯 HTTP 再试一次（刚导航过，带上了完整 cookie 与 session）
    time.sleep(5)
    print("--- ctx.request ---", flush=True)
    try:
        r = s.ctx.request.get(URL, timeout=30000, fail_on_status_code=False)
        h2 = r.text()
        diag("http", h2, f"status={r.status} ctype={r.headers.get('content-type')}")
        print("  head:", h2[:200].replace("\n", " "), flush=True)
    except Exception as e:
        print("  http fail:", str(e)[:120], flush=True)
