# -*- coding: utf-8 -*-
"""本地图形界面（Flask + 浏览器）。"""
import atexit
import csv
import io
import threading
import time
import webbrowser

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from . import config, db, runner
from .browser import Session, bm, login_flow
from .scrapers import detail as detail_mod

app = Flask(__name__, template_folder="templates", static_folder="static")

PLATFORMS = config.PLATFORMS


@app.route("/")
def index():
    groups = db.keyword_groups()
    platforms = [
        {
            "id": k,
            "name": v["name"],
            "mobile": v["mobile"],
            "login_hint": v.get("login_hint", "扫码登录（手机 App 扫窗口里的二维码）"),
            "login_mode": v.get("login_mode", "qrcode"),
        }
        for k, v in PLATFORMS.items()
    ]
    return render_template(
        "index.html",
        groups=groups,
        platforms=platforms,
        schedule_time=config.SCHEDULE_TIME,
        gentle=getattr(config, "GENTLE", True),
        schedule_enabled=config.SCHEDULE_ENABLED,
        db_path=config.DB_PATH,
    )


@app.route("/api/state")
def api_state():
    task = runner.current_task()
    if task:
        task = {k: v for k, v in task.items() if k != "messages"}
        task["messages"] = runner.current_task()["messages"][-60:] if runner.current_task() else []
    flags = {p: bm.login_flags.get(p, None) for p in PLATFORMS}
    active = bm.snapshot_active()
    return jsonify({
        "ok": True,
        "task": task,
        "login": flags,
        "active": active,
        "stats": db.stats(),
        "last": db.last_snapshots(),
        "now": time.time(),
    })


@app.route("/api/collect", methods=["POST"])
def api_collect():
    data = request.get_json(silent=True) or {}
    platforms = data.get("platforms") or list(PLATFORMS.keys())
    platforms = [p for p in platforms if p in PLATFORMS]
    groups = data.get("groups") or []
    cur = runner.current_task()
    if cur and cur["status"] == "running":
        return jsonify({"ok": False, "message": "已有采集任务在跑，等它结束或先取消"})
    task = runner.run_collect(platforms=platforms, group_ids=groups, source="manual")
    return jsonify({"ok": True, "task_id": task["id"]})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    runner.cancel()
    return jsonify({"ok": True})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    platform = data.get("platform")
    if platform not in PLATFORMS:
        return jsonify({"ok": False, "message": "未知平台"})
    groups = data.get("groups") or []

    def _open():
        name = PLATFORMS[platform]["name"]
        db.log("info", f"正在打开 {name} 登录窗口…")
        ok = login_flow(
            platform,
            timeout_s=600,
            interval_s=3.0,
            on_success=lambda: runner.resume_after_login(platform, group_ids=groups),
        )
        db.log("info", f"{name} 登录{'成功，已保存登录态' if ok else '未成功'}")

    threading.Thread(target=_open, daemon=True).start()
    return jsonify({"ok": True, "message": "正在打开登录窗口，请稍等几秒"})


@app.route("/api/check_login", methods=["POST"])
def api_check_login():
    data = request.get_json(silent=True) or {}
    platform = data.get("platform")
    groups = data.get("groups") or []
    if platform not in PLATFORMS:
        return jsonify({"ok": False, "message": "未知平台"})

    def _check():
        ok = bm.is_logged_in(platform, refresh=True, probe=True)
        db.log("info", f"{PLATFORMS[platform]['name']} {'已登录' if ok else '还没登录'}")
        if ok:
            runner.resume_after_login(platform, group_ids=groups)

    threading.Thread(target=_check, daemon=True).start()
    return jsonify({"ok": True, "message": "正在检测登录态"})


@app.route("/api/specs")
def api_specs():
    """按规格分类聚合：每个分类的最低价/最高价/商品数 + 最低价那件商品的链接。"""
    platform = request.args.get("platform") or None
    group = request.args.get("group") or None
    level = request.args.get("level") or "ok"
    cond = request.args.get("condition") or None
    rows = db.spec_summary(platform=platform, group=group,
                           only_ok=(level == "ok"), condition=cond)
    return jsonify({"ok": True, "specs": rows})


