# -*- coding: utf-8 -*-
"""任务调度：后台线程跑采集，状态供 UI 轮询。"""
import random
import threading
import time
import uuid

from . import classify, config, cooldown, db, fakefilter, outlier, sku_cache
from .browser import Session, bm
from .scrapers import collector

_state_lock = threading.RLock()
_tasks = {}
_current = {"task_id": None}
_last_auto = {"date": None}


def _new_task(platforms, group_ids, source="manual"):
    task = {
        "id": uuid.uuid4().hex[:10],
        "status": "running",          # running / done / need_login / error / cancelled
        "source": source,
        "platforms": list(platforms),
        "groups": list(group_ids),
        "progress": {"done": 0, "total": 0, "current": ""},
        "items": 0,
        "new_items": 0,
        "need_login": [],             # 平台列表：需要用户去登录
        "messages": [],               # (level, text)
        "started": time.time(),
        "finished": None,
    }
    with _state_lock:
        _tasks[task["id"]] = task
        _current["task_id"] = task["id"]
        # 只保留最近 30 个任务的内存状态
        if len(_tasks) > 30:
            old = sorted(_tasks.items(), key=lambda kv: kv[1]["started"])[:-30]
            for k, _ in old:
                _tasks.pop(k, None)
    return task


def get_task(task_id=None):
    with _state_lock:
        tid = task_id or _current["task_id"]
        if not tid:
            return None
        return _tasks.get(tid)


def current_task():
    return get_task()


def cancel():
    """取消当前任务。

    只改标志位是不够的 —— 采集线程可能正卡在详情页抓取（单条 1~5 秒）
    或 sleep 间隔里，标志位要等到下一次检查才生效，用户会觉得「取消没反应」。
    这里把标志位设成 cancelled，并且让所有等待点都用 _sleep_unless_cancelled()
    轮询，几百毫秒内就能响应。
    """
    with _state_lock:
        t = get_task()
        if t and t["status"] == "running":
            t["status"] = "cancelled"
    return True


def _is_cancelled(task=None):
    """当前任务是否已被取消。task 传 None 时取当前任务。"""
    t = task if task is not None else get_task()
    try:
        return bool(t) and t.get("status") == "cancelled"
    except Exception:
        return False


def _sleep_unless_cancelled(seconds, task=None, step=0.25):
    """可被取消打断的 sleep。

    把一整段 sleep 切成 step 小片，每片检查一次取消标志。
    返回 True 表示「中途被取消，调用方应立刻收手」。
    """
    remaining = float(seconds or 0)
    while remaining > 0:
        if _is_cancelled(task):
            return True
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return _is_cancelled(task)


def _set_login_needed(task, platform):
    if platform not in task["need_login"]:
        task["need_login"].append(platform)


def _msg(task, level, text):
    task["messages"].append((level, text))
    if len(task["messages"]) > 200:
        task["messages"] = task["messages"][-200:]
    db.log(level, text)


def _prepare_groups(group_ids):
    groups = db.keyword_groups()
    if group_ids:
        groups = [g for g in groups if g["id"] in group_ids]
    return [g for g in groups if g["enabled_keywords"]]


