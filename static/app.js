/* 硬件比价台 前端逻辑 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const PLAT = window.__CONFIG__.platforms;
const PLAT_NAME = Object.fromEntries(PLAT.map(p => [p.id, p.name]));

let state = { login: {}, task: null, stats: {}, active: {} };
let items = [];
let filters = { group: "", platform: "", q: "", showReview: false, condition: "new" };
let viewMode = "spec";   // spec=分类比价, list=商品明细
let specs = [];
let loginPoll = null;

/* ---------- 工具 ---------- */
const fmtPrice = v => "¥" + Number(v).toFixed(2).replace(/\.00$/, "");
const fmtTime = ts => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(url, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const r = await fetch(url, opts);
  return r.json();
}

/* ---------- 状态轮询 ---------- */
async function pollState() {
  try {
    const s = await api("/api/state");
    if (!s.ok) return;
    state = s;
    $("#aliveDot").classList.toggle("off", false);
    renderPlatforms();
    renderTask();
    $("#resultCount").textContent = `${items.length} 条`;
  } catch (e) {
    $("#aliveDot").classList.add("off");
  }
}

function renderPlatforms() {
  $$(".pcard").forEach(card => {
    const pid = card.dataset.platform;
    const flag = state.login ? state.login[pid] : null;
    const badge = $("[data-role=login-badge]", card);
    const text = $("[data-role=login-text]", card);
    const busy = state.active && state.active[pid] === "login";
    badge.className = "badge";
    if (busy) {
      badge.textContent = "登录中";
      badge.classList.add("warn");
      text.textContent = "浏览器窗口已打开，扫码登录后关掉那个窗口即可";
    } else if (flag === true) {
      badge.textContent = "已登录";
      badge.classList.add("ok");
      text.textContent = "登录态已保存，可以采集";
    } else if (flag === false) {
      badge.textContent = "未登录";
      badge.classList.add("err");
      text.textContent = "需要扫码登录一次";
    } else {
      badge.textContent = "未知";
      text.textContent = "还没检测登录态，点「检测」或直接采集";
    }
  });
}

function renderTask() {
  const t = state.task;
  const wrap = $("#progressWrap");
  const bar = $("#progressBar");
  const txt = $("#progressText");
  const start = $("#startBtn");
  const cancel = $("#cancelBtn");
  const running = t && t.status === "running";

  start.disabled = running;
  cancel.disabled = !running;
  wrap.hidden = !t;

  if (!t) { $("#miniLog").innerHTML = ""; return; }

  const p = t.progress || { done: 0, total: 0, current: "" };
  const pct = p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : (t.status === "done" ? 100 : 0);
  bar.style.width = pct + "%";
  const statusMap = { running: "采集中", done: "已完成", need_login: "等待登录", rate_limited: "被限流", error: "出错", cancelled: "已取消" };
  const cur = p.current ? ` · ${p.current}` : "";
  txt.textContent = `${statusMap[t.status] || t.status} ${pct}%　(${p.done}/${p.total})${cur}　已抓到 ${t.items} 条（新增 ${t.new_items}）`;

  if (t.need_login && t.need_login.length) {
    txt.textContent += `　⚠ 需要登录：${t.need_login.map(x => PLAT_NAME[x] || x).join("、")}`;
  }
  if (t.rate_limited && t.rate_limited.length) {
    txt.textContent += `　⏳ 被限流：${t.rate_limited.map(x => PLAT_NAME[x] || x).join("、")}（等 10 分钟再跑）`;
  }

  const ml = $("#miniLog");
  const msgs = (t.messages || []).slice(-40);
  ml.innerHTML = msgs.map(m => {
    const [lvl, text] = Array.isArray(m) ? m : ["info", String(m)];
    return `<div class="l-${lvl}">${esc(text)}</div>`;
  }).join("");
  ml.scrollTop = ml.scrollHeight;

  if (!running) refreshItems();
  const last = Object.values(state.last || {})[0];
  $("#lastRunInfo").textContent = last ? fmtTime(last.ts) : "—";
}

/* ---------- 采集 ---------- */
async function startCollect() {
  const platforms = $$("#platformChips input:checked").map(i => i.value);
  const groups = $$("#groupChips input:checked").map(i => i.value);
  if (!platforms.length) return alert("至少选一个平台");
  if (!groups.length) return alert("至少选一个品类");
  const r = await api("/api/collect", { method: "POST", body: JSON.stringify({ platforms, groups }) });
  if (!r.ok) return alert(r.message || "启动失败");
  pollState();
}
$("#startBtn").addEventListener("click", startCollect);
$("#cancelBtn").addEventListener("click", async () => { await api("/api/cancel", { method: "POST" }); pollState(); });

