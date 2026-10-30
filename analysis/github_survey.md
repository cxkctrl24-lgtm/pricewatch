# GitHub 同类项目调研报告

> 调研日期：2026-10-31　目标：为 pricewatch 找可借鉴的开源实现
> 搜索方式：GitHub search/repositories API（10 组关键词，85 个去重仓库）+ 源码浅克隆实证

## 一、结论先说

**没有可以直接复用的成熟项目。** 这个方向上「README 漂亮但核心是空壳」的项目极多，
真正能跑的三个各有硬伤，且都绕不开我们已经踩过的坑。最有价值的不是代码，
而是几条**用别人的失败换来的情报**（见第四节）。

## 二、核心候选实测

| 仓库 | 星 | 真实度 | 关键发现 |
|---|---|---|---|
| `DAILtech/PriceDive` | 92 | ❌ **空壳** | `fetch_price()` 直接 `random.uniform(-50,50)` 返回随机数，注释写着 "SIMULATION"；采集逻辑全在注释块里。654 行里没有一行真抓取 |
| `wangdw495/ecommerce-price-analysis` | 51 | ⚠️ 有实现但过时 | 1 万行、38 文件，淘宝/JD/亚马逊/抖音采集器齐全。但淘宝用纯 `requests` + 正则找 `g_page_config` —— **这是 2018 年前的老淘宝页面结构**，早废弃；无登录态处理 |
| `Kumagt/price-monitor` | 28 | ⚠️ 不自己爬 | 走 `appapi.maishou88.com` 第三方买手 API，需邀请码。本质是 API 客户端，不是爬虫 |
| `cmrhyq/TaobaoProductMonitor` | 41 | 待查 | 淘宝单平台监控 |
| `buhuipao/Commodity-analysis` | 46 | 待查 | Flask + 逻辑回归价格预测（可借鉴预测思路） |

高星项目（`Jack-Cherish/python-spider` 19.8k★、`shengqiangzhang/examples-of-web-crawlers` 14.7k★）
都是**教学合集**，讲通用技巧，不针对比价场景，反爬内容也大多停留在 2018 年水平。

## 三、和 pricewatch 的对比

| 维度 | pricewatch | 开源同类 |
|---|---|---|
| 登录态 | Playwright 持久化 profile + cookie 判定 | 多数没有；有也是明文 session |
| 反爬 | 拟真节奏（首页预热/滚动/随机间隔）、分平台 pacing、限流冷却持久化 | 基本只有「加 UA + sleep」 |
| SKU 多规格 | `repricing.py` 按 SKU 独立归类（**业内少见**） | 全部只看列表页标题 |
| 数据层 | SQLite + 三级过滤（假货指纹/离群/二手分档） | 多为单表存价格 |
| 界面 | Flask + 手写 SVG 曲线 + 双主题 | PriceDive 只有 matplotlib 静态图 |

**结论：在「SKU 拆价」和「反爬节奏」这两块，pricewatch 已经走在前面。**

## 四、真正值得吸收的四条情报

### 1. PDD 必须有 AccessToken 或好 IP（最重要）
`onetwo1/pinduoduo`（161★）README 明确写：

```
!!!拼多多策略修改, 列表页及搜索页都需要在请求头中加
  'AccessToken': "登录后的 token", 才可以访问!!!
!!!或者 IP 质量较好, 直接访问也是可以获取数据的!!!
```

这直接解释了我们 PDD 为什么返回空页（正文仅 340 字）。**修法**：登录后从 profile
里取 `AccessToken` 注入请求头；或在 `browser.py` 里对我们发起的 POST 请求做 header 改写。
比继续调选择器有意义得多。

配套：`njmxye/pdd-reverse`（23★）做 anti_content 参数的 AST 字面量还原，
`Alohahahahaha/pdd_AntiContent` 有参数文件。我们现有的「hook 页面自己发的请求」
其实更省事 —— 让页面自己算，我们只复制它的 header。

### 2. 淘宝详情页不可靠是共性，不是我们的 bug
`ecommerce-price-analysis` 的淘宝采集器只有列表页逻辑，详情页 `get_product_details`
用 `BeautifulSoup` 解析静态 HTML —— 说明作者也放弃了详情页。
结合我们实测（详情页 `page.goto` 超时都能挂几百秒），**默认关闭 SKU 补读是对的**。

### 3. 价格预测可借鉴
`buhuipao/Commodity-analysis`（46★，Flask + 逻辑回归）做了历史价格 → 未来价格预测。
我们有 `price_history` 表和按商品的曲线，接一个简单趋势判断（线性回归斜率 +
置信度）不算难，可以加到曲线卡片上显示「涨/跌趋势」。

### 4. 曲线可视化不必依赖图表库
`adrianhajdin/pricewise`（635★）是 Next.js 教程项目，图表用 recharts。
我们已经手写 SVG，反而更轻 —— 这条路不用改。

## 五、避坑清单（从这些项目学到的）

- **别信 README 的功能列表**，直接看 `fetch_price` / `collect` 这类入口函数有没有真实实现
- **别用 `g_page_config` 解析淘宝**，那是 2018 年前的页面结构
- **别指望第三方买手 API**，要邀请码、会限流、数据不透明
- 高星爬虫教学库的反爬手段普遍过时，照抄会立刻被拦

## 六、建议的下一步（按性价比排序）

1. **PDD 注入 AccessToken** —— 直接解决 PDD 空页，成本低收益高
2. **给曲线加趋势判断** —— 复用现有 `price_history`，加个斜率计算
3. 京东继续用冷却持久化（已有），不必再找开源方案
4. 淘宝 SKU 补读维持关闭；若真要精度，走「手动看规格」而非批量

## 附：原始数据

- 搜索结果：`tmp/gh_search.json`（85 仓库）、`tmp/gh_search2.json`
- 源码快照：`tmp/refs/PriceDive`、`tmp/refs/ecommerce-price-analysis`、
  `tmp/refs/price-monitor`、`tmp/pdd_ref`
