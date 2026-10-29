# -*- coding: utf-8 -*-
"""任务调度：后台线程跑采集，状态供 UI 轮询。"""
import random
import threading
import time
import uuid

from . import classify, config, db, fakefilter, outlier
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
    with _state_lock:
        t = get_task()
        if t and t["status"] == "running":
            t["status"] = "cancelled"
    return True


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

        skip_platform = False
        for g in groups:
            if task["status"] != "running" or skip_platform:
                break
            lo, hi = g["price"]
            for kw in g["enabled_keywords"]:
                if task["status"] != "running" or skip_platform:
                    break
                task["progress"]["current"] = f"{name} · {kw}"
                try:
                    status, message, items = collector.collect(session, platform, kw, (lo, hi))
                except Exception as e:
                    status, message, items = "error", str(e)[:200], []
                if status == "ok":
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
                    for it in items:
                        try:
                            bad, pts, reasons, used = fakefilter.score(it, None)
                        except Exception:
                            bad, pts, reasons = False, 0, []
                        if bad:
                            it["_reject"] = "；".join(reasons[:3])
                            suspect_n += 1
                        else:
                            it["_reject"] = ""

                    # 2) 同档价格离群检测（只对没被 1) 拒掉的做）
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
                    _msg(task, "warn",
                         f"{name} 触发搜索限流（{message}），这个平台剩下的关键词先跳过，"
                         f"{int(config.RATE_LIMIT_COOLDOWN // 60)} 分钟后再跑就不会被拦")
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
                time.sleep(random.uniform(*config.GENTLE_KW_GAP))

        bm.set_active(platform, None)
        try:
            session.close()
        except Exception:
            pass
        # 平台之间歇一会儿，别三个站连着刷
        if config.GENTLE and task["status"] == "running":
            if skip_platform:
                rest = float(config.RATE_LIMIT_COOLDOWN)
                _msg(task, "warn", f"{name} 被限流，冷却 {int(rest // 60)} 分钟再继续下一个平台")
            else:
                rest = random.uniform(*config.GENTLE_PLATFORM_GAP)
                _msg(task, "info", f"歇 {rest:.0f} 秒再切下一个平台（温和模式）")
            time.sleep(rest)

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
    """登录完成后继续采集。若已有任务在跑，排队等它结束再启动。"""
    plats = platforms or [platform]

    def _wait_then_run():
        for _ in range(240):  # 最多等 20 分钟
            cur = current_task()
            if not (cur and cur["status"] == "running"):
                break
            time.sleep(5)
        run_collect(platforms=plats, group_ids=group_ids, source="resume", force_relogin=True)

    threading.Thread(target=_wait_then_run, daemon=True).start()
    return True


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