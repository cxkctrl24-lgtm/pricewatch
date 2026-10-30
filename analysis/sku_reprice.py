# -*- coding: utf-8 -*-
"""详情页 SKU 定价 → 按规格分类拆出「每个分类的真实最低价」。

独立参考实现，不修改 pricewatch 主代码。用途：
  一件挂 15 个 SKU 的商品（横跨 DDR3/DDR4、4G~32G），列表页只暴露最低那个价，
  拿它配标题分类会把「4G DDR3 ¥69」算成「DDR4 8G 最低价」。
  本脚本把每个 SKU 自己归类，让一件商品向多个分类各贡献一个真实价格。

用法：
    python analysis/sku_reprice.py --url <URL> --group ddr4
    python analysis/sku_reprice.py --measure --limit 8      # 从库里挑多规格商品实测
    python analysis/sku_reprice.py --list                   # 只列候选，不联网
    python analysis/sku_reprice.py --json out.json          # 结果另存

设计要点：
  1. **SKU 名自己归类，不用标题归类**。标题写「DDR4 ... 8G16G」，逐字喂给
     classify() 会被 if/elif 链先命中 DDR4，把 DDR3 档也算成 DDR4 —— 这正是错价的来源。
     SKU 名（"4G DDR3 1600【1.35v】"）自带代数+容量，是唯一干净的输入。
  2. SKU 名缺的维度（品牌/台式机/笔记本）从**父标题**继承；代数只在 SKU 名没写时才继承，
     且要求父标题只有一个代数（多代标题不许猜）。
  3. 无规格含义的 SKU（"★【批量购可咨询客服】★" 这类占位档）不产出分类，单独记录。
  4. 节奏：详情页之间 3~6 秒随机间隔，单 Session 复用浏览器（省掉每次 ~3s 启动开销）。
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time

# 本文件在 <repo>/pricewatch/analysis/ 下，回报上级到仓库根，才能 import pricewatch 包
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from pricewatch import config                       # noqa: E402
from pricewatch.classify import classify, category_key   # noqa: E402
from pricewatch.scrapers import detail as detail_mod     # noqa: E402

# Chromium 在受限 TMP 下会 EPERM mkdtemp('...\playwright-artifacts-XXXXXX')；
# Windows 上 TEMP 通常已被系统设成一个不可写目录，setdefault 无效，必须强制覆盖 +
# 清掉 tempfile 的缓存，才能让 playwright 用仓库内的可写目录（只影响本进程）。
_TMP = os.path.join(config.BASE_DIR, "tmp", "playwright")
os.makedirs(_TMP, exist_ok=True)
os.environ["TMP"] = _TMP
os.environ["TEMP"] = _TMP
import tempfile                                              # noqa: E402
tempfile.tempdir = None
assert tempfile.gettempdir() == _TMP or os.access(tempfile.gettempdir(), os.W_OK), \
    f"临时目录不可写: {tempfile.gettempdir()}"

# 详情页之间的节奏（秒），像真人翻页，不连刷
GAP_RANGE = (3.0, 6.0)

# 无规格含义的 SKU 名：占位/引导/客服/运费/赠品
NOISE_RE = re.compile(
    r"客服|咨询|批量|运费|补差|差价|赠品|备注|下单|联系|色卡|选项|不发货|勿拍|勿拍下|测试|套餐[一二三]",
    re.I,
)

# 标题里的多规格信号（和 classify 的口径一致，但这里只看"值不值得进详情页"）
CAP_RE = re.compile(r"(\d{1,3})\s*[Gg][Bb]?")
CAPS_VALID = {2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128}
GEN_PATS = (("DDR5", r"DDR\s*5"), ("DDR4", r"DDR\s*4"),
            ("DDR3", r"DDR\s*3"), ("DDR2", r"DDR\s*2"))


# ---------------------------------------------------------------- 分类核心

def _gens(title):
    return sorted({g for g, p in GEN_PATS if re.search(p, title or "", re.I)})


def _caps(title):
    return sorted({int(x) for x in CAP_RE.findall(title or "") if int(x) in CAPS_VALID})


def needs_detail(title):
    """这件商品的列表价能不能直接代表某一档？不能就值得进详情页。"""
    return len(_caps(title)) >= 2 or len(_gens(title)) >= 2


def sku_spec_key(parent_title, sku_name):
    """把一个 SKU 名 + 父标题 合成 classify() 的输入，返回 (分类键, 分类字典, 说明)。

    关键：**只用 SKU 名做主体**。父标题只在 SKU 名缺维度时补位，
    且代数补位有严格条件（父标题必须是单一代数）。
    """
    name = (sku_name or "").strip()
    parent = (parent_title or "").strip()

    c = classify(name, None)
    notes = []

    # 代数：SKU 名没写才从父标题继承；父标题多代则不猜
    if not c.get("gen"):
        pg = _gens(parent)
        if len(pg) == 1:
            c["gen"] = pg[0]
            notes.append(f"代={pg[0]}(继承)")
        else:
            notes.append("代数无法确定")
            return None, c, "；".join(notes)

    # 容量：SKU 名没写就没法归类（不拿父标题的容量硬套）
    if not c.get("cap"):
        notes.append("容量缺失")
        return None, c, "；".join(notes)

    # 台式/笔记本：SKU 名一般不带，从父标题继承（不参与代数判定，安全）
    if c.get("mem_type") == "通用":
        pt = classify(parent, None).get("mem_type") or "通用"
        if pt != "通用":
            c["mem_type"] = pt
            notes.append(f"机型={pt}(继承)")

    # 品牌从父标题继承（只影响展示标签，不影响比价档位）
    if not c.get("brand"):
        b = classify(parent, None).get("brand")
        if b:
            c["brand"] = b

    key = category_key({"spec": c}, None)
    return key, c, "；".join(notes)


def reprice_from_skus(parent_title, skus, group_id=None):
    """核心出口：{规格分类: {"price": 最低价, "sku": 规格名, ...}}"""
    by_cat = {}
    skipped = []

    for s in skus:
        spec = s.get("spec") or ""
        price = s.get("price")
        if not price or price <= 0:
            continue
        if NOISE_RE.search(spec):
            skipped.append({"spec": spec, "price": price, "why": "占位/非商品 SKU"})
            continue
        key, c, why = sku_spec_key(parent_title, spec)
        if not key or key == "未分类":
            skipped.append({"spec": spec, "price": price, "why": why or "无法归类"})
            continue
        cur = by_cat.get(key)
        if cur is None or price < cur["price"]:
            by_cat[key] = {
                "price": round(float(price), 2),
                "sku": spec,
                "sku_id": s.get("sku_id") or "",
                "gen": c.get("gen"), "cap": c.get("cap"), "count": c.get("count"),
                "freq": c.get("freq"), "mem_type": c.get("mem_type"),
                "brand": c.get("brand"), "note": why,
            }
        # 同一分类下的其他 SKU 记成候选，方便回看"是不是取错了最便宜那档"
        (by_cat[key].setdefault("others", [])).append({"spec": spec, "price": price})

    return by_cat, skipped


# ---------------------------------------------------------------- 抓取

def fetch_one(session, platform, url):
    """打开详情页 → 解析 SKU。返回 dict，含耗时与诊断。"""
    t0 = time.time()
    page = session.page()
    info = {"url": url, "platform": platform, "sku_count": 0, "skus": [],
            "title": "", "status": "ok", "message": "", "elapsed_s": 0.0,
            "blocked": False}

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
    except Exception as e:
        info.update(status="error", message=f"打开失败: {str(e)[:120]}")
        info["elapsed_s"] = round(time.time() - t0, 2)
        return info

    # SKU JSON 是 SSR 内嵌的：domcontentloaded 后 2 秒它一定在 HTML 里。
    # **只取一次 content()** —— 反复序列化 500KB+ 的 DOM 会把 Edge 的渲染进程干崩，
    # 崩了之后同一 Session 里后续每条都 "Target page ... has been closed"。
    page.wait_for_timeout(2200)
    try:
        html = page.content()
    except Exception as e:
        info.update(status="error", message=f"取页面失败: {str(e)[:120]}")
        info["elapsed_s"] = round(time.time() - t0, 2)
        return info
    parser = detail_mod.PARSERS.get(platform)
    props, skus = parser(html) if parser else ([], [])

    m = re.search(r"<title>([^<]{2,160})</title>", html)
    info["title"] = (m.group(1).strip() if m else "") or (page.title() or "")
    info["final_url"] = page.url

    # 风控判定：baxia / umidtoken 是所有详情页都带的常规安全 SDK，不能当拦截信号；
    # 真正的拦截信号是「滑块/验证码落地页」「x5sec / _____tmd_____ cookie 墙」
    # 或「标题变成验证/访问受限」。SKU 解析成功就不报。
    low = html.lower()
    if not skus:
        hit = ("x5sec" in low or "_____tmd_____" in low or "//sufei." in low
               or "滑块验证" in html or "nc_1_n1z" in low)
        title_low = (info.get("title") or "").lower()
        if "验证" in title_low or "captcha" in title_low or "punish" in low:
            hit = True
        info["blocked"] = hit
        if hit:
            info["message"] = "疑似被风控拦截（滑块/验证码页），未取到 SKU"

    info["skus"] = skus
    info["sku_count"] = len(skus)
    info["prop_groups"] = [p.get("name") for p in props] if props else []
    if not skus:
        info["status"] = "no_sku"
        info["message"] = info["message"] or "解析不到 SKU（可能单规格商品或未登录/被拦）"
    info["elapsed_s"] = round(time.time() - t0, 2)
    return info


def _profile_hygiene(platform):
    """启动前的 profile 卫生。

    browser.py 只在 Edge **启动失败**时才清僵尸锁/杀残留进程；但真实故障模式是
    Edge 启动"成功"、连上的却是上一轮没退干净的残留实例，于是 page.goto 当场
    "Target page ... has been closed"。所以这里在每次起 Session 之前主动做一遍。
    """
    from pricewatch import browser as browser_mod
    try:
        p = browser_mod.profile_path(platform)
        killed = browser_mod._kill_profile_holders(p)
        locks = browser_mod._clean_profile_locks(p)
        return killed, locks
    except Exception:
        return 0, 0


def run_batch(items, platform="taobao", headless=True, gap=GAP_RANGE,
              recycle_every=1, retry=1, verbose=True):
    """items: [(url, group_id, label)]。

    **默认每条商品一个全新 Session（recycle_every=1）** —— 实测结论：
    同一个浏览器实例连续渲染到第 2~3 个淘宝详情页时，渲染进程必崩
    （page.goto / new_page 直接 "Target page, context or browser has been closed"）。
    每 Session 一条是最稳的形态，代价只是每条多 ~1.5s 的浏览器启动。
    recycle_every=2 可以省一半启动开销，但要靠 retry 兜底。
    """
    from pricewatch.browser import Session

    results = []
    session = None
    try:
        for i, (url, group_id, label) in enumerate(items):
            if i and gap:
                d = random.uniform(*gap)
                if verbose:
                    print(f"  …等待 {d:.1f}s", flush=True)
                time.sleep(d)

            if session is not None and i % recycle_every == 0:
                try:
                    session.close()
                except Exception:
                    pass
                session = None
                if verbose:
                    print("  [重建浏览器会话]", flush=True)
                time.sleep(random.uniform(1.5, 2.5))

            attempt = 0
            while True:
                attempt += 1
                if session is None:
                    k, lk = _profile_hygiene(platform)
                    if verbose and (k or lk):
                        print(f"  [清 profile] 杀残留 {k} / 清锁 {lk}", flush=True)
                    session = Session(platform, headless=headless)
                    session.__enter__()
                    if not session.has_login_cookie():
                        raise RuntimeError(f"{platform} 无登录态（cookie 里找不到身份标记）")
                if verbose:
                    print(f"[{i + 1}/{len(items)}] {label or url[:60]}", flush=True)
                try:
                    raw = fetch_one(session, platform, url)
                except Exception as e:
                    raw = {"url": url, "status": "error", "message": str(e)[:150],
                           "skus": [], "title": "", "sku_count": 0, "elapsed_s": 0.0}

                crashed = "has been closed" in (raw.get("message") or "")
                if crashed and attempt <= retry:
                    # 浏览器已死：重建再试一次同一条（不计入正常节奏）
                    try:
                        session.close()
                    except Exception:
                        pass
                    session = None
                    if verbose:
                        print("  [浏览器崩溃，重建后重试本条]", flush=True)
                    time.sleep(random.uniform(2.0, 3.5))
                    continue
                break

            cats, skipped = (reprice_from_skus(raw.get("title") or "", raw["skus"], group_id)
                             if raw.get("skus") else ({}, []))
            out = {
                "label": label, "group": group_id,
                "title": raw.get("title"),
                "url": url,
                "status": raw["status"],
                "message": raw.get("message", ""),
                "blocked": raw.get("blocked", False),
                "elapsed_s": raw.get("elapsed_s", 0),
                "sku_count": raw.get("sku_count", 0),
                "attempts": attempt,
                "distinct_categories": len(cats),
                "categories": {k: {kk: vv for kk, vv in v.items() if kk != "others"}
                               for k, v in sorted(cats.items(), key=lambda x: x[1]["price"])},
                "other_skus": {k: v.get("others", []) for k, v in cats.items()},
                "skipped_skus": skipped,
                "raw_skus": raw.get("skus", []),
            }
            results.append(out)
            if verbose:
                print(f"    {out['status']}  {out['sku_count']} SKU → "
                      f"{out['distinct_categories']} 分类  {out['elapsed_s']}s"
                      f"{'  [风控]' if out['blocked'] else ''}"
                      f"{'  (重试 %d)' % attempt if attempt > 1 else ''}", flush=True)
                for k, v in out["categories"].items():
                    print(f"      {k:<22} ¥{v['price']:>9.2f}   ← {v['sku']}", flush=True)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    return results


# ---------------------------------------------------------------- 数据源

def db_candidates(limit=8, only_cheap=True):
    """从库里挑标题明显多规格的商品（列表价越低越可疑）。"""
    dbp = config.DB_PATH
    conn = sqlite3.connect(dbp)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT id, platform, title, price, url FROM products ORDER BY price ASC"))
    conn.close()
    picks = [r for r in rows if needs_detail(r["title"])]
    if only_cheap:
        picks.sort(key=lambda r: r["price"])
    return picks[:limit]


def group_of(title):
    g = _gens(title)
    if "DDR5" in g: return "ddr5"
    if "DDR4" in g: return "ddr4"
    return None


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--group", default="ddr4")
    ap.add_argument("--measure", action="store_true", help="从库里挑多规格商品实测")
    ap.add_argument("--list", action="store_true", help="只列候选，不联网")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--headless", dest="headless", action="store_true", default=True)
    ap.add_argument("--no-headless", dest="headless", action="store_false")
    ap.add_argument("--gap", default="3,6", help="详情页间隔秒数，如 3,6")
    a = ap.parse_args()

    gap = tuple(float(x) for x in a.gap.split(","))

    if a.list:
        for r in db_candidates(a.limit):
            print(f"{r['id']:>4} ¥{r['price']:>8.0f} caps={_caps(r['title'])} "
                  f"gens={_gens(r['title'])} {r['title'][:44]}", flush=True)
        print(f"\n库内总数 236 中，多容量/多代标题命中上述候选；"
              f"完整统计见 analysis/sku_design.md", flush=True)
        return

    if a.measure:
        picks = db_candidates(a.limit)
        items = [(r["url"], group_of(r["title"]) or a.group,
                  f"#{r['id']} ¥{r['price']:.0f}") for r in picks]
        results = run_batch(items, headless=a.headless, gap=gap)
        _summary(results)
    elif a.url:
        results = run_batch([(a.url, a.group, "single")], headless=a.headless)
        _summary(results)
    else:
        ap.print_help()
        return

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n已写入 {a.json_out}", flush=True)


def _summary(results):
    ok = [r for r in results if r["status"] == "ok"]
    times = [r["elapsed_s"] for r in results if r["elapsed_s"]]
    print("\n=== 汇总 ===", flush=True)
    print(f"成功 {len(ok)}/{len(results)}"
          f"，平均单条 {sum(times) / len(times):.1f}s" if times else "无耗时数据", flush=True)
    print(f"SKU 总数 {sum(r['sku_count'] for r in results)}，"
          f"产出分类 {sum(r['distinct_categories'] for r in results)} 个", flush=True)
    for r in results:
        if r["status"] != "ok":
            print(f"  失败: {r['url'][:70]} → {r['status']} {r['message'][:80]}", flush=True)


if __name__ == "__main__":
    main()
