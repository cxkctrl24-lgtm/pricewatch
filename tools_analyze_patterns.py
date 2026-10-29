# -*- coding: utf-8 -*-
"""从真实商品标题里自动归纳「共同点」，用于校准分类规则。

不靠拍脑袋定规则，而是统计现有数据：
  1. 高频规格组合（哪些 (代数, 容量, 频率, 台式/笔记本) 组合真的存在）
  2. 每个组合的价格分布（中位数 / 四分位 / 样本数）→ 回填到 outlier.BASELINE
  3. 高频词 / 品牌覆盖率 / 二手标识词命中率
  4. 异常分类（样本极少或价格跨度巨大的分类）

输出：
  · 直接打印报告
  · 可选 --write 把统计出的基准价写回 baseline_auto.json
"""
import json
import os
import re
import statistics
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricewatch import config, db
from pricewatch.classify import classify, category_key


def group_of(kw):
    u = (kw or "").upper()
    if "DDR5" in u:
        return "ddr5"
    if "DDR4" in u:
        return "ddr4"
    if "RTX" in u:
        return "rtx50"
    return None


def collect_rows():
    db.init()
    rows = db.latest_products(limit=99999, only_ok=False)
    return rows


def analyze(rows):
    report = {}

    # ---------- 1. 规格组合分布 ----------
    combos = {}
    for r in rows:
        spec = classify(r.get("title"), group_of(r.get("keyword")))
        key = (spec.get("gen"), spec.get("cap"), spec.get("count") or 1,
               spec.get("mem_type"), spec.get("gpu"), spec.get("vram"))
        combos.setdefault(key, []).append(r["price"])

    report["combos"] = []
    for key, prices in sorted(combos.items(), key=lambda kv: -len(kv[1])):
        if len(prices) < 2:
            continue
        s = sorted(prices)
        n = len(s)
        report["combos"].append({
            "gen": key[0], "cap": key[1], "count": key[2],
            "mem_type": key[3], "gpu": key[4], "vram": key[5],
            "n": n,
            "min": s[0], "max": s[-1],
            "median": statistics.median(s),
            "q1": statistics.median(s[: n // 2]) if n >= 4 else None,
            "q3": statistics.median(s[(n + 1) // 2:]) if n >= 4 else None,
            "span": round(s[-1] / s[0], 1) if s[0] else 0,
        })

    # ---------- 2. 价格跨度过大的组合 ----------
    report["unstable"] = [c for c in report["combos"] if c["span"] >= 4]

    # ---------- 3. 品牌覆盖率 ----------
    n_brand = sum(1 for r in rows if classify(r.get("title"))["brand"])
    report["brand_coverage"] = round(n_brand / len(rows) * 100, 1) if rows else 0

    # ---------- 4. 二手占比 ----------
    n_used = sum(1 for r in rows if classify(r.get("title"))["condition"] == "used")
    report["used_ratio"] = round(n_used / len(rows) * 100, 1) if rows else 0

    # ---------- 5. 高频词（非规格词） ----------
    stop = set("的内存条台式机笔记全新正品原装电脑游戏适用型号系列".split())
    words = Counter()
    for r in rows:
        t = r.get("title") or ""
        for w in re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fa5]{2,4}", t):
            if w.lower() in stop or len(w) < 2:
                continue
            words[w] += 1
    report["top_words"] = words.most_common(30)

    return report


def to_baseline(report, min_n=3):
    """把统计出的组合价写回 BASELINE 格式（用于样本不足时的兜底）。"""
    out = {}
    for c in report["combos"]:
        if c["n"] < min_n:
            continue
        if c["gen"] and c["cap"]:
            key = f'{c["gen"]}|{c["cap"]}|{c["count"]}|{c["mem_type"] or "通用"}'
            out[key] = [round(c["min"]), round(c["max"])]
        elif c["gpu"]:
            key = f'RTX|{c["gpu"].replace("RTX ", "")}|{c["vram"]}'
            out[key] = [round(c["min"]), round(c["max"])]
    return out


def main():
    rows = collect_rows()
    print(f"读取 {len(rows)} 件商品\n")
    rep = analyze(rows)

    print("=" * 76)
    print("一、规格组合分布（样本 ≥2 件）")
    print("=" * 76)
    print(f'{"代数":6s}{"容量":>6}{"条数":>4}{"类型":>6}{"显卡":>10}{"显存":>5}{"件数":>4}{"最低":>8}{"中位":>8}{"最高":>8}{"跨度":>7}')
    for c in rep["combos"][:30]:
        gpu = (c["gpu"] or "").replace("RTX ", "") if c["gpu"] else ""
        print(f'{str(c["gen"] or ""):6s}{str(c["cap"] or ""):>6}{str(c["count"]):>4}'
              f'{str(c["mem_type"] or ""):>6}{gpu:>10}{str(c["vram"] or ""):>5}'
              f'{c["n"]:>4}¥{c["min"]:>7.0f}¥{c["median"]:>7.0f}¥{c["max"]:>7.0f}'
              f'{c["span"]:>6.1f}x')

    print("\n" + "=" * 76)
    print("二、价格跨度 ≥4 倍的组合（分类内部仍有离谱价格）")
    print("=" * 76)
    for c in rep["unstable"]:
        name = f'{c["gen"] or ""} {c["cap"] or ""}G×{c["count"]} {c["mem_type"] or ""} {c["gpu"] or ""} {c["vram"] or ""}G'.strip()
        print(f'  {name:34s} {c["n"]:>3}件  ¥{c["min"]:>7.0f} ~ ¥{c["max"]:>7.0f}  ({c["span"]}x)')

    print("\n" + "=" * 76)
    print("三、共同点统计")
    print("=" * 76)
    print(f'  品牌覆盖率: {rep["brand_coverage"]}%')
    print(f'  二手占比:   {rep["used_ratio"]}%')
    print(f'\n  高频词 Top20:')
    for w, n in rep["top_words"][:20]:
        print(f'    {w:12s} {n:>4} 次')

    bl = to_baseline(rep)
    print(f"\n可自动推导的基准价区间: {len(bl)} 个组合")
    for k, v in list(bl.items())[:12]:
        print(f'    {k:34s} ¥{v[0]} – ¥{v[1]}')

    if "--write" in sys.argv:
        path = os.path.join(config.DATA_DIR, "baseline_auto.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bl, f, ensure_ascii=False, indent=1)
        print(f"\n已写入 {path}")


if __name__ == "__main__":
    main()