/* ---------- 登录 ---------- */
async function doLogin(pid) {
  const groups = $$("#groupChips input:checked").map(i => i.value);
  $("#miniLog").insertAdjacentHTML("afterbegin",
    `<div class="l-warn">正在打开 ${PLAT_NAME[pid] || pid} 登录窗口，请在弹出的浏览器里扫码登录…</div>`);
  await api("/api/login", { method: "POST", body: JSON.stringify({ platform: pid, groups }) });
  pollState();
  startLoginPoll(pid);
}

async function doCheck(pid) {
  await api("/api/check_login", { method: "POST", body: JSON.stringify({ platform: pid }) });
  pollState();
}

let checkTimers = {};
function startLoginPoll(pid) {
  clearInterval(checkTimers[pid]);
  let ticks = 0;
  checkTimers[pid] = setInterval(async () => {
    ticks++;
    const flag = state.login ? state.login[pid] : null;
    if (flag === true || ticks > 40) {
      clearInterval(checkTimers[pid]);
      pollState();
      return;
    }
    if (ticks % 3 === 0) {
      await api("/api/check_login", { method: "POST", body: JSON.stringify({ platform: pid }) }).catch(() => {});
      pollState();
    }
  }, 3000);
}

$$("[data-role=login-btn]").forEach(b => b.addEventListener("click", () => doLogin(b.dataset.platform)));
$$("[data-role=check-btn]").forEach(b => b.addEventListener("click", () => doCheck(b.dataset.platform)));

/* ---------- 结果表 ---------- */
async function refreshItems() {
  const lvl = filters.showReview ? "all" : "ok";
  const c = filters.condition ? `&condition=${filters.condition}` : "";
  const s = await api(`/api/items?limit=1200&level=${lvl}${c}`);
  if (s.ok) { items = s.items || []; renderTable(); }
}

