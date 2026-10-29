# -*- coding: utf-8 -*-
"""Playwright 会话管理。

关键约束：Playwright 同步 API 的 pw / context / page 对象都绑定在**创建它的那条线程**上，
跨线程调用会直接抛 "Playwright connection closed"。所以这里不持常驻单例，
而是每次操作在**当前线程内**建一个完整 Session，用完即关；
登录态靠 data/profiles/<platform> 这个持久化目录跨会话保留。
"""
import os
import random
import threading
import time

from playwright.sync_api import sync_playwright

from . import config
from .db import log

# 去掉自动化痕迹，降低被风控挑出来的概率
STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
window.chrome = window.chrome || { runtime: {}, csi: function(){}, loadTimes: function(){} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications' ? Promise.resolve({ state: Notification.permission }) : origQuery(p);
}
"""

# 各平台登录后才会下发的 cookie，命中任意一个即视为已登录
LOGIN_COOKIES = {
    # 只认登录后才会下发的身份 cookie；cookie2 / _l_g_ 访客也有，绝不能算
    "taobao": ["unb", "tracknick"],
    "jd": ["pt_key", "thor", "pt_pin"],
    "pdd": ["PASS_ID", "pdd_user_id"],
}

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-features=Translate,OptimizationHints",
    "--disable-infobars",
]


def profile_path(platform):
    p = os.path.join(config.PROFILE_DIR, platform)
    os.makedirs(p, exist_ok=True)
    return p


def _browser_kwargs(platform, headless):
    cfg = config.PLATFORMS[platform]
    return dict(
        user_data_dir=profile_path(platform),
        headless=headless,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=LAUNCH_ARGS,
        viewport=dict(config.MOBILE_VIEWPORT) if cfg.get("mobile") else dict(config.DESKTOP_VIEWPORT),
        user_agent=config.MOBILE_UA if cfg.get("mobile") else config.DESKTOP_UA,
        ignore_default_args=["--enable-automation"],
    )


class Session:
    """一次完整的浏览器会话。只能在创建它的线程里使用，用 with 保证释放。"""

    def __init__(self, platform, headless=None):
        self.platform = platform
        self.cfg = config.PLATFORMS[platform]
        self.headless = config.HEADLESS if headless is None else headless
        self.pw = None
        self.ctx = None
        self.home_visited = False   # 温和模式用：本会话是否已在站内首页待过
        self._pid = threading.get_ident()

    # ---- 生命周期 ----
    def __enter__(self):
        self.pw = sync_playwright().start()
        kw = _browser_kwargs(self.platform, self.headless)
        try:
            self.ctx = self._launch(kw)
        except Exception as first_err:
            try:
                self.pw.stop()
            except Exception:
                pass
            self.pw = None
            raise RuntimeError(f"{self.cfg['name']} 浏览器启动失败: {first_err}") from first_err
        try:
            self.ctx.add_init_script(STEALTH_JS)
            self.ctx.set_default_timeout(config.PAGE_TIMEOUT_MS)
        except Exception:
            pass
        log("info", f"{self.cfg['name']} 浏览器就绪（{'无头' if self.headless else '可见'}）")
        return self

    def _launch(self, kw):
        """自带 chromium → 本机 Edge → 本机 Chrome，依次尝试。"""
        pw = self.pw
        bundle = pw.chromium.executable_path
        if bundle and os.path.exists(bundle):
            return pw.chromium.launch_persistent_context(**kw)
        log("info", "自带的 Chromium 未安装，改用本机 Edge 浏览器")
        try:
            return pw.chromium.launch_persistent_context(channel="msedge", **kw)
        except Exception as e:
            log("warn", f"Edge 启动失败({str(e)[:80]})，改试本机 Chrome")
            for exe in (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ):
                if os.path.exists(exe):
                    return pw.chromium.launch_persistent_context(executable_path=exe, **kw)
            raise e

    def close(self):
        ctx, pw = self.ctx, self.pw
        self.ctx = self.pw = None
        if ctx is not None:
            try:
                ctx.close()          # 关掉会把 profile 里的登录态落盘
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 页面 ----
    def page(self):
        if threading.get_ident() != self._pid:
            raise RuntimeError("Session 不能跨线程使用")
        if self.ctx is None:
            raise RuntimeError("Session 已关闭")
        pages = [p for p in self.ctx.pages if not p.is_closed()]
        pg = pages[0] if pages else self.ctx.new_page()
        pg.set_default_timeout(config.PAGE_TIMEOUT_MS)
        return pg

    def new_page(self):
        return self.ctx.new_page()

    # ---- 登录态 ----
    def has_login_cookie(self):
        """纯本地读取 cookie，不发任何网络请求。"""
        if self.ctx is None:
            return False
        try:
            names = {c.get("name") for c in self.ctx.cookies()}
        except Exception:
            return False
        keys = LOGIN_COOKIES.get(self.platform, [])
        return any(k in names for k in keys)

    def judge_by_page(self, page):
        """访问 check_url 后按落地页判定（cookie 判不出来时的兜底）。"""
        url = (page.url or "").lower()
        try:
            html = page.content()
        except Exception:
            html = ""
        p = self.platform
        if p == "taobao":
            if "login.taobao.com" in url or "login.tmall.com" in url:
                return False
            return "taobao.com" in url and "i.taobao.com" in url
        if p == "jd":
            if "passport.jd.com" in url or "plogin.m.jd.com" in url or "login.jd.com" in url:
                return False
            return "jd.com" in url and "请登录" not in html[:20000]
        if p == "pdd":
            if "login" in url and ("yangkeduo" in url or "pinduoduo" in url):
                return False
            return "yangkeduo.com" in url or "pinduoduo.com" in url
        return True


class BrowserManager:
    """只维护内存状态（登录标记、活动标记），不持有任何 Playwright 对象。"""

    def __init__(self):
        self.login_flags = {}
        self.active = {}               # platform -> "login" | "scrape"
        self._lock = threading.RLock()

    # ---- 状态 ----
    def set_active(self, platform, what):
        with self._lock:
            if what:
                self.active[platform] = what
            else:
                self.active.pop(platform, None)

    def snapshot_active(self):
        with self._lock:
            return dict(self.active)

    # ---- 登录态判定 ----
    def is_logged_in(self, platform, refresh=False, probe=False):
        """读 cookie 判定；probe=True 时再用真实页面复核一次。"""
        if not refresh and platform in self.login_flags:
            return self.login_flags[platform]
        result = False
        try:
            with Session(platform, headless=True) as s:
                result = s.has_login_cookie()
                if not result and probe:
                    pg = s.page()
                    pg.goto(config.PLATFORMS[platform]["check_url"],
                            wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
                    pg.wait_for_timeout(1500)
                    result = s.judge_by_page(pg)
        except Exception as e:
            log("warn", f"{platform} 登录态检测失败: {str(e)[:120]}")
            result = False
        self.login_flags[platform] = result
        log("info", f"{platform} 登录态: {'已登录' if result else '未登录'}")
        return result

    def close_all(self):
        """兼容旧调用：现在没有常驻浏览器，无事可做。"""
        with self._lock:
            self.active.clear()


bm = BrowserManager()


# ---------- 交互式登录（在调用线程内自包含，可放心从任意后台线程启动） ----------

def login_flow(platform, timeout_s=600, interval_s=3.0, on_success=None):
    """打开可见窗口 → 等用户扫码 → 检测到登录 cookie 就关窗收尾。返回是否成功。"""
    cfg = config.PLATFORMS[platform]
    bm.set_active(platform, "login")
    deadline = time.time() + timeout_s
    ok = False
    try:
        with Session(platform, headless=False) as s:
            page = s.page()
            try:
                page.goto(cfg["login_url"], wait_until="domcontentloaded",
                          timeout=config.NAV_TIMEOUT_MS)
                page.bring_to_front()
            except Exception as e:
                log("warn", f"打开 {cfg['name']} 登录页失败: {str(e)[:120]}")
            # 拼多多网页端只有手机号+验证码，帮用户把"同意协议"勾上、光标放进输入框
            if platform == "pdd":
                try:
                    page.wait_for_timeout(2000)
                    for sel in ("label[class*=agree]", "div[class*=agree]", "span[class*=agree]",
                                "input[type=checkbox]"):
                        try:
                            el = page.query_selector(sel)
                            if el:
                                el.click(timeout=3000)
                                break
                        except Exception:
                            continue
                    phone = page.query_selector("input[type=tel]")
                    if phone:
                        phone.click(timeout=3000)
                    log("info", "拼多多：请在窗口里填手机号 → 点「发送验证码」→ 手机收到后填进第二格 → 点「登录」")
                except Exception as e:
                    log("warn", f"拼多多登录页预处理失败: {str(e)[:80]}")
                log("info", f"已打开 {cfg['name']} 登录窗口")
            else:
                log("info", f"已打开 {cfg['name']} 登录窗口，请扫码或输账号密码")

            while time.time() < deadline:
                if s.has_login_cookie():
                    ok = True
                    log("info", f"{cfg['name']} 检测到登录 cookie，正在保存登录态")
                    time.sleep(1.0)
                    break
                # 用户把所有页面都关了又没登上 → 不再傻等
                try:
                    alive = [p for p in s.ctx.pages if not p.is_closed()]
                except Exception:
                    alive = []
                if not alive:
                    log("warn", f"{cfg['name']} 登录窗口被关闭，未检测到登录")
                    break
                time.sleep(interval_s)
            if not ok and time.time() >= deadline:
                log("warn", f"{cfg['name']} 等待登录超时（{timeout_s // 60} 分钟）")
    except Exception as e:
        log("error", f"{cfg['name']} 登录流程出错: {str(e)[:160]}")
        ok = False
    finally:
        bm.set_active(platform, None)

    if ok:
        # 关窗后 cookie 才落盘，稍等再用全新会话复核一次，避免"假登录"
        time.sleep(1.2)
        try:
            with Session(platform, headless=True) as v:
                ok = v.has_login_cookie()
        except Exception:
            ok = False
        if not ok:
            log("warn", f"{cfg['name']} 登录态没落盘，请重试一次")
        else:
            log("info", f"{cfg['name']} 登录态已确认保存")
    bm.login_flags[platform] = bool(ok)
    if ok and on_success:
        try:
            on_success()
        except Exception:
            pass
    return ok


def rand_pause(a=0.6, b=1.8):
    time.sleep(random.uniform(a, b))