def _fetch_detail_with_deadline(session, platform, url, timeout_ms, platform_name="平台"):
    """在独立线程里读详情页，超过 (timeout_ms + 安全余量) 就放弃。

    返回 payload dict 或 None。

    为什么要线程：淘宝详情页被风控挂起时，Playwright 的 page.goto 不按
    timeout 参数返回（实测卡 430 秒），主线程调用会把整轮采集拖死。
    用 daemon 线程 + join 超时，到点就放弃这一次读取继续往下走；
    被遗弃的线程拿不到结果，但它结束时会把 session 关掉，不会泄漏到主流程。

    注意：这里**新建一个 Session**给子线程用，不复用主采集线程的 session ——
    Playwright 对象绑定创建它的线程，跨线程用会直接报 "Playwright connection closed"。
    **但绝不能用同一个 profile 目录**：子线程开浏览器会跟主采集线程抢
    Chromium 单实例锁，实测直接打架（"已清理 9 个占用该 profile 的残留浏览器进程"，
    然后主线程的采集被顶掉、整轮卡死）。这里建一个临时 profile，
    用完就删 —— 详情页只看价格，不需要主 profile 里的登录 cookie 以外的状态。
    """
    import os
    import shutil
    import tempfile
    import threading

    from . import config as _cfg
    from .browser import Session
    from .scrapers import detail as detail_mod

    deadline = (int(timeout_ms) + 8000) / 1000.0      # 给 8 秒收尾余量
    box = {"payload": None, "err": None}

    # 临时 profile 目录（放在包内 tmp 下，规避系统 Temp 的 EPERM）
    tmp_profile = None
    try:
        base_tmp = os.path.join(_cfg.BASE_DIR, "tmp")
        os.makedirs(base_tmp, exist_ok=True)
        tmp_profile = tempfile.mkdtemp(prefix=f"skuprofile-{platform}-", dir=base_tmp)
    except Exception:
        tmp_profile = None

    def _worker():
        s2 = None
        try:
            if tmp_profile:
                s2 = Session(platform, headless=True, profile_dir=tmp_profile).__enter__()
                # 关键：临时 profile 是全新的，**没有登录 cookie**。
                # 淘宝/京东详情页要登录才能看完整 SKU，所以把主 profile 的
                # cookie 复制过来（只复制 cookie，不共享目录 —— 共享目录会抢锁）。
                try:
                    for c in session.ctx.cookies():
                        try:
                            s2.ctx.add_cookies([{
                                "name": c.get("name"),
                                "value": c.get("value"),
                                "domain": c.get("domain"),
                                "path": c.get("path") or "/",
                            }])
                        except Exception:
                            continue
                except Exception as e:
                    db.log("warn", f"[SKU校正] 复制登录 cookie 失败: {str(e)[:80]}")
            else:
                s2 = Session(platform, headless=True).__enter__()
            _st, _msg, payload = detail_mod.fetch_sku_prices(
                s2, platform, url, timeout_ms=timeout_ms)
            box["payload"] = payload or {}
        except Exception as e:
            box["err"] = str(e)[:120]
        finally:
            try:
                if s2 is not None:
                    s2.close()
            except Exception:
                pass

    th = threading.Thread(target=_worker, daemon=True,
                          name=f"sku-detail-{platform}")
    th.start()
    th.join(timeout=deadline)
    alive = th.is_alive()
    if alive:
        db.log("warn", f"[SKU校正] {platform_name}详情页超过 {int(deadline)} 秒仍无响应，放弃本次读取")
    if not alive and box["err"]:
        db.log("warn", f"[SKU校正] 详情页读取异常: {box['err'][:90]}")
    elif box["err"] and alive:
        db.log("warn", f"[SKU校正] 详情页读取异常(已放弃线程): {box['err'][:80]}")

    # 清理临时 profile（线程还活着时不动它，避免删正在用的目录）
    if tmp_profile and not alive:
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass
    if alive:
        return None
    return box["payload"]


