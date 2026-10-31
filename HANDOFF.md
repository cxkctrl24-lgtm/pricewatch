# pricewatch 交接速查（每轮先读这一份）

> 生成于 2026-10-31 15:45。任何新会话只需读本文件即可继续，不必回看长对话。

## 项目位置与运行

- 仓库：`E:\wb\pricewatch`（GitHub `cxkctrl24-lgtm/pricewatch`，SSH 已认证）
- 解释器：`E:\wb\pricewatch\.venv\Scripts\python.exe`
- 启动：双击 `启动.bat`（已固定 TMP/TEMP 到包内 `tmp/`，Playwright 在系统 Temp 会 EPERM）
- 服务：`http://127.0.0.1:8765`；当前后台 job `bash-ff5361c6341a45b2`
- 手动起：`cd /e/wb/pricewatch && TMP="E:\\wb\\pricewatch\\tmp" TEMP=... .venv/Scripts/python.exe run.py`
- 数据库：`data/pricewatch.db`（当前 products 302 / history 395 / snapshots 61）

## 平台现状（2026-10-31 实测）

| 平台 | 搜索可用 | 原因/备注 |
|---|---|---|
| 淘宝 | ✅ | 唯一稳定可采，302 条数据全来自淘宝；46 次快照 ok=25 |
| 京东 | ❌ | 搜索页 200 但商品容器 0 个，正文含「访问频繁」；**按 IP+频次风控，非指纹** |
| 拼多多 | ❌ | 搜索返回 339 字空壳页（仅「请稍后再试」）；首页/PC 站正常，只封搜索 |

京东 9 次快照 ok=0、PDD 6 次 ok=0，均跨 16 小时，属系统性封锁。

## 网络环境（关键前提）

- Clash **TUN 模式**（`Clash Tunnel` 网卡 Up）；`cfw-settings` 里 `randomControllerPort/randomMixedPort: true` → **1090/1091/7890 都查不到监听是正常的**
- 国内站解析全为 `198.18.0.x`（fake-ip，确认走 TUN）
- 实测延迟：`www.jd.com` 0.13s vs 境外 API 0.60s → **Clash 走规则模式，国内域名直连**
- 出口 IP：`142.249.36.x`（US/California，AS153371 BACK WAVES LIMITED），同 C 段轮换
- **不要用 `ip.cn` 判地区**（曾误报"中国广东"，是它自己的假数据）；用 `ipinfo.io/json`

## 已实现模块

| 文件 | 作用 |
|---|---|
| `net_route.py` | 出口探测与代理（`PROXY_URL`/`PLATFORM_PROXY`，探测失败安全降级，5 分钟缓存） |
| `repricing.py` | SKU 拆价：每 SKU 独立归类，一件商品贡献多个真实档位价 |
| `sku_cache.py` | 详情页 SKU 缓存 45 分钟；被风控页面拒写 |
| `card_classify.py` | 商品卡 vs 广告位：规则(0秒,~94%) + 模型(仅边界,~6%) |
| `llm_extract.py` | 大模型兜底：**仅正则 0 条时**调本机 Ollama 9b |

指向记忆节点：`mem_1793431330246_be9a7c`（出口管理 CCG 六要素）

## 已修的关键 bug（别回退）

1. `db.record_items` INSERT 16 列只给 15 个 `?` → 新商品必抛异常，整批丢弃
2. `runner.py` 循环外 `lo,hi` 被 `outlier.judge` 返回值遮蔽 → 每组只有第一个词能采
3. `runner.py` 16 处 `log()` 应为 `db.log()` → NameError 被吞，表现为"卡住但无日志"
4. 详情页 `page.goto` 挂起（卡 380-430s）→ 改**独立 daemon 线程 + join(23s) 硬截止**
5. 子线程读主 session cookie → `Cannot switch to a different thread` → 改**主线程预取 cookie 再传入**
6. `browser.py` `STEALTH_JS` 改写 `permissions.query`/`plugins` → 暴露"非原生"；已删除该改写，`hardwareConcurrency` 改读真实 CPU 核数，`screen` 修正为 1920×1080

## 当前配置快照（config.py）

- `SKU_REPRICE=True`（配了缓存才敢开）、`SKU_REPRICE_MAX_PER_KEYWORD=8`、`SKU_REPRICE_BUDGET=90`
- `CARD_CLASSIFY=True`、`CARD_CLASSIFY_MAX_CALLS=12`、`LLM_CARD_MODEL=qwen3.5:9b`
- `LLM_FALLBACK=True`、`LLM_MODEL=qwen3.5:9b`（Ollama `127.0.0.1:11434`，think=false）
- `PROXY_URL=""`（直连）、`PLATFORM_PROXY={}`
- `USE_REAL_VIEWPORT=True`、`REAL_WINDOW_SIZE=(1280,720)`、`REAL_SCREEN_SIZE=(1920,1080)`
- 分平台节奏 `PLATFORM_PACING`：jd 冷却 1800s / kw_gap 10-22s；hits 翻倍放慢（`PACING_BOOST`）

## 已知未解 / 可选下一步

- 京东要恢复只能：①换未被标记的国内 IP；②把 jd `kw_gap` 调到 60-120s 只搜 1 个词试水
- PDD 若要复活：需 AccessToken（已在 `_access_token()` 实现）+ 国内出口
- 可加：价格趋势预测（`price_history` 已有）、Session 保活池、错误分类重试

## 测试脚本（tmp/，可复用）

`fp_compare.py`（指纹对比）、`jd_search_test.py`（京东搜索实测）、`sku_norm_probe.py`（规格名）、`card_cls_probe.py`（卡片判别）、`llm_scale.py`（模型规模实测）

## 分析报告（analysis/）

`github_survey.md`（GitHub 同类项目调研）、`optimization_plan.md`（优化方案+安卓虚拟机不可行实测）、`llm_page_analysis.md`（大模型接入实测数据）
