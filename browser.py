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

# 去掉自动化痕迹，降低被风控挑出来的概率。
#
# 这版是按**指纹实测**结果改的（tmp/fp_compare.py 对比过「项目会话 vs 干净 Edge」）：
#   1. 删掉 `navigator.permissions.query` 的改写 —— 原文案会让
#      `permissions.query.toString()` 不再是 `native code`，风控一测一个准
#      （实测：改写后 permissions_query_is_native=false，干净浏览器是 true）。
#      这一项的破绽比 webdriver 本身更明显。
#   2. 删掉 `navigator.plugins` 的伪造 —— 返回 [1,2,3,4,5] 这种数组，
#      `plugins[0].name` 是 undefined，任何真浏览器都不会这样。
#   3. `hardwareConcurrency` 不再写死 8：本机真实核心数是 12，
#      写死 8 与 navigator 其他字段（deviceMemory 16 等）不自洽。
#      改为**读机器真实值**（由 Python 注入 _PW_CORES）。
#   4. `languages` 补齐成真实 Edge 的形态（4 项），原来只有 3 项偏少。
#
# 保留 `webdriver` 为 undefined：这是必要的（干净 Edge 反而因为
# Playwright 启动而暴露 true，实测确认）。
STEALTH_JS = r"""
(() => {
  // 1) webdriver：Playwright 默认留下 true，必须抹掉
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}

  // 2) languages：用真实 Edge 的形态，别只给 3 项
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['zh-CN', 'zh', 'en', 'en-GB', 'en-US'],
      configurable: true,
    });
  } catch (e) {}

  // 3) hardwareConcurrency：读真实核心数（Python 注入 __PW_CORES__）
  try {
    const cores = (typeof window.__PW_CORES__ === 'number' && window.__PW_CORES__ > 0)
      ? window.__PW_CORES__ : 12;
    Object.defineProperty(navigator, 'hardwareConcurrency', {
      get: () => cores,
      configurable: true,
    });
  } catch (e) {}

  // 4) window.chrome：只在缺失时补，不覆盖浏览器自带的
  if (!window.chrome) {
    window.chrome = { runtime: {}, csi: function(){}, loadTimes: function(){} };
  }

  // 5) 屏幕与窗口尺寸：headless Chromium 的 screen 固定 800×600，
  //    启动参数 --kiosk-printing-screen-size 实测无效（复测 screen 仍是 800×600）。
  //    这里直接从 JS 层让 screen/avail/inner/outer 自洽：
  //    屏幕 1920×1080、可用 1920×1040（减去任务栏）、窗口 1280×720。
  //    注意只改可枚举的 getter，不破坏 Screen.prototype 的原生形态。
  try {
    const SCREEN = { w: 1920, h: 1080, aw: 1920, ah: 1040 };
    const WIN = { w: 1280, h: 720 };
    const def = (obj, prop, val) => {
      try {
        Object.defineProperty(obj, prop, { get: () => val, configurable: true });
      } catch (e) {}
    };
    def(window.screen, 'width', SCREEN.w);
    def(window.screen, 'height', SCREEN.h);
    def(window.screen, 'availWidth', SCREEN.aw);
    def(window.screen, 'availHeight', SCREEN.ah);
    def(window.screen, 'colorDepth', 24);
    def(window.screen, 'pixelDepth', 24);
    def(window, 'outerWidth', WIN.w);
    def(window, 'outerHeight', WIN.h);
    // innerWidth/Height：留出边框与滚动条，让 inner < outer 更自然
    def(window, 'innerWidth', WIN.w - 16);
    def(window, 'innerHeight', WIN.h - 92);
  } catch (e) {}

  // 6) performance.now 精度：headless 下量程被降到 0.1ms（真实浏览器是µs级）。
  //    直接改写 Performance.prototype.now 会暴露"非原生"，风险大于收益，
  //    所以这里**不改** —— 实测差异存在但不构成明确判定依据。
  //    （若以后确需处理，正确做法是 numactl/--disable-features=... 层面，
  //     而非 JS 层改写。）

  // 注意：**故意不改** navigator.permissions.query 与 navigator.plugins。
  //        前者会让函数不再是 native code（最明显的自动化破绽），
  //        后者伪造出的 plugins 结构经不起检查。
})();
"""

