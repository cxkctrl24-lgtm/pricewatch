# -*- coding: utf-8 -*-
"""看拼多多登录页有哪些登录方式（扫码 / 手机号 / 微信）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricewatch.browser import Session

URLS = [
    "https://mobile.yangkeduo.com/login.html",
    "https://mobile.yangkeduo.com/login.html?referrer=https%3A%2F%2Fmobile.yangkeduo.com%2F",
    "https://www.pinduoduo.com/",
]

with Session("pdd", headless=True) as s:
    page = s.page()
    for url in URLS:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3500)
            txt = page.evaluate("() => document.body.innerText") or ""
            print("URL:", page.url[:110])
            print("标题:", page.title())
            print("文本:", " | ".join(txt.split())[:320])
            info = page.evaluate("""() => {
              const out = {imgs: [], inputs: [], btns: []};
              document.querySelectorAll('img').forEach(i => { if(i.src) out.imgs.push(i.src.slice(0, 80)); });
              document.querySelectorAll('input').forEach(i => out.inputs.push((i.type||'') + ':' + (i.placeholder||'')));
              document.querySelectorAll('button,div[role=button],a').forEach(b => {
                const t=(b.innerText||'').trim(); if(t && t.length < 24) out.btns.push(t);
              });
              return out;
            }""")
            print("图片:", info["imgs"][:6])
            print("输入框:", info["inputs"][:8])
            print("按钮:", list(dict.fromkeys(info["btns"]))[:12])
        except Exception as e:
            print("失败:", url, type(e).__name__, str(e)[:120])
        print("-" * 70)