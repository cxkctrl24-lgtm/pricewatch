# -*- coding: utf-8 -*-
"""进商品详情页看 SKU：多规格商品到底哪个规格卖多少钱。

用法：python tools_item_sku.py taobao <商品ID或整条URL>
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricewatch.browser import Session

plat = sys.argv[1] if len(sys.argv) > 1 else "taobao"
target = sys.argv[2] if len(sys.argv) > 2 else ""

if target.startswith("http"):
    url = target
elif plat == "taobao":
    url = f"https://item.taobao.com/item.htm?id={target}"
elif plat == "jd":
    url = f"https://item.jd.com/{target}.html"
else:
    url = f"https://mobile.yangkeduo.com/goods.html?goods_id={target}"

with Session(plat, headless=True) as s:
    page = s.page()
    print("打开:", url[:100], flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)

    print("标题:", page.title()[:60], flush=True)
    print("最终URL:", page.url[:100], flush=True)

    # 抓详情页的接口请求（SKU 数据一般来自单独接口）
    reqs = []
    page.on("response", lambda r: reqs.append(r.url) if any(
        k in r.url for k in ("sku", "item", "detail", "price", "spec")) else None)
    page.mouse.wheel(0, 900)
    page.wait_for_timeout(2500)

    # 页面上能看到的规格选项
    info = page.evaluate("""() => {
      const out = {specs: [], prices: []};
      // 常见规格容器
      const sel = ['[class*=sku]','[class*=Sku]','[class*=spec]','[class*=Spec]',
                   '[class*=property]','[class*=Property]','[class*=option]'];
      for (const s of sel) {
        document.querySelectorAll(s).forEach(e => {
          const t = (e.innerText||'').trim();
          if (t && t.length < 40 && t.length > 1) out.specs.push(t.replace(/\\n/g,'/'));
        });
        if (out.specs.length > 40) break;
      }
      out.specs = Array.from(new Set(out.specs)).slice(0, 30);
      // 页面上的价格
      const m = (document.body.innerText||'').match(/[¥￥]\\s*[\\d,]+(?:\\.\\d+)?/g) || [];
      out.prices = Array.from(new Set(m)).slice(0, 15);
      return out;
    }""")
    print("\n规格选项:", json.dumps(info["specs"][:20], ensure_ascii=False), flush=True)
    print("页面价格:", info["prices"], flush=True)
    print("\n相关接口:", [u[:90] for u in reqs[:8]], flush=True)