function renderTable() {
  let rows = items.filter(it => {
    if (filters.group && it.group_id !== filters.group) return false;
    if (filters.platform && it.platform !== filters.platform) return false;
    if (filters.q) {
      const q = filters.q.toLowerCase();
      const hay = (it.title + " " + (it.shop || "") + " " + it.keyword).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  // 按分类 -> 价格升序，让最低价一眼看到
  rows.sort((a, b) => {
    const g = (a.group_id || "").localeCompare(b.group_id || "");
    return g !== 0 ? g : a.price - b.price;
  });
  $("#resultCount").textContent = `${rows.length} 条`;
  const tb = $("#resultBody");
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="8" class="empty">${items.length ? "没有符合筛选条件的商品" : "还没有数据，先在左边点「开始采集」"}</td></tr>`;
    return;
  }
  tb.innerHTML = rows.map((it, i) => {
    const low = it.is_low;
    const priceHtml = `<span class="price ${low ? "low" : ""}">${fmtPrice(it.price)}</span>`;
    const minHtml = it.min_price < it.price - 0.01
      ? `<span class="muted">${fmtPrice(it.min_price)}</span>`
      : `<span class="muted">—</span>`;
    return `<tr>
      <td class="muted">${i + 1}</td>
      <td class="pname-cell"><a href="${esc(it.url)}" target="_blank" rel="noreferrer" title="${esc(it.title)}">${esc(it.title)}</a>${
        (it.price_level === "review") ? `<span class="suspect-tag" title="${esc(it.price_note||"")}">异常</span>` : ""
      }</td>
      <td><span class="ptag ${esc(it.platform)}">${esc(PLAT_NAME[it.platform] || it.platform)}</span></td>
      <td class="num">${priceHtml}</td>
      <td class="num">${minHtml}</td>
      <td class="muted">${esc(it.shop || "—")}</td>
      <td>${it.condition === "used"
          ? '<span class="cond-used">二手</span>'
          : '<span class="cond-new">一手</span>'}</td>
      <td class="speccell">${esc(it.spec_key || "—")}</td>
      <td class="muted">${fmtTime(it.last_seen)}
        <div><button class="sku-btn" data-url="${esc(it.url)}" data-plat="${esc(it.platform)}" data-title="${esc(it.title)}">看规格</button></div>
      </td>
    </tr>`;
  }).join("");

  $$(".sku-btn").forEach(b => b.addEventListener("click", () => loadSku(b)));
}

/* ---------- 异常清单 ---------- */
async function refreshReview() {
  const r = await api("/api/review");
  const rows = r.items || [];
  $("#reviewBody").innerHTML = rows.length ? rows.map(it => `
    <tr>
      <td class="pname-cell"><a href="${esc(it.url)}" target="_blank" rel="noreferrer" title="${esc(it.title)}">${esc(it.title)}</a></td>
      <td><span class="ptag ${esc(it.platform)}">${esc(PLAT_NAME[it.platform] || it.platform)}</span></td>
      <td class="num"><span class="price">${fmtPrice(it.price)}</span></td>
      <td class="speccell">${esc(it.spec_key || "—")}</td>
      <td class="reason-cell">${esc(it.price_note || "—")}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="empty">没有异常条目</td></tr>`;
}

/* ---------- 已剔除 ---------- */
async function refreshRejected() {
  const r = await api("/api/rejected");
  const rows = r.items || [];
  $("#rejectedBody").innerHTML = rows.length ? rows.map(it => `
    <tr>
      <td><span class="ptag ${esc(it.platform)}">${esc(PLAT_NAME[it.platform] || it.platform)}</span></td>
      <td class="num">${fmtPrice(it.price)}</td>
      <td class="pname-cell"><a href="${esc(it.url)}" target="_blank" rel="noreferrer">${esc(it.title)}</a></td>
      <td class="muted">${esc(it.shop || "—")}</td>
      <td class="reject-reason">${esc(it.reason || "—")}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="empty">没有剔除记录</td></tr>`;
}

/* ---------- 商品 SKU 明细 ---------- */
async function loadSku(btn) {
  const url = btn.dataset.url;
  const plat = btn.dataset.plat;
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = "读取中…";

  $("#skuTitle").textContent = "商品规格";
  $("#skuSub").textContent = "正在打开商品详情页读取规格，约 5–10 秒…";
  $("#skuBody").innerHTML = `<tr><td colspan="3" class="empty">加载中…</td></tr>`;
  $("#skuModal").hidden = false;

  try {
    const r = await api("/api/sku", { method: "POST", body: JSON.stringify({ url, platform: plat }) });
    const d = r.data || {};
    if (!r.ok) {
      $("#skuSub").textContent = r.message || "读取失败";
      $("#skuBody").innerHTML = `<tr><td colspan="3" class="empty">${esc(r.message || "读取失败")}</td></tr>`;
    } else {
      $("#skuTitle").textContent = d.title || "商品规格";
      const list = d.sku_prices || [];
      const lo = list.length ? list[0].price : 0;
      const hi = list.length ? list[list.length - 1].price : 0;
      $("#skuSub").textContent = (r.message || "") +
        (list.length > 1 ? `　跨度 ¥${lo.toFixed(0)} – ¥${hi.toFixed(0)}（差 ¥${(hi - lo).toFixed(0)}）` : "");
      $("#skuBody").innerHTML = list.length ? list.map(p => {
        const cls = p.price === lo ? "low" : p.price === hi ? "high" : "mid";
        return `<tr><td>${esc(p.spec)}</td>
          <td class="num"><span class="${cls}">${fmtPrice(p.price)}</span></td>
          <td class="muted">${esc(p.price_title || "—")}</td></tr>`;
      }).join("") : `<tr><td colspan="3" class="empty">这个商品只有单一规格</td></tr>`;
    }
  } catch (e) {
    $("#skuSub").textContent = "请求失败：" + e;
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

$("#skuClose").addEventListener("click", () => { $("#skuModal").hidden = true; });
$("#skuModal").addEventListener("click", e => { if (e.target.id === "skuModal") $("#skuModal").hidden = true; });
document.addEventListener("keydown", e => { if (e.key === "Escape") $("#skuModal").hidden = true; });

$$(".vbtn").forEach(b => b.addEventListener("click", () => {
  viewMode = b.dataset.view;
  applyView();
  if (viewMode === "spec") renderSpecs();
  else if (viewMode === "list") renderTable();
}));

/* ---------- 分类比价视图 ---------- */
async function refreshSpecs() {
  const p = filters.platform ? `&platform=${filters.platform}` : "";
  const g = filters.group ? `&group=${filters.group}` : "";
  const lvl = filters.showReview ? "all" : "ok";
  const c = filters.condition ? `&condition=${filters.condition}` : "";
  const r = await api(`/api/specs?limit=9999${p}${g}&level=${lvl}${c}`);
  if (r.ok) { specs = r.specs || []; renderSpecs(); }
}

function renderSpecs() {
  let rows = specs.slice();
  if (filters.q) {
    const q = filters.q.toLowerCase();
    rows = rows.filter(r => (r.spec_key + " " + (r.spec_label||"") + " " + (r.best_title||"")).toLowerCase().includes(q));
  }
  const tb = $("#specBody");
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="8" class="empty">还没有分类数据，先采集一次</td></tr>`;
    return;
  }
  tb.innerHTML = rows.map(r => {
    const pct = r.spread_pct >= 20;
    const gapCls = pct ? "gap-hi" : "gap-lo";
    return `<tr class="catrow">
      <td class="specname">${esc(r.spec_key)}</td>
      <td class="num"><span class="price low">${fmtPrice(r.min)}</span></td>
      <td class="num muted">${fmtPrice(r.max)}</td>
      <td class="num"><span class="${gapCls}">${r.spread > 0.01 ? "+" + fmtPrice(r.spread) : "—"}</span>${pct ? `<span class="pct">${r.spread_pct}%</span>` : ""}</td>
      <td class="num muted">${r.count}</td>
      <td><span class="ptag ${esc(r.best_platform)}">${esc(PLAT_NAME[r.best_platform] || r.best_platform)}</span></td>
      <td class="pname-cell"><a class="best-link" href="${esc(r.best_url)}" target="_blank" rel="noreferrer" title="${esc(r.best_title)}">${esc(r.best_title)}</a></td>
    </tr>`;
  }).join("");
}

$("#filterGroup").addEventListener("change", e => { filters.group = e.target.value; renderSpecs(); renderTable(); });
$("#filterPlatform").addEventListener("change", e => { filters.platform = e.target.value; renderSpecs(); renderTable(); });
$("#searchBox").addEventListener("input", e => { filters.q = e.target.value.trim(); renderSpecs(); renderTable(); });
$("#showReview").addEventListener("change", e => {
  filters.showReview = e.target.checked;
  refreshItems().then(() => refreshSpecs());
});
$("#filterCond").addEventListener("change", e => {
  filters.condition = e.target.value;
  refreshItems().then(() => refreshSpecs());
});

/* ---------- 关键词管理 ---------- */
async function reloadKeywords() {
  const r = await api("/api/keywords");
  if (!r.ok) return;
  const box = $("#keywordGroups");
  box.innerHTML = r.groups.map(g => `
    <details class="kwgroup">
      <summary>${esc(g.name)} <span class="muted">(${g.keywords.length} 个词)</span></summary>
      <div class="kwlist">
        ${g.keywords.map(k => `<div class="kwrow">
          <label class="switch">
            <input type="checkbox" class="kw-toggle" data-group="${g.id}" data-kw="${esc(k.keyword)}" ${k.enabled ? "checked" : ""}>
            <span class="kwtext">${esc(k.keyword)}</span>
          </label>
          <button class="xbtn kw-del" data-kw="${esc(k.keyword)}" title="删除">×</button>
        </div>`).join("")}
        <div class="kwadd">
          <input type="text" class="kw-input" data-group="${g.id}" placeholder="加一个词，比如 DDR5 6400 32G">
          <button class="ghost-btn tiny kw-add" data-group="${g.id}">加入</button>
        </div>
      </div>
    </details>`).join("");
  bindKeywordEvents();
}

function bindKeywordEvents() {
  $$(".kw-toggle").forEach(cb => cb.addEventListener("change", async () => {
    await api("/api/keywords", { method: "POST", body: JSON.stringify({ action: "toggle", keyword: cb.dataset.kw, enabled: cb.checked }) });
  }));
  $$(".kw-del").forEach(b => b.addEventListener("click", async () => {
    if (!confirm(`删除关键词「${b.dataset.kw}」？`)) return;
    await api("/api/keywords", { method: "DELETE", body: JSON.stringify({ keyword: b.dataset.kw }) });
    reloadKeywords();
  }));
  $$(".kw-add").forEach(b => b.addEventListener("click", async () => {
    const input = $(`.kw-input[data-group="${b.dataset.group}"]`);
    const kw = input.value.trim();
    if (!kw) return;
    const r = await api("/api/keywords", { method: "POST", body: JSON.stringify({ action: "add", group_id: b.dataset.group, keyword: kw }) });
    if (!r.ok) alert(r.message);
    else input.value = "";
    reloadKeywords();
  }));
  $$(".kw-input").forEach(inp => inp.addEventListener("keydown", e => {
    if (e.key === "Enter") $(".kw-add", inp.closest(".kwadd")).click();
  }));
}

/* ---------- 启动 ---------- */
function applyView() {
  $("#specWrap").hidden = viewMode !== "spec";
  $("#listWrap").hidden = viewMode !== "list";
  $("#reviewWrap").hidden = viewMode !== "review";
  $("#rejectedWrap").hidden = viewMode !== "rejected";
  $$(".vbtn").forEach(x => x.classList.toggle("active", x.dataset.view === viewMode));
  if (viewMode === "review") refreshReview();
  if (viewMode === "rejected") refreshRejected();
}

(async function boot() {
  applyView();
  await pollState();
  await refreshItems();
  await refreshSpecs();
  bindKeywordEvents();
  setInterval(pollState, 2000);
  setInterval(() => { if (!state.task || state.task.status !== "running") { refreshItems(); refreshSpecs(); } }, 4000);
})();