# 各平台登录后才会下发的 cookie，命中任意一个即视为已登录
LOGIN_COOKIES = {
    # 只认登录后才会下发的身份 cookie；cookie2 / _l_g_ 访客也有，绝不能算
    "taobao": ["unb", "tracknick"],
    "jd": ["pt_key", "thor", "pt_pin"],
    # 拼多多：实测登录态里没有 PASS_ID，真正的凭据是 PDDAccessToken
    # （见 analysis/github_survey.md：PDD 列表页/搜索页要求请求头带
    #   AccessToken，值就是这枚 cookie）。PASS_ID 仍保留以兼容旧登录态。
    "pdd": ["PDDAccessToken", "PASS_ID", "pdd_user_id"],
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


# Chromium 的单实例锁文件；进程被强杀后会残留，导致下次启动
# "Failed to create a ProcessSingleton for your profile directory"
PROFILE_LOCK_FILES = (
    "lockfile", "SingletonLock", "SingletonSocket", "SingletonCookie",
)


def _clean_profile_locks(user_data_dir):
    """删掉僵死的 profile 锁文件。进程还活着时这些删了也无害，进程会自己重建。"""
    if not user_data_dir:
        return 0
    n = 0
    for name in PROFILE_LOCK_FILES:
        p = os.path.join(user_data_dir, name)
        try:
            if os.path.lexists(p):
                os.remove(p)
                n += 1
        except Exception:
            pass
    return n


def _kill_profile_holders(user_data_dir):
    """杀掉仍在占用该 profile 目录的本机 Edge/Chrome 进程。

    只杀命令行里带本 profile 路径的进程 —— 绝不碰用户自己开着的浏览器。
    返回被清理的进程数。
    """
    if not user_data_dir or os.name != "nt":
        return 0
    uad = os.path.normcase(os.path.abspath(user_data_dir))
    uad_alt = uad.replace("\\", "/")
    script = (
        "$d=[Console]::In.ReadToEnd();"
        "Get-CimInstance Win32_Process -Filter \"name='msedge.exe' or name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -and ("
        "($_.CommandLine -like ('*'+$d+'*')) -or ($_.CommandLine -like ('*'+$d.Replace('\\','/')+'*'))) } | "
        "ForEach-Object { $_.ProcessId }"
    )
    killed = 0
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=uad, capture_output=True, text=True, timeout=25,
        ).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
                killed += 1
            except Exception:
                pass
    except Exception:
        pass
    return killed


def _browser_kwargs(platform, headless, profile_dir=None):
    cfg = config.PLATFORMS[platform]
    kw = dict(
        user_data_dir=profile_dir or profile_path(platform),
        headless=headless,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=LAUNCH_ARGS,
        viewport=dict(config.MOBILE_VIEWPORT) if cfg.get("mobile") else dict(config.DESKTOP_VIEWPORT),
        user_agent=config.MOBILE_UA if cfg.get("mobile") else config.DESKTOP_UA,
        ignore_default_args=["--enable-automation"],
    )
    # 指纹实测发现：固定 viewport 会让 innerWidth/outerWidth/screen 三者
    # 全是 1440×900，而真实 Edge 是窗口 1280×720、屏幕另有实际值 ——
    # 「视口 = 屏幕 = 窗口」这种完全一致本身就是自动化特征。
    #
    # 但实测也发现：直接 no_viewport 会让 headless Edge 掉到默认 800×600，
    # screen/avail 变成 [800,600] 反而更假。
    # 所以改成：**视口不定死，但显式把窗口与屏幕尺寸设成真实感的值**，
    # 让 inner/outer/screen 三者自洽又不等于屏幕全尺寸。
    if not cfg.get("mobile") and getattr(config, "USE_REAL_VIEWPORT", True):
        w, h = getattr(config, "REAL_WINDOW_SIZE", (1280, 720))
        sw, sh = getattr(config, "REAL_SCREEN_SIZE", (1920, 1080))
        kw["no_viewport"] = True
        kw["viewport"] = None
        args = kw.setdefault("args", [])
        args.append(f"--window-size={w},{h}")
        # 强制真实屏幕尺寸（headless 默认 800×600 太小，反而更假）
        args.append(f"--force-device-scale-factor=1")
        # Edge/Chromium 没有直接的 --screen-size，但窗口 + 以下参数可让
        # screen.width/height 报出接近真机的值
        args.append(f"--kiosk-printing-screen-size={sw},{sh}")
    # 出口代理：config.PROXY_URL / PLATFORM_PROXY 设了就带上。
    try:
        from . import net_route
        proxy = net_route.playwright_proxy(platform)
        if proxy:
            kw["proxy"] = proxy
    except Exception:
        pass
    return kw


