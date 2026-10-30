# -*- coding: utf-8 -*-
"""探针 2：找淘宝 PC 详情页背后的 JSON 接口（能否绕开整页渲染）。
同时实测多件商品的耗时。跑完可删。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("TMP", r"E:\wb\pricewatch\tmp\skuprobe")
os.environ.setdefault("TEMP", r"E:\wb\pricewatch\tmp\skuprobe")

from pricewatch.browser import Session

ITEM = sys.argv[1] if len(sys.argv) > 1 else "874277088694"
URL = f"https://detail.tmall.com/item.htm?id={ITEM}"

cap = []


with Session("taobao", headless=True) as s:
    page = s.page()

    def on_resp(r):
        try:
            if any(k in r.url for k in ("mtop", "h5api", "detail", "sku")):
                cap.append(r)
        except Exception:
            pass

    page.on("response", on_resp)
    t0 = time.time()
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    print(f"goto {time.time() - t0:.1f}s, captured {len(cap)} resp", flush=True)

    for r in cap:
        try:
            body = r.text()
        except Exception:
            continue
        hit = "skuBase" in body or "skuCore" in body
        print(f"  [{r.request.method}] {r.status} len={len(body):>7} sku={'YES' if hit else '-'} {r.url[:130]}", flush=True)
        if hit:
            print("     >>> FULL URL:\n", r.url, flush=True)
            try:
                pd = r.request.post_data
                print("     >>> POST DATA:", (pd or "")[:400], flush=True)
            except Exception:
                pass
            with open(os.path.join(os.environ["TMP"], "mtop_body.json"), "w", encoding="utf-8") as f:
                f.write(body)
            print("     saved to tmp/skuprobe/mtop_body.json", flush=True)