def _reprice_multi_spec(session, platform, items, group, task):
    """对「多容量 / 跨代」的商品补读详情页，用 SKU 真实价重定这一条。

    为什么必须这样做：
      一件商品挂 15 个 SKU、横跨 DDR3/DDR4 与 4G~32G，列表页只显示最低那个价。
      拿它配标题分类，会把「4G DDR3 ¥69」算成「DDR4 8G 最低价」（实测踩过）。
      读详情页拿到完整 SKU 表后，把每个 SKU **自己**归类，就能知道
      这件商品的 DDR4 8G 档真实是 ¥339，而不是 ¥69。

    策略（考虑风控，详情页比列表页重得多）：
      · 只对 needs_detail() 为真的商品做（多容量或多代），其余照列表价
      · 每个关键词最多补读 SKU_REPRICE_MAX_PER_KEYWORD 件
      · 件间随机间隔，像真人翻页
      · 读失败/被风控就直接放弃，保持列表价 —— 宁可用旧价，也不要编造

    items 会被就地修改（price/spec_key/_spec），返回被成功改写的条数。
    """
    from . import repricing  # 延迟导入，避免影响未启用该功能时的启动速度

    gid = group["id"]
    candidates = []
    for it in items:
        if it.get("_reject"):
            continue
        if not repricing.needs_detail(it.get("title")):
            continue
        candidates.append(it)

    if not candidates:
        return 0
    # 优先级：越可疑越先校正。详情页很重，每轮只补读很小一批（默认 12 件），
    # 所以要先把「最可能把低价算错档位」的挑出来。
    # 判定：标题挂了多个容量/多代，且当前价格落在该组基准下界附近或以下 ——
    # 这种条目的列表价大概率属于最小那档，不是标题强调的那档。
    def _suspicion(x):
        score = 0
        try:
            spec = x.get("_spec") or {}
            if spec.get("multi_cap"):
                score += 2
            if spec.get("multi_gen"):
                score += 3
            base = outlier._baseline(spec)
            price = x.get("price") or 0
            if base and price:
                lo, hi = base
                # 贴着下界或低于下界 → 高度可疑
                if price <= lo * 1.15:
                    score += 4
                elif price <= lo * 1.6:
                    score += 2
            # 容量与价格明显不符（8G 类里出现接近 4G 的价格）
            caps = spec.get("caps_all") or []
            if len(caps) >= 2 and price and base:
                score += 1
        except Exception:
            pass
        return -score          # 负数用于升序排序 = 分数高的排前面

    candidates.sort(key=_suspicion)
    limit = int(getattr(config, "SKU_REPRICE_MAX_PER_KEYWORD", 8))
    # 只保留真正可疑的（score>0），别为了"校正"去跑一堆本来就对价的商品
    candidates = [c for c in candidates if _suspicion(c) < 0][:limit]

    budget = float(getattr(config, "SKU_REPRICE_BUDGET", 90))
    detail_timeout = int(getattr(config, "SKU_DETAIL_TIMEOUT_MS", 15000))
    started = time.time()

    fixed = 0
    tried = 0
    cache_hit = 0
    from .scrapers import detail as detail_mod
    for it in candidates:
        # 用户点了取消就立刻收手 —— 详情页一轮要跑几十秒，
        # 不检查的话「取消」看起来要等一整轮才生效。
        if _is_cancelled(task):
            db.log("info", "[SKU校正] 收到取消，停止补读详情页")
            break
        # 总时间预算：详情页可能被风控卡到超时，不能让一件商品
        # 把整轮采集拖住。超预算就直接收手，用列表价。
        if time.time() - started > budget:
            db.log("info", f"[SKU校正] 已用满 {int(budget)} 秒预算，剩下的下一轮再校正")
            break
        url = it.get("url") or ""
        if not url:
            continue
        ikey = sku_cache.item_key_of(url)

        # 先查缓存：同一件商品 45 分钟内的 SKU 表基本不变，
        # 命中就完全不用开详情页（这正是详情页挂起问题的根治办法）。
        skus = sku_cache.get(platform, ikey)
        if skus:
            cache_hit += 1
            db.log("info", f"[SKU校正] 命中缓存，跳过详情页: {it.get('title', '')[:24]}")
        else:
            tried += 1
            # 详情页读取放进**独立线程**跑，外面套一个硬超时。
            # 为什么必须这么做：淘宝详情页被风控时会挂起，Playwright 的
            # page.goto 即使传了 timeout 也不按它返回（实测卡 430 秒）。
            # 主线程直接调它 = 整轮采集被一件事拖死。
            # 放到 daemon 线程里，join 到点就放弃这个线程继续往下走 ——
            # 卡住的线程拿不到结果也不会影响主流程（进程退出时自动清理）。
            payload = _fetch_detail_with_deadline(
                session, platform, url, detail_timeout,
                platform_name=config.PLATFORMS.get(platform, {}).get("name", platform))
            if payload is None:
                db.log("warn", f"[SKU校正] 详情页超时/失败，保留列表价: {it.get('title', '')[:22]}")
                continue
            skus = (payload or {}).get("sku_prices") or []
            if skus:
                # 被风控的页面 SKU 可能不完整 → put() 内部会拒绝写入
                blocked = bool((payload or {}).get("blocked"))
                if not sku_cache.put(platform, ikey, skus,
                                     title=it.get("title") or "", blocked=blocked):
                    db.log("info", "[SKU校正] 页面疑似被风控，不缓存这份 SKU")
        if not skus:
            continue
        try:
            by_cat, _skipped = repricing.reprice_from_skus(it.get("title"), skus)
            key, info = repricing.best_for_group(by_cat, gid)
        except Exception as e:
            db.log("warn", f"[SKU校正] 归类失败，保留列表价: {str(e)[:90]}")
            continue
        if not key or not info:
            continue

        old_price = it.get("price")
        new_price = info["price"]
        # 只有当「该组真实最低档」和列表价不是同一档时才改写。
        # 同一档说明列表价本来就对（或者只有一档），不用动。
        if abs(new_price - (old_price or 0)) < 0.01 and key == it.get("spec_key"):
            continue

        it["price"] = new_price
        it["spec_key"] = key
        try:
            spec = classify.classify(info.get("sku") or "", gid)
            spec["gen"] = info.get("gen") or spec.get("gen")
            spec["cap"] = info.get("cap") if info.get("cap") else spec.get("cap")
            spec["count"] = info.get("count") or spec.get("count")
            spec["mem_type"] = info.get("mem_type") or spec.get("mem_type")
            spec["condition"] = it.get("condition") or "new"
            it["_spec"] = spec
            it["spec_label"] = classify.detail_key(it, group_id=gid)
        except Exception:
            pass
        it["_reprice_note"] = (f"列表价 ¥{old_price:.0f} 对应的是其他规格档；"
                               f"本档真实价 ¥{new_price:.0f}（{info.get('sku', '')[:26]}）")
        fixed += 1
        db.log("info", f"[SKU校正] {it.get('title', '')[:26]}… "
                    f"¥{old_price:.0f} → ¥{new_price:.0f}（{key}）")
        if _sleep_unless_cancelled(
                random.uniform(*getattr(config, "SKU_REPRICE_GAP", (3.0, 6.0))), task):
            db.log("info", "[SKU校正] 收到取消，停止补读详情页")
            break

    if tried or cache_hit:
        bits = []
        if cache_hit:
            bits.append(f"缓存命中 {cache_hit}")
        if tried:
            bits.append(f"实读 {tried}")
        if task is not None:
            _msg(task, "info",
                 f"[SKU校正] {'，'.join(bits)} 件，修正 {fixed} 件价格档位"
                 f"（用时 {int(time.time() - started)} 秒）")
    return fixed