class Session:
    """一次完整的浏览器会话。只能在创建它的线程里使用，用 with 保证释放。"""

    def __init__(self, platform, headless=None, profile_dir=None):
        self.platform = platform
        self.cfg = config.PLATFORMS[platform]
        self.headless = config.HEADLESS if headless is None else headless
        self.pw = None
        self.ctx = None
        self.home_visited = False   # 温和模式用：本会话是否已在站内首页待过
        self._pid = threading.get_ident()
        # profile_dir 非空时用这个目录（而不是 data/profiles/<platform>）。
        # 用途：详情页补读在独立线程里跑，必须用**独立 profile**，
        # 否则会跟主采集线程抢同一个 Chromium 单实例锁，直接打架。
        self.profile_dir = profile_dir

    # ---- 生命周期 ----
    def __enter__(self):
        self.pw = sync_playwright().start()
        kw = _browser_kwargs(self.platform, self.headless, self.profile_dir)
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
            # 把本机真实 CPU 核心数注入到 window.__PW_CORES__，
            # 供 STEALTH_JS 覆盖 navigator.hardwareConcurrency 时读真实值，
            # 避免"写死 8 但 deviceMemory=16"这种自相矛盾。
            cores = os.cpu_count() or 12
            self.ctx.add_init_script(f"window.__PW_CORES__ = {int(cores)};")
            self.ctx.add_init_script(STEALTH_JS)
            self.ctx.set_default_timeout(config.PAGE_TIMEOUT_MS)
        except Exception:
            pass
        log("info", f"{self.cfg['name']} 浏览器就绪（{'无头' if self.headless else '可见'}）")
        return self

    def _launch(self, kw):
        """自带 chromium → 本机 Edge → 本机 Chrome，依次尝试。

        启动前先做 profile 卫生：上次崩溃或被强杀会在 user_data_dir 里留下
        lockfile / Singleton 锁，且残留的 msedge.exe 仍持有该目录，于是下次启动报
        "Target page, context or browser has been closed" 或
        "Failed to create a ProcessSingleton for your profile directory"。
        这里先清僵尸锁；仍失败就杀掉占用该 profile 的残留进程再重试一次。
        """
        pw = self.pw
        uad = kw.get("user_data_dir")
        bundle = pw.chromium.executable_path
        if bundle and os.path.exists(bundle):
            _clean_profile_locks(uad)
            return pw.chromium.launch_persistent_context(**kw)
        log("info", "自带的 Chromium 未安装，改用本机 Edge 浏览器")

        def _try_edge():
            return pw.chromium.launch_persistent_context(channel="msedge", **kw)

        # 第一轮：清僵死锁再试
        n = _clean_profile_locks(uad)
        if n:
            log("info", f"已清理 {n} 个残留 profile 锁文件")
        try:
            return _try_edge()
        except Exception as e:
            first = e
            log("warn", f"Edge 启动失败({str(e)[:80]})，清理残留进程后重试")

        # 第二轮：杀掉占用该 profile 的残留浏览器进程 → 再清锁 → 再试
        killed = _kill_profile_holders(uad)
        _clean_profile_locks(uad)
        if killed:
            log("info", f"已清理 {killed} 个占用该 profile 的残留浏览器进程")
            time.sleep(1.0)
        try:
            return _try_edge()
        except Exception as e2:
            log("warn", f"清理后 Edge 仍失败({str(e2)[:80]})，改试本机 Chrome")

        for exe in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ):
            if os.path.exists(exe):
                try:
                    return pw.chromium.launch_persistent_context(executable_path=exe, **kw)
                except Exception:
                    continue
        raise first

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