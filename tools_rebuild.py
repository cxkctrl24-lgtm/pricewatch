# -*- coding: utf-8 -*-
"""把三级过滤（可疑丢弃 / 异常标记 / 正常收录）应用到库里已有的全部数据。

用途：
  · 改过分类规则或过滤规则后，不用重抓，直接重算存量
  · 已入库的可疑商品会移到 rejected 表并从 products 删除

用法：
    python tools_rebuild.py            # 重算，打印报告
    python tools_rebuild.py --apply    # 真正写库
"""
import os
import sys
import statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricewatch import config, db, fakefilter, outlier
from pricewatch.classify import classify, category_key


def group_of(kw):
    u = (kw or "").upper()
    return "ddr5" if "DDR5" in u else "ddr4" if "DDR4" in u else "rtx50" if "RTX" in u else None


def main():
    apply = "--apply" in sys.argv
    db.init()
    rows = db.latest_products(limit=99999, only_ok=False)

    # ---------- 1. 重新分类 ----------
    for r in rows:
        gid = group_of(r.get("keyword"))
        r["_spec"] = classify(r.get("title"), gid)
        r["_cat"] = category_key(r, gid)
        r["_cond"] = r["_spec"].get("condition") or "new"

    # ---------- 2. 假货指纹 ----------
    for r in rows:
        try:
            bad, pts, reasons, used = fakefilter.score(r, None)
        except Exception:
            bad, reasons = False, []
        r["_reject"] = "；".join(reasons[:3]) if bad else ""

    # ---------- 3. 同档离群 ----------
    grouped_new = {}
    for r in rows:
        if r["_cond"] == "new":
            grouped_new.setdefault(r["_cat"], []).append(r["price"])
    for r in rows:
        if r["_reject"]:
            continue
        peers = grouped_new.get(r["_cat"], [])
        try:
            lvl, why, lo, hi = outlier.judge(
                r["price"], r["_spec"], peers, r["_cond"] == "used")
        except Exception:
            lvl, why = "ok", ""
        if lvl == "reject":
            r["_reject"] = why
            r["_level"] = "reject"
        elif lvl == "review":
            r["_level"] = "review"
            r["_note"] = why
        else:
            r["_level"] = "ok"
            r["_note"] = ""

    # ---------- 4. 统计 ----------
    rejects = [r for r in rows if r["_reject"]]
    reviews = [r for r in rows if r.get("_level") == "review"]
    oks = [r for r in rows if not r["_reject"] and r.get("_level") != "review"]

    print(f"共 {len(rows)} 件")
    print(f"  正常收录 {len(oks)}")
    print(f"  异常标记 {len(reviews)}")
    print(f"  直接剔除 {len(rejects)}\n")

    if rejects:
        print("=== 将被剔除 ===")
        for r in sorted(rejects, key=lambda x: x["price"])[:20]:
            print(f'  ¥{r["price"]:>7.0f} {r["title"][:36]:38s} | {r["_reject"][:44]}')
    if reviews:
        print("\n=== 将标记异常 ===")
        for r in sorted(reviews, key=lambda x: -x["price"])[:20]:
            print(f'  ¥{r["price"]:>7.0f} {r["title"][:36]:38s} | {r["_note"][:44]}')

    if not apply:
        print("\n（预览模式，加 --apply 才会真正写库）")
        return

    # ---------- 5. 写库 ----------
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    n_del = n_upd = 0
    for r in rows:
        if r["_reject"]:
            db.record_rejected(r["platform"], r.get("keyword"), r, r["_reject"])
            conn.execute("DELETE FROM products WHERE id=?", (r["id"],))
            n_del += 1
        else:
            conn.execute(
                "UPDATE products SET spec_key=?, condition=?, price_level=?, price_note=? WHERE id=?",
                (r["_cat"], r["_cond"], "review" if r.get("_level") == "review" else "ok",
                 r.get("_note") or "", r["id"]),
            )
            n_upd += 1
    conn.commit()
    conn.close()
    print(f"\n已写库：删除 {n_del} 件（转入 rejected），更新 {n_upd} 件")


if __name__ == "__main__":
    main()