def run_collect(platforms=None, group_ids=None, source="manual", force_relogin=False):
    """后台线程入口。立即返回 task dict。已有任务在跑时不重复启动。"""
    cur = current_task()
    if cur and cur["status"] == "running":
        return cur
    platforms = platforms or list(config.PLATFORMS.keys())
    task = _new_task(platforms, group_ids or [], source)
    th = threading.Thread(target=_run, args=(task, platforms, group_ids, force_relogin), daemon=True)
    th.start()
    return task


def _run(task, platforms, group_ids, force_relogin):
    groups = _prepare_groups(group_ids)
    total = 0
    for p in platforms:
        for g in groups:
            total += len(g["enabled_keywords"])
    task["progress"]["total"] = total

    for platform in platforms:
        if task["status"] != "running":
            break
        name = config.PLATFORMS[platform]["name"]
        skipped = sum(len(g["enabled_keywords"]) for g in groups)

        # 每个平台开一个自包含的浏览器会话（Playwright 对象不能跨线程）
        session = None
        try:
            bm.set_active(platform, "scrape")
            session = Session(platform, headless=config.HEADLESS).__enter__()
        except Exception as e:
            bm.set_active(platform, None)
            _msg(task, "error", f"{name} 浏览器启动失败: {str(e)[:160]}")
            db.record_snapshot(task["id"], platform, None, "error", str(e)[:160], 0)
            task["progress"]["done"] += skipped
            continue

        if not session.has_login_cookie():
            bm.login_flags[platform] = False
            bm.set_active(platform, None)
            try:
                session.close()
            except Exception:
                pass
            _set_login_needed(task, platform)
            db.record_snapshot(task["id"], platform, None, "need_login", "需要登录", 0)
            _msg(task, "warn", f"{name} 还没登录 —— 点上面「登录 / 扫码」，扫完自动继续")
            task["progress"]["done"] += skipped
            continue
        bm.login_flags[platform] = True

        # 这个平台还在限流冷却期就别再去撞 —— 越撞锁得越久
        left = cooldown.remaining(platform)
        if left > 0:
            mins = int(left // 60) + 1
            _msg(task, "warn",
                 f"{name} 还在限流冷却中（约 {mins} 分钟后解除，"
                 f"已连续被限 {cooldown.hits(platform)} 次），本次先跳过")
            db.record_snapshot(task["id"], platform, None, "cooldown",
                               f"冷却中 {mins} 分钟", 0)
            task["progress"]["done"] += skipped
            task.setdefault("cooldown", []).append(platform)
            try:
                session.close()
            except Exception:
                pass
            continue

        skip_platform = False
        for g in groups:
            if task["status"] != "running" or skip_platform:
                break
            price_lo, price_hi = g["price"]
            for kw in g["enabled_keywords"]:
                if task["status"] != "running" or skip_platform:
                    break
                task["progress"]["current"] = f"{name} · {kw}"
                try:
                    status, message, items = collector.collect(session, platform, kw, (price_lo, price_hi))
                except Exception as e:
                    status, message, items = "error", str(e)[:200], []
                if status == "ok":
                    # 这一轮采成了 —— 该平台的限流冷却清零，退避重来
                    cooldown.clear(platform)
                    # ===== 三级过滤：可疑丢弃 / 异常标记 / 正常收录 =====
                    kept = []
                    suspect_n = 0
                    review_n = 0

                    # 先按分类分组，用同组价格做基准
                    grouped = {}
                    for it in items:
                        try:
                            it["spec_key"] = classify.category_key(it, group_id=g["id"])
                            it["spec_label"] = classify.detail_key(it, group_id=g["id"])
                            spec = classify.classify(it.get("title"), g["id"])
                        except Exception:
                            it["spec_key"] = "未分类"
                            spec = {}
                        it["condition"] = spec.get("condition") or "new"
                        it["_spec"] = spec
                        grouped.setdefault((it["spec_key"], it["condition"]), []).append(it)

                    # 1) 假货指纹判定（标题堆砌 / 非商品词 / 通用店名 / 无品牌）
                    #    group_id 传进去，才能识别"搜 DDR4 却混进 DDR3"这类跨代混淆
                    for it in items:
                        try:
                            bad, pts, reasons, used = fakefilter.score(it, None, group_id=g["id"])
                        except Exception:
                            bad, pts, reasons = False, 0, []
                        if bad:
                            it["_reject"] = "；".join(reasons[:3])
                            suspect_n += 1
                        else:
                            it["_reject"] = ""

                    # 2) 详情页 SKU 校正：列表价只代表「多规格里最便宜那一档」，
                    #      对这种商品去详情页读真实 SKU，用它各档的真实价重定。
                    #      实测案例：¥68 的「DDR4 8G」其实是同一件商品里的
                    #      「4G DDR3 ¥69」；它真正的 DDR4 8G 那档是 ¥339。
                    #      放在假货过滤之后，避免为已判假的商品白开详情页。
                    if getattr(config, "SKU_REPRICE", False):
                        if _reprice_multi_spec(session, platform, items, g, task):
                            # 价格/分类被改写，重建分组
                            grouped = {}
                            for it in items:
                                if it.get("_reject"):
                                    continue
                                grouped.setdefault(
                                    (it["spec_key"], it["condition"]), []).append(it)

                    # 3) 同档价格离群检测（只对没被 1) 拒掉的做）
                    for key, group_items in grouped.items():
                        cat, cond = key
                        peers_new = [x["price"] for x in grouped.get((cat, "new"), [])]
                        for it in group_items:
                            if it["_reject"]:
                                continue
                            try:
                                lvl, why, lo, hi = outlier.judge(
                                    it["price"], it["_spec"], peers_new, cond == "used")
                            except Exception:
                                lvl, why = "ok", ""
                            it["price_level"] = "review" if lvl == "review" else "ok"
                            it["price_note"] = why if lvl == "review" else ""
                            if lvl == "review":
                                review_n += 1
                            elif lvl == "reject":
                                it["_reject"] = why

                    # 3) 落库：拒掉的不入库，只留档
                    for it in items:
                        if it.get("_reject"):
                            db.record_rejected(platform, kw, it, it["_reject"])
                        else:
                            it.pop("_spec", None)
                            kept.append(it)

                    items = kept
                    new_n = db.record_items(task["id"], platform, kw, items)
                    task["items"] += len(items)
                    task["new_items"] += new_n
                    db.record_snapshot(task["id"], platform, kw, "ok", message, len(items))
                    bits = [f"{len(items)} 件收录"]
                    if suspect_n:
                        bits.append(f"丢弃可疑 {suspect_n}")
                    if review_n:
                        bits.append(f"标记异常 {review_n}")
                    _msg(task, "ok", f"{name} · {kw}: " + "，".join(bits) + f"（新增 {new_n}）")
                    task["filtered"] = task.get("filtered", 0) + suspect_n
                    task["reviewed"] = task.get("reviewed", 0) + review_n
                elif status == "need_login":
                    _set_login_needed(task, platform)
                    bm.login_flags[platform] = False
                    db.record_snapshot(task["id"], platform, kw, "need_login", message, 0)
                    _msg(task, "warn", f"{name} · {kw}: 被要求登录，去登录后重跑这个平台")
                elif status == "rate_limited":
                    db.record_snapshot(task["id"], platform, kw, "rate_limited", message, 0)
                    _wait = cooldown.record_limit(platform)
                    _hits = cooldown.hits(platform)
                    _msg(task, "warn",
                         f"{name} 触发搜索限流（{message}），这个平台剩下的关键词先跳过。"
                         f"本次冷却 {int(_wait // 60)} 分钟（连续第 {_hits} 次被限，"
                         f"冷却自动翻倍，采成一次就清零）")
                    # 剩下的关键词全部记为跳过，避免继续撞墙把限流时间拉长
                    remaining = len(g["enabled_keywords"]) - g["enabled_keywords"].index(kw) - 1
                    for _ in range(remaining):
                        task["progress"]["done"] += 1
                    task["rate_limited"] = task.get("rate_limited", [])
                    if platform not in task["rate_limited"]:
                        task["rate_limited"].append(platform)
                    skip_platform = True
                else:
                    db.record_snapshot(task["id"], platform, kw, "error", message, 0)
                    _msg(task, "error", f"{name} · {kw}: {message}")
                task["progress"]["done"] += 1
                # 可被取消打断的间隔等待
                if _sleep_unless_cancelled(
                        random.uniform(*config.pacing(platform)["kw_gap"]), task):
                    _msg(task, "info", "收到取消，停止当前任务")
                    break

        bm.set_active(platform, None)
        try:
            session.close()
        except Exception:
            pass
        # 平台之间歇一会儿，别三个站连着刷
        if config.GENTLE and task["status"] == "running":
            if skip_platform:
                # 不再傻等整个冷却窗口（京东可能锁半小时以上），只提示，
                # 真正的等待由 cooldown.json 跨会话负责，下次采集会自动跳过。
                left = cooldown.remaining(platform)
                if left > 0:
                    _msg(task, "warn",
                         f"{name} 被限流，冷却约 {int(left // 60) + 1} 分钟。"
                         f"不用干等，直接换下一个平台 / 稍后再点采集即可")
                rest = min(float(left), 30.0) if left > 0 else 0.0
            else:
                rest = random.uniform(*config.GENTLE_PLATFORM_GAP)
                _msg(task, "info", f"歇 {rest:.0f} 秒再切下一个平台（温和模式）")
            if _sleep_unless_cancelled(rest, task):
                _msg(task, "info", "收到取消，不再切换下一个平台")

    with _state_lock:
        if task["status"] == "running":
            if task["need_login"] and task["items"] == 0:
                task["status"] = "need_login"
            else:
                task["status"] = "done"
        task["finished"] = time.time()
        task["progress"]["current"] = ""
    _msg(task, "info", f"任务结束：共 {task['items']} 条，新增 {task['new_items']} 条")


def resume_after_login(platform, group_ids=None, platforms=None):
    """已停用：登录成功后不再自动开始采集。

    早期行为是「扫码登录成功 → 立刻排队起一轮采集」。这带来三个问题：
      1. 新采集 Session 会抢同一个 profile 的 Chromium 单实例锁，
         把用户刚打开的登录窗口顶掉（"顶掉了登录页面"）；
      2. 用户只是想登录，并没有要求采集；
      3. 采集一起，取消按钮还要等这一整轮跑完才生效。

    现在保留这个函数只为兼容旧调用点：它什么都不做，直接返回 False。
    要采集请由用户在界面点「开始采集」。
    """
    db.log("info", f"{config.PLATFORMS.get(platform, {}).get('name', platform)} "
                   f"登录完成；不会自动开始采集，请手动点「开始采集」")
    return False


# ---------------- 每日定时 ----------------
def _auto_loop():
    last_day = None
    while True:
        try:
            if config.SCHEDULE_ENABLED:
                now = time.localtime()
                hhmm = f"{now.tm_hour:02d}:{now.tm_min:02d}"
                day = time.strftime("%Y-%m-%d", now)
                if hhmm == config.SCHEDULE_TIME and last_day != day:
                    running = current_task()
                    if not (running and running["status"] == "running"):
                        last_day = day
                        db.log("info", "定时采集触发")
                        run_collect(platforms=config.SCHEDULE_PLATFORMS, source="schedule")
        except Exception as e:
            db.log("error", f"定时器异常: {e}")
        time.sleep(25)


def start_scheduler():
    th = threading.Thread(target=_auto_loop, daemon=True)
    th.start()