@app.route("/api/sku", methods=["POST"])
def api_sku():
    """点「看规格」：进商品详情页把该商品的全部 SKU 价格列出来。
    详情页很重，所以只在用户主动点时才访问，不批量抓。"""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    platform = data.get("platform") or detail_mod.guess_platform(url)
    if not platform or platform not in PLATFORMS:
        return jsonify({"ok": False, "message": "无法识别平台"})
    if not url.startswith("http"):
        return jsonify({"ok": False, "message": "链接无效"})

    # 详情页要跟列表页同一会话，否则拿不到已登录态
    try:
        with Session(platform, headless=config.HEADLESS) as sess:
            if not sess.has_login_cookie():
                bm.login_flags[platform] = False
                return jsonify({"ok": False, "message": f"{PLATFORMS[platform]['name']} 还没登录"})
            st, msg, payload = detail_mod.fetch_sku_prices(sess, platform, url)
    except Exception as e:
        db.log("warn", f"SKU 解析失败: {str(e)[:120]}")
        return jsonify({"ok": False, "message": str(e)[:120]})

    db.log("info", f"SKU 解析 {platform}: {msg}")
    return jsonify({"ok": st == "ok", "message": msg, "data": payload})


@app.route("/api/review")
def api_review():
    """价格异常清单：单独列出太离谱的。"""
    return jsonify({"ok": True, "items": db.review_items(500)})


@app.route("/api/rejected")
def api_rejected():
    """被剔除的可疑商品留档，方便回看调规则。"""
    return jsonify({"ok": True, "items": db.recent_rejected(300)})


@app.route("/api/items")
def api_items():
    platform = request.args.get("platform") or None
    group = request.args.get("group") or None
    limit = int(request.args.get("limit") or 1200)
    # level=ok(默认，只正常) / review(只看异常) / all(全看)
    level = request.args.get("level") or "ok"
    cond = request.args.get("condition") or None
    rows = db.latest_products(platform=platform, group=group, limit=limit,
                              only_ok=(level == "ok"), condition=cond)
    if level == "review":
        rows = [r for r in rows if (r.get("price_level") or "ok") == "review"]
    return jsonify({"ok": True, "items": rows})


@app.route("/api/history/<int:product_id>")
def api_history(product_id):
    return jsonify({"ok": True, "history": db.price_history(product_id)})


@app.route("/api/keywords", methods=["GET", "POST", "DELETE"])
def api_keywords():
    if request.method == "GET":
        return jsonify({"ok": True, "groups": db.keyword_groups()})
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if request.method == "POST":
        if action == "add":
            ok, msg = db.add_keyword(data.get("group_id"), data.get("keyword") or "")
        elif action == "toggle":
            ok, msg = db.toggle_keyword(data.get("keyword"), bool(data.get("enabled")))
        else:
            ok, msg = False, "未知操作"
        return jsonify({"ok": ok, "message": msg})
    if request.method == "DELETE":
        ok, msg = db.delete_keyword(data.get("keyword"))
        return jsonify({"ok": ok, "message": msg})


@app.route("/api/logs")
def api_logs():
    return jsonify({"ok": True, "logs": db.recent_logs(120)})


@app.route("/api/export.csv")
def api_export():
    rows = db.latest_products(limit=5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["平台", "分组", "关键词", "标题", "价格", "历史最低", "店铺", "链接", "最后采集时间"])
    for r in rows:
        w.writerow([
            PLATFORMS.get(r["platform"], {}).get("name", r["platform"]),
            r.get("group_name") or "", r["keyword"], r["title"], f'{r["price"]:.2f}',
            f'{r["min_price"]:.2f}', r.get("shop") or "", r["url"],
            time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_seen"])),
        ])
    data = "\ufeff" + buf.getvalue()
    return Response(data, mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=pricewatch.csv"
    })


def _shutdown():
    try:
        bm.close_all()
    except Exception:
        pass


def main(open_browser=True):
    db.init()
    atexit.register(_shutdown)
    runner.start_scheduler()
    url = f"http://{config.HOST}:{config.PORT}/"
    print("\n" + "=" * 62)
    print("  硬件比价 —— 淘宝 / 京东 / 拼多多  DDR4 · DDR5 · RTX 50 系")
    print(f"  界面地址: {url}")
    print("  第一次用请先在网页里点各平台「登录」，扫码后关掉窗口即可")
    print("  关掉这个黑色窗口 = 停止程序")
    print("=" * 62 + "\n")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True, use_reloader=False)