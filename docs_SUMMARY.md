# 硬件比价工作台（pricewatch）实战总结

> 2026-10-29/30 项目总结
> 代码：https://github.com/cxkctrl24-lgtm/pricewatch
> 目标：抓 DDR4/DDR5 内存 + RTX 50 系显卡价格，做本地比价工具

---

## 一、最终架构

```
Flask 本地网页(127.0.0.1:8765) + Playwright(本机 Edge) + SQLite
├─ browser.py    Session 模式（每线程自包含，避开 Playwright 跨线程崩溃）
├─ scrapers/
│   ├─ collector.py  淘宝/京东 DOM 解析
│   ├─ pdd_api.py    拼多多 POST 接口（anti_content 签名）
│   └─ detail.py     详情页 SKU JSON 解析
├─ classify.py   分类器（代数/容量/条数/频率/台式笔记本/成色）
├─ fakefilter.py 假货指纹打分
├─ outlier.py    价格离群检测 + 行业基准
├─ runner.py     采集调度 + 三级过滤
└─ 四视图界面：分类比价 / 商品明细 / 异常清单 / 已剔除
```

---

## 二、三平台数据来源（全部实测确认）

| 平台 | 列表页 | 详情页 SKU |
|---|---|---|
| 淘宝 | DOM 解析，**必须按商品域名筛链接** | 内嵌 `skuBase` + `skuCore.sku2info` |
| 京东 | DOM `li.gl-item` | `#choose-attrs` |
| 拼多多 | **POST** `proxy/api/search?pdduid=` | 内嵌 `rawData` |

### 淘宝：卡片第一个 `<a>` 是广告位（关键坑）

```html
<div class="doubleCard--gO3Bz6bu">
  <a href="https://click.simba.taobao.com/cc_im?...">  ← 广告跳转，第一个 a
  <a href="//item.taobao.com/item.htm?id=xxx">        ← 真商品，第二个 a
```

原实现取「卡片里第一个 a[href]」→ 拿到广告链接 → 被域名白名单过滤 → 整卡丢弃 → 0 条。
**修正：遍历所有商品链接（按域名匹配），再向上找卡片容器。** 修复后 44→45 件，零杂质。

class 带每次发版就变的哈希后缀（`doubleCard--gO3Bz6bu`），所以选择器一律用 `[class*=]` 前缀匹配。

### 拼多多：anti_content 签名（最深的坑）

抓包实证：
```
POST https://mobile.yangkeduo.com/proxy/api/search?pdduid=<uid>
Content-Type: application/json
body: {"q":"DDR5 16G","page":1,"size":50,"sort":"default","source":"search",
       "flip":"<翻页游标>","anti_content":"<签名>",...}
```

- 自己构造 query string → `error_code: 54001`（签名校验失败），**与 URL 编码无关**
- `anti_content` 由页面 JS 生成，长度上千字符，无法离线复刻
- **正解：hook 页面自己的 fetch/XHR，捕获它发出的那条请求，原样复刻改 q/page/flip**

`it` 的结构还有三种形态，全都要兼容：
- `{"item_data": {"goods_model": {...}}}`
- `{"item_data": {...}}`
- `{...}`  ← 实际最常返回这种

价格单位是**分**（`price: 108000` = ¥1080），券后价在 `couponPromoPrice`。

---

## 三、Playwright 跨线程崩溃（登录窗口弹不出的根因）

日志实证：`jd 浏览器启动失败(Playwright connection closed)`

Playwright 同步 API 的 `pw` / `context` / `page` **绑定创建它的那条线程**。
旧架构用全局单例 `BrowserManager` 持有 `sync_playwright()` 实例，
Flask 请求线程、采集线程、登录监听线程共享 → 跨线程一调必崩，异常被 try/except 吞掉，表现是"点了没反应"。

**修正：Session 模式**
```python
class Session:
    def __enter__(self):   # 当前线程内 start() + launch_persistent_context
    def page(self):        # threading.get_ident() 校验同线程
    def close(self):       # 先关 ctx（让 cookie 落盘）再 pw.stop()
```
登录态靠 `data/profiles/<platform>` 持久目录跨会话保留。

### 登录态误判的两个坑
1. 淘宝 `cookie2` / `_l_g_` **访客也会下发**，不能当登录标志 → 只认 `unb` / `tracknick`
2. 判定成功后立刻 close，Edge 还没写盘 → 新会话读不到 → 需 sleep 1s 再 close，再用全新无头会话复核

实测未登录时 cookie 数：淘宝 31 / 京东 27 / 拼多多 21，身份 cookie 命中全空。

---

## 四、分类维度（这是比价能否成立的前提）

### 为什么必须细分

v1 把「DDR5 16G 台式」归成一类，38 件价格 **¥645 ~ ¥4799，跨度 7.4 倍** —— 没法比价。

### 最终六个维度

| 维度 | 拆法 | 例子 |
|---|---|---|
| 代数 | DDR5 / DDR4 | |
| 单条容量 | 4/8/16/32/64G 各自独立 | `DDR5 8G 台式` |
| 套装条数 | 明确标 ×N，**不等于总容量** | `DDR5 16G×2 台式`（不是"32G"） |
| 频率档 | ≤4800/5200/5600/6000/6400/7000/7600+ | `DDR5 16G 台式 6000` |
| 台式/笔记本 | 独立，未知时归"通用"（**不分裂**） | `DDR5 16G 笔记本` |
| 成色 | 一手 / 二手独立 | 二手加「二手」后缀 |

**归一结果：315 件 → 64 个分类。**

### 显卡单独规则
只按型号 + 显存：`RTX 5060 8G` / `RTX 5070 Ti 16G`。
多型号标题（RTX5060/5070TI/5080）取**第一个**型号 —— 和列表页展示的低价对应。

### 两个易错点
1. `PC5-` 是 DDR5 SO-DIMM 代号，**属笔记本**，不是台式
2. 套装按总容量归一（16G×2 和单条 32G）会让「¥4199 的 16G」混进「¥1299 的 32G」，
   所以改成**单条容量 × 条数**独立表示

---

## 五、三级过滤（假货治理）

| 档 | 判定 | 处置 | 界面 |
|---|---|---|---|
| 正常 | 通过全部检查 | 收录 | 默认显示 |
| 异常 | 同档价格离群 | 收录但标记 | 「异常清单」页 |
| 剔除 | 假货指纹命中 | **不入库** | 「已剔除」留档（rejected 表） |

### 第一级：假货指纹（打分，≥4 分剔除）

| 检测项 | 分值 | 说明 |
|---|---|---|
| 标题含非商品词 | **5** | 配件/支架/空盒/定金/**议价**/差价链接/测试链接 |
| 标题堆砌互斥规格 | 2/项 | 同时写 DDR4+DDR5、台式+笔记本、堆3种容量、堆4种频率 |
| 价格异常 | 3 | 低于同类中位数 45% |
| 店名通用 | 2 | 正品XX经销商 / 中国XX供应商 / tb+数字 / XX批发 |
| 标题无品牌 | 1 | 无任何品牌名 |

**关键取舍**：第一版把「多容量标题」全判假货，误杀 12%（连正规多规格商品也筛掉）。
改成 **有品牌 + 店名正常 → 多容量列表不扣分**，误杀率降到 **5%**。

### 第二级：价格离群（只在同档内比）

三种判定法按样本量自动选：
1. **IQR**（样本 ≥4）：`Q1-1.5×IQR ~ Q3+1.5×IQR`
2. **中位数倍数**（样本 2-3）：低于 40% 或高于 3 倍
3. **行业基准**（样本 <2）：`outlier.BASELINE` 内置 28 个规格区间

低得离谱 → 剔除；高得离谱 → 只标记（旗舰型号确实贵）。

### 第三级：二手独立
二手不是假货，是真实低价商品，但**必须和一手分开**——否则最低价永远被二手占着。

判定词：二手/拆机/拆解/翻新/矿卡/9成新/99新/散片/回收/退役/成色

---

## 六、列表页的固有局限（重要认知）

分析工具发现 `DDR4 8G 台式` 价格跨度 **19.2 倍**（¥69 ~ ¥1323），查明细才明白：

> 标题挂「8G 16G 32G」，实际在售的是 16G 或 32G，
> 列表页那个价格对应 16G 规格，但解析只能取"最小容量 8G" → 高价进了 8G 组。

**这是列表页的固有局限，不是 bug。** 两个补救：
1. 详情页 SKU 解析（`skuBase.skus` + `skuCore.sku2info`，实测一件商品 36 个规格、跨度 ¥208~¥839）
2. 离群检测（同档内自动把离谱价标出来）

---

## 七、实测数据（315 件真实淘宝数据）

```
正常收录 301 件 | 标记异常 14 | 直接剔除 13
一手 279 | 二手 22
64 个分类
品牌覆盖率 82% | 二手占比 6.7%
```

剔除示例（全是真钓鱼）：
```
¥69   拆机DDR4台式机内存条4G 8G 2133 2400 2666  [安信精品硬盘批发]
¥365  16g内存条8G DDR4 32G ddr5 笔记本内存台式    [电码堡]
¥892  议价宏基ddr5 16g*2 6000 c32一起32g        [新一代芙蓉工控]
¥920  议价议价宏基ddr5 16g*2 6000 c32一起32g     [永赢工控]
¥920  议价宏基ddr5 16g*2 6000 c32一起32g        [联创科技工控]
```

---

## 八、降低风控的实测清单

| 措施 | 说明 |
|---|---|
| 本机 Edge 真内核 | Playwright 自带 chromium-1248 未安装，自动降级 `channel="msedge"` |
| 抹自动化标记 | `ignore_default_args=["--enable-automation"]` + `navigator.webdriver` + `window.chrome` + permissions.query 覆写 |
| 持久化 profile | 攒真实浏览历史与 cookie |
| 站内 Referer | `page.goto(search_url, referer=home_url)` |
| 节奏随机化 | 首屏 1.8-3.2s、滚动 700-1500px、滚动间隔 0.7-1.8s（每 3 次插 3-6s 长顿）、关键词间 3-7s、平台间 6-13s |
| 首页预热 | 进站先待 2-3.6s、滚一下，再跳搜索 |
| 绝不并发 | 三平台串行 |
| 定时避开日间 | `SCHEDULE_TIME = "21:40"` |

**限流识别**：京东「访问频繁」会返回特定文案，程序识别为 `rate_limited`，
立刻停该平台所有关键词（越滑限流越久），冷却 10 分钟。

真实触发记录：短时间内调试抓了十几个页面 → 京东 `抱歉由于访问频繁导致无法搜索` + 拼多多 `ERR_CONNECTION_RESET`。
**结论：真正的风控大头是频率，不是指纹。** 一天一次、一次十来个词，基本不会被拦。

---

## 九、工程经验

### Playwright 必须 Session 化
任何被多线程调用的浏览器操作，都要在**调用线程内**创建完整 Session。全局单例必崩。

### SQLite 并发
Flask 多线程 + 后台采集线程会 `database is locked`。
统一用单个连接做批量写，或者批量操作前先停服务。

### 诊断工具要早写
`_diagnose()` 在解析失败时写：
- `diag_<平台>_<ts>.txt`：各选择器命中数、商品链接 DOM 链、页面文本长度
- `page_<平台>_<ts>.html`：整页 HTML，可离线反复调选择器

这一个函数省掉了十几次重抓。

### 离线调解析器
```python
html2 = re.sub(r"<script[^>]*>.*?</script>", "", html)  # 去脚本
ctx.route("**/*", lambda r: r.abort() if r.request.url.startswith("http") else r.continue_())
pg.set_content(html2)   # set_content 而非 goto file://（file:// 会因外链卡死）
```

### 工具脚本（可复用）
| 脚本 | 用途 |
|---|---|
| `tools_analyze_patterns.py` | 从真实数据归纳共同点：规格组合分布/价格跨度/品牌覆盖率/高频词，`--write` 导出基准价 |
| `tools_rebuild.py` | 存量重算：改规则不用重抓，`--apply` 写库并把可疑转入 rejected |
| `tools_sniff.py` / `tools_sniff_post.py` | 抓包分析接口（GET vs POST + body） |
| `tools_item_sku.py` | 进详情页看 SKU |
| `tools_verify.py` | 单平台单关键词采集验证 |

---

## 十、待办 / 已知未验证

- **京东采集**：搜索页 DOM 结构未在登录态下完整验证（调试期被限流）
- **拼多多接口**：hook 复刻方案代码已写完，但因连接被重置未完整跑通
- **详情页 SKU 批量**：目前只在用户点「看规格」时单件访问（详情页重，不适合批量）
- **京东详情页**：`#choose-attrs` 只有规格名，价格需逐 SKU 请求，未接

---

## 十一、一句话总结

淘宝踩的是 DOM 结构坑（广告位、哈希 class），
拼多多踩的是签名坑（anti_content），
Playwright 踩的是线程模型坑（对象绑线程）。

数据能不能用来比价，不取决于抓多少条，
而取决于**分类维度够不够细、假货筛不筛得掉**——
315 件里 13 件是钓鱼商品，混进去最低价就永远是假的。
