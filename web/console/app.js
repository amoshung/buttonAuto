// AI 工程助理 主控台：辦公室、日誌、設定。資料只用 textContent 放進畫面（不使用 innerHTML），所有會動手的請求都帶 X-Token。
'use strict';

const TOKEN = document.querySelector('meta[name="aea-token"]').content;
const LOG_FILTERS = [['all', '全部'], ['action', '動作'], ['human', '需要你'], ['think', 'AI 判斷'], ['warn', '警告與錯誤']];
const VIEWS = ['office', 'logs', 'settings'];

const S = {
  snap: null, view: 'office', logs: [], logAfter: 0, toastAfter: 0, logFilter: 'all', logQuery: '', logFollow: true,
  settings: null, connected: false, notify: localStorage.getItem('aea.notify') === '1',
  sound: localStorage.getItem('aea.sound') !== '0',
};

// ---------------------------------------------------------------- 小工具
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (sel, root = document) => root.querySelector(sel);
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
const short = (s, n) => { s = String(s || '').replace(/\s+/g, ' ').trim(); return s.length <= n ? s : s.slice(0, n - 1) + '…'; };

async function api(path, body) {
  const opt = body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN }, body: JSON.stringify(body) };
  try {
    const res = await fetch(path, opt);
    const data = await res.json().catch(() => ({ ok: false, text: `HTTP ${res.status}` }));
    if (res.status === 401) location.reload();
    return data;
  } catch (e) {
    return { ok: false, text: '連不上助理後端：請執行 restart.bat' };
  }
}
async function act(path, body, opts = {}) {
  const r = await api(path, body);
  if (r && r.text && !opts.silent) toast(r.text, r.ok !== false);
  return r;
}
function toast(text, ok = true) {
  const box = $('#toasts');
  const t = h('div', { class: 'toast' + (ok ? '' : ' bad'), role: 'status' }, text);
  box.append(t);
  setTimeout(() => t.remove(), ok ? 4200 : 8000);
}
function beep() {
  if (!S.sound) return;
  try {
    const ctx = new AudioContext(); const o = ctx.createOscillator(); const g = ctx.createGain();
    o.frequency.value = 660; g.gain.setValueAtTime(0.08, ctx.currentTime); g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
    o.connect(g); g.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.35);
  } catch (e) { /* 沒有音效裝置 */ }
}


// 收件匣多了新項目：響一聲；分頁在背景且開了瀏覽器通知就跳通知
function notifyNeeds(title) {
  beep();
  if (S.notify && 'Notification' in window && Notification.permission === 'granted' && document.hidden) {
    new Notification('AI 工程助理：需要你決定', { body: title || '收件匣有新項目' });
  }
}

// ---------------------------------------------------------------- 即時連線
let es = null;
function connect() {
  if (es) es.close();
  es = new EventSource(`/api/stream?log=${S.logAfter}&toast=${S.toastAfter}`);
  es.addEventListener('state', (ev) => { setConnected(true); onState(JSON.parse(ev.data)); });
  es.addEventListener('team', (ev) => { setConnected(true); if (window.Office) window.Office.onState(JSON.parse(ev.data)); });
  es.addEventListener('log', (ev) => {
    const items = JSON.parse(ev.data);
    if (!items.length) return;
    S.logAfter = items[items.length - 1].id;
    S.logs.push(...items);
    if (S.logs.length > 1500) S.logs.splice(0, S.logs.length - 1500);
    if (S.view === 'logs') appendLogs(items);
  });
  es.addEventListener('toast', (ev) => {
    for (const t of JSON.parse(ev.data)) {
      S.toastAfter = Math.max(S.toastAfter, t.id);
      if (Date.now() / 1000 - t.at < 30) toast(t.text, t.ok);
    }
  });
  es.onerror = () => {
    setConnected(false);
    // EventSource 會自己重連；連線斷太久就重建（伺服器重新啟動後換了連線參數）
    clearTimeout(connect._t);
    connect._t = setTimeout(() => { if (!S.connected) connect(); }, 4000);
  };
}
function setConnected(ok) {
  if (S.connected === ok) return;
  S.connected = ok;
  const c = $('#conn');
  c.classList.toggle('bad', !ok);
  c.textContent = ok ? '已連線' : '連線中斷，重新連線中…';
}
function onState(snap) {
  S.snap = snap;
  const a = snap.app;
  $('#ver').textContent = a.version;
  const banner = $('#banner');
  if (a.code_updated) {
    clear(banner).append(h('span', {}, '磁碟上的程式比執行中的新。'),
      h('button', { class: 'btn', onclick: restartBackend }, '重新啟動後端套用'));
    banner.classList.remove('hidden');
  } else banner.classList.add('hidden');
}

// ---------------------------------------------------------------- 上方列與導覽
// 由 office.js 在每次辦公室快照後呼叫：標題數字、導覽列徽章、上方的全辦公室狀態
function refreshTitle() {
  const n = window.Office ? window.Office.count() : 0;
  document.title = (n ? `(${n}) ` : '') + 'AI 工程助理';
  renderNav();
  renderRun();
}
function renderRun() {
  const st = window.Office ? window.Office.stats() : null;
  const run = $('#run');
  if (!st) return;
  run.className = 'run ' + (st.waiting ? 'needs' : st.active ? 'on' : 'off');
  const parts = [`運作中 ${st.running}`, `等你 ${st.waiting}`, `排隊 ${st.queued}`];
  const what = parts.join('、');
  if ($('#run-what').textContent !== what) $('#run-what').textContent = what;
  const sub = st.paused ? `另有 ${st.paused} 間暫停` : st.total ? '' : '還沒有房間';
  if ($('#run-sub').textContent !== sub) $('#run-sub').textContent = sub;
  $('#run-btn').disabled = !st.active;
}
async function pauseAll() {
  const st = window.Office ? window.Office.stats() : null;
  if (!st || !st.active) { toast('沒有運作中或排隊的房間', true); return; }
  const btn = $('#run-btn');
  btn.disabled = true;
  await act('/api/team/pause_all', {});
  renderRun();
}
function renderNav() {
  for (const btn of document.querySelectorAll('.nav button[data-view]')) {
    btn.setAttribute('aria-current', btn.dataset.view === S.view ? 'page' : 'false');
    const cnt = btn.querySelector('.count');
    const needs = window.Office ? window.Office.count() : 0;
    if (cnt) { cnt.textContent = needs; cnt.classList.toggle('hidden', !needs); }
  }
}
function go(view) {
  if (!VIEWS.includes(view)) view = 'office';
  S.view = view;
  history.replaceState(null, '', view === 'office' ? location.pathname : '#' + view);
  renderNav();
  const main = clear($('#main'));
  if (view === 'office') window.Office.render(main);
  else if (view === 'logs') renderLogsView(main);
  else if (view === 'settings') renderSettings(main);
  main.focus({ preventScroll: true });
}
async function restartBackend() {
  const r = await act('/api/restart', {});
  if (r.ok) setTimeout(() => location.reload(), 9000);
}

// ---------------------------------------------------------------- 日誌
function renderLogsView(main) {
  const list = h('div', { class: 'logs', id: 'logs', role: 'log', 'aria-live': 'off' });
  list.addEventListener('scroll', () => { S.logFollow = list.scrollTop + list.clientHeight >= list.scrollHeight - 30; });
  const q = h('input', { type: 'text', placeholder: '搜尋（專案名稱、指令、關鍵字）', value: S.logQuery });
  q.addEventListener('input', () => { S.logQuery = q.value; fillLogs(); });
  main.append(
    h('div', { class: 'view-title' }, h('h1', {}, '日誌'), h('p', {}, '助理的系統訊息；每間房的細節在房間的「最近發生的事」。')),
    h('div', { class: 'logbar' }, q,
      h('div', { class: 'filter row', role: 'group' }, LOG_FILTERS.map(([id, name]) => h('button', {
        'aria-pressed': String(S.logFilter === id), onclick: (e) => {
          S.logFilter = id; for (const b of e.target.parentNode.children) b.setAttribute('aria-pressed', String(b === e.target)); fillLogs();
        } }, name))),
      h('button', { class: 'btn ghost', onclick: () => act('/api/open', { what: 'log' }) }, '開啟 automation.log')),
    list);
  fillLogs();
}
function logMatch(l) {
  if (S.logFilter === 'warn' && !['warn', 'error'].includes(l.level)) return false;
  if (!['all', 'warn'].includes(S.logFilter) && l.level !== S.logFilter) return false;
  return !S.logQuery || l.text.toLowerCase().includes(S.logQuery.toLowerCase());
}
function fillLogs() {
  const list = $('#logs'); if (!list) return;
  list.replaceChildren(...S.logs.filter(logMatch).slice(-800).map((l) => h('div', { class: l.level }, l.text)));
  if (!list.childNodes.length) list.append(h('div', { class: 'faint' }, '沒有符合的紀錄。'));
  list.scrollTop = list.scrollHeight;
}
function appendLogs(items) {
  const list = $('#logs'); if (!list) return;
  for (const l of items) if (logMatch(l)) list.append(h('div', { class: l.level }, l.text));
  while (list.childNodes.length > 1200) list.removeChild(list.firstChild);
  if (S.logFollow) list.scrollTop = list.scrollHeight;
}

// ---------------------------------------------------------------- 設定
async function loadSettings() { S.settings = await api('/api/settings'); return S.settings; }
const ROLLOVER_MIN = 0.5;
const ROLLOVER_MAX = 0.9;

async function renderSettings(main) {
  main.append(h('div', { class: 'view-title' }, h('h1', {}, '設定'), h('p', {}, '辦公室的排程、換手與危險指令規則。危險指令一律交給你。')));
  const s = await loadSettings();
  if (S.view !== 'settings') return;
  const ai = s.ai || {};
  const wrap = h('div', { class: 'settings' });

  // 模型設定檔狀態（litellm.local.json：glm-5.3 與 Decision-1）
  const m = s.models || {};
  wrap.append(h('section', { class: 'sect' }, h('h3', {}, '判斷模型'),
    h('div', { class: 'box' }, h('p', {}, h('span', { class: 'light ' + (m.ready ? 'green' : 'red') }), ' ',
      m.ready ? '已讀到模型設定：權限灰色地帶由 Decision-1 與 glm-5.3 判斷。' : '沒有模型設定：代理的權限請求會全部交給你。'),
    h('p', { class: 'hint' }, '設定檔：', h('code', {}, m.file || 'litellm.local.json'), '（base_url、key、decision1_key；金鑰不會顯示在這裡）'))));

  // 模型（Task 18）：座位模型、判斷與撰寫用途
  wrap.append(modelsSection());

  // 辦公室
  const tf = {};
  const ores = h('div', { class: 'result' });
  const tfield = (k, name, hint, attrs) => {
    tf[k] = h('input', Object.assign({ type: 'number', id: 'f-' + k, value: ai[k] }, attrs));
    return h('div', { class: 'field' }, h('label', { for: 'f-' + k }, name), tf[k], h('p', { class: 'hint', id: 'h-' + k }, hint));
  };
  let roll = Number(ai.team_context_rollover);
  let rollHint = '實作代理的對話用量到這個比例，就存檔並開新對話接續';
  if (!(roll >= ROLLOVER_MIN && roll <= ROLLOVER_MAX)) {
    const was = roll;
    roll = Math.min(ROLLOVER_MAX, Math.max(ROLLOVER_MIN, Number.isFinite(roll) ? roll : 0.7));
    rollHint = `目前存的是 ${was}，超出這裡可調的範圍，已先調成 ${roll}；按「儲存辦公室設定」才會生效。`;
  }
  wrap.append(h('section', { class: 'sect' }, h('h3', {}, '辦公室', h('small', {}, '多代理房間的排程與換手')),
    h('div', { class: 'box' }, h('div', { class: 'form' },
      tfield('team_max_running', '同時運作的房間上限', '超過的房間排隊等空位（1～16）', { min: 1, max: 16, step: 1 }),
      tfield('team_context_rollover', '對話用量換手門檻（0.5～0.9）', rollHint, { min: ROLLOVER_MIN, max: ROLLOVER_MAX, step: 0.05 }),
      tfield('team_cost_weight', '派工時成本的權重（0～1）', '越高越偏好便宜的座位', { min: 0, max: 1, step: 0.05 })),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: async () => {
      const body = {};
      for (const [k, el] of Object.entries(tf)) body[k] = Number(el.value);
      const r0 = body.team_context_rollover;
      const clamped = Math.min(ROLLOVER_MAX, Math.max(ROLLOVER_MIN, Number.isFinite(r0) ? r0 : 0.7));
      body.team_context_rollover = clamped;
      tf.team_context_rollover.value = clamped;
      const r = await act('/api/settings/ai', body);
      ores.replaceChildren(h('span', { class: r.ok ? 'ok' : 'bad' }, r.text + (clamped !== r0 ? `（換手門檻調到可用範圍：${clamped}）` : '')));
    } }, '儲存辦公室設定')), ores)));
  tf.team_context_rollover.value = roll;

  // 危險指令規則
  const deny = h('textarea', { class: 'tall mono', spellcheck: 'false' }, s.deny_patterns.join('\n'));
  const tester = h('input', { type: 'text', placeholder: '貼一段指令試試看，例如 git push --force origin main' });
  const tres = h('div', { class: 'result' });
  const dres = h('div', { class: 'result' });
  tester.addEventListener('input', async () => {
    if (!tester.value.trim()) { tres.replaceChildren(); return; }
    const r = await api('/api/settings/deny/test', { text: tester.value });
    tres.replaceChildren(h('span', { class: r.hit ? 'bad' : 'ok' }, r.text));
  });
  wrap.append(h('section', { class: 'sect' }, h('h3', {}, '危險指令規則', h('small', {}, '每行一條正規表示式（不分大小寫）；命中就一律交給你')),
    h('div', { class: 'box' }, deny,
      h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: async () => {
        const r = await act('/api/settings/deny', { patterns: deny.value.split('\n') }); dres.replaceChildren(h('span', { class: r.ok ? 'ok' : 'bad' }, r.text));
      } }, '儲存規則')), dres,
      h('div', { class: 'field' }, h('label', {}, '測試'), tester), tres)));
  main.append(wrap);
}

// ---------------------------------------------------------------- 設定：模型（Task 18）
// 資料來自 GET /api/team/models；draft 保留你還沒儲存的選擇（重新偵測後畫面更新也不會清掉）
const MS = { view: null, draft: null, box: null, poll: 0, open: {} };
const AUTO_LABEL_DEFAULT = '自動（Decision-1 在 Claude／GLM 中選）';

function modelsDraft(v) {
  const d = { seats: {}, purposes: Object.assign({}, v.purposes), rows: {} };
  for (const seat of v.order) {
    const s = v.seats[seat];
    d.seats[seat] = s.pref || '';
    d.rows[seat] = s.models.map((m) => ({ id: m.id, name: m.name, description: m.description, tier: m.tier || '',
      cost: m.cost, missing: !!m.missing, inferred: !!m.inferred }));
  }
  return d;
}
// 偵測後清單變了：沿用草稿裡的等級／成本，補上新偵測到的模型
function mergeDraft(v) {
  const old = MS.draft;
  const d = modelsDraft(v);
  if (!old) return d;
  for (const seat of v.order) {
    if (seat in old.seats) d.seats[seat] = old.seats[seat];
    const prev = Object.fromEntries((old.rows[seat] || []).map((r) => [r.id, r]));
    d.rows[seat] = d.rows[seat].map((r) => (prev[r.id] ? Object.assign({}, r, { tier: prev[r.id].tier, cost: prev[r.id].cost, inferred: prev[r.id].inferred, dirty: prev[r.id].dirty }) : r));
    for (const r of old.rows[seat] || []) if (r.added && !d.rows[seat].some((x) => x.id === r.id)) d.rows[seat].push(r);
  }
  d.purposes = Object.assign({}, old.purposes);
  return d;
}

function modelsSection() {
  const box = h('div', { class: 'box models', id: 'models-box' }, h('p', { class: 'hint' }, '讀取模型設定中…'));
  MS.box = box; MS.draft = null;
  loadModels();
  return h('section', { class: 'sect', id: 'sect-models' },
    h('h3', {}, '模型', h('small', {}, '都預設「自動」；房間裡另外指定的會優先。只存模型 id，不含金鑰')), box);
}
async function loadModels() {
  const v = await api('/api/team/models');
  if (!MS.box || !document.body.contains(MS.box)) return;
  if (!v || !v.seats) { clear(MS.box).append(h('p', { class: 'bad' }, (v && v.text) || '讀不到模型設定')); return; }
  MS.view = v;
  MS.draft = mergeDraft(v);
  drawModels();
  const busy = v.order.some((s) => v.seats[s].detecting);
  clearTimeout(MS.poll);
  if (busy) MS.poll = setTimeout(loadModels, 1000);
}
function tierSelect(seat, row) {
  const sel = h('select', { 'aria-label': `${row.id} 的等級`, class: 'm-tier' },
    MS.view.tiers.map((t) => h('option', { value: t.id }, t.label)));
  sel.value = row.tier || '';
  sel.addEventListener('change', () => { row.tier = sel.value; row.inferred = false; row.dirty = true; if (row.tier && !(Number(row.cost) > 0)) row.cost = { economy: 1, standard: 3, premium: 15 }[row.tier]; drawModels(); });
  return sel;
}
function costInput(row) {
  const inp = h('input', { type: 'number', min: 0, max: 1000, step: 0.5, value: row.cost, 'aria-label': `${row.id} 的相對成本`, class: 'm-cost' });
  inp.disabled = !row.tier;
  inp.addEventListener('input', () => { row.cost = Number(inp.value); row.inferred = false; row.dirty = true; });
  return inp;
}
function seatCard(seat) {
  const v = MS.view.seats[seat];
  const d = MS.draft;
  const rows = d.rows[seat];
  const head = h('div', { class: 'm-head' },
    h('strong', {}, v.label),
    h('span', { class: 'tag' }, { detected: 'ACP 偵測', catalog: '目錄', fixed: '固定' }[v.source]),
    v.available ? null : h('span', { class: 'tag' }, '未安裝'));
  let status = null;
  if (v.source === 'detected') {
    let txt;
    if (v.detecting) txt = '偵測中…（只開對話讀清單，不送訊息、不花 token）';
    else if (v.error && !v.known) txt = v.error;
    else if (v.known) txt = `上次偵測 ${(v.detected_at || '').replace('T', ' ')}；CLI 目前用 ${v.current || '（未回報）'}${v.error ? '；最近一次：' + v.error : ''}`;
    else txt = '還沒偵測過可用模型';
    status = h('div', { class: 'row m-status' }, h('span', { class: 'hint' }, txt),
      h('button', { class: 'btn', disabled: v.detecting || !v.available, 'data-detect': seat,
        onclick: () => detectModels(seat) }, v.detecting ? '偵測中…' : '重新偵測'));
  }
  // 座位用哪個模型
  const pick = h('select', { id: 'm-seat-' + seat, 'aria-label': `${v.label} 使用的模型` },
    h('option', { value: '' }, MS.view.auto_label || AUTO_LABEL_DEFAULT),
    rows.filter((r) => !r.missing).map((r) => h('option', { value: r.id }, r.name && r.name !== r.id ? `${r.id}（${r.name}）` : r.id)));
  pick.value = d.seats[seat] || '';
  if (pick.value !== (d.seats[seat] || '')) { // 你選的模型已不在清單：照樣顯示，讓你知道
    pick.append(h('option', { value: d.seats[seat] }, `${d.seats[seat]}（不在可用清單）`)); pick.value = d.seats[seat];
  }
  pick.addEventListener('change', () => { d.seats[seat] = pick.value; });
  const field = h('div', { class: 'field' }, h('label', { for: 'm-seat-' + seat }, '使用的模型'), pick,
    v.source === 'detected' && !v.known ? h('p', { class: 'hint' }, '偵測到可用模型後才能指定；現在只能用「自動」') : null);
  // 等級與成本
  let table = null;
  if (v.auto_eligible === false) {
    // Kiro／Antigravity：自動派工不會選，等級與成本用不到
    table = h('p', { class: 'hint m-manual' }, MS.view.manual_only_text || '只在你指定時使用（自動派工不會選）');
  } else if (rows.length && v.editable) {
    const tiered = rows.filter((r) => r.tier).length;
    table = h('details', { class: 'm-tiers', open: seat === 'claude' || !!tiered },
      h('summary', {}, `等級與相對成本（自動派工用；已標 ${tiered}／${rows.length} 個）`),
      h('table', { class: 'm-table' },
        h('thead', {}, h('tr', {}, h('th', {}, '模型'), h('th', {}, '等級'), h('th', {}, '相對成本'))),
        h('tbody', {}, rows.map((r) => h('tr', { class: r.missing ? 'missing' : '' },
          h('td', { class: 'm-idcell' }, h('code', { title: r.id }, r.id), r.description ? h('div', { class: 'faint m-desc', title: r.description }, r.description) : null,
            r.inferred ? h('span', { class: 'tag m-guess', title: '依模型名稱推定的等級；改了就以你選的為準' }, '推定') : null),
          h('td', {}, tierSelect(seat, r)), h('td', {}, costInput(r)))))));
    // 展開狀態跨重繪保留
    table.open = MS.open[seat] !== undefined ? MS.open[seat] : table.open;
    table.addEventListener('toggle', () => { MS.open[seat] = table.open; });
  } else if (!v.editable) {
    table = h('p', { class: 'hint' }, `固定用 ${rows.map((r) => r.id).join('、')}（${rows.map((r) => r.description).join('、')}）`);
  }
  let add = null;
  if (v.can_add) {
    const inp = h('input', { type: 'text', placeholder: '完整模型 id，例如 claude-opus-4-1-20250805', 'aria-label': '加入 Claude 模型 id' });
    add = h('div', { class: 'row m-add' }, inp, h('button', { class: 'btn', onclick: () => {
      const id = inp.value.trim();
      if (!id || /\s/.test(id)) { toast('模型 id 不能空白或含空格', false); return; }
      if (!rows.some((r) => r.id === id)) rows.push({ id, name: id, description: '完整 id（儲存後生效）', tier: 'standard', cost: 3, added: true });
      inp.value = ''; drawModels();
    } }, '加入模型'));
  }
  return h('div', { class: 'm-seat', 'data-seat': seat }, head, status, v.editable ? h('div', { class: 'form' }, field) : null, table, add);
}
function purposeFields() {
  const v = MS.view; const d = MS.draft;
  return h('div', { class: 'form' }, Object.entries(v.purpose_options).map(([key, p]) => {
    const sel = h('select', { id: 'm-purpose-' + key }, p.options.map((o) => h('option', { value: o.id }, o.label)));
    sel.value = d.purposes[key] || p.default;
    sel.addEventListener('change', () => { d.purposes[key] = sel.value; });
    return h('div', { class: 'field' }, h('label', { for: 'm-purpose-' + key }, p.label), sel);
  }));
}
function drawModels() {
  const v = MS.view;
  const res = h('div', { class: 'result', id: 'models-result' });
  clear(MS.box).append(
    h('p', { class: 'hint' }, '自動派工只在 Claude 與 GLM 之間選：Decision-1 依任務難度選等級（經濟／標準／高階），再挑標了那個等級、成本最低的模型。Kiro 與 Antigravity 只在你指定座位時使用。下面每個座位的「使用的模型」選了就固定用它。指定的模型不在可用清單時房間會停下來問你，不會默默改用別的。額度用完時仍會由 GLM 代班，恢復後回到你指定的模型。'),
    h('div', { class: 'm-seats' }, v.order.map(seatCard)),
    h('h4', { class: 'm-sub' }, '判斷與撰寫模型'),
    purposeFields(),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', id: 'models-save', onclick: () => saveModels(res) }, '儲存模型設定')),
    res);
}
async function saveModels(res) {
  const v = MS.view; const d = MS.draft;
  const body = { seats: {}, purposes: d.purposes, models: {} };
  for (const seat of v.order) {
    if (!v.seats[seat].editable) continue;
    body.seats[seat] = d.seats[seat] || '';
    // 只送你改過的列＋原本就存過的列（推定的等級不送，維持「推定」）；明確選的（含「不參與自動」）優先於推定
    body.models[seat] = d.rows[seat].filter((r) => r.dirty || !r.inferred).map((r) => ({ id: r.id, tier: r.tier || '', cost: Number(r.cost) || 0 }));
  }
  const r = await act('/api/team/models', body, { silent: true });
  res.replaceChildren(h('span', { class: r.ok ? 'ok' : 'bad' }, r.text || (r.ok ? '已儲存' : '儲存失敗')));
  if (r.ok && r.models) { MS.view = r.models; MS.draft = modelsDraft(r.models); drawModels(); $('#models-result').replaceChildren(h('span', { class: 'ok' }, r.text)); }
}
async function detectModels(seat) {
  const r = await act('/api/team/models/detect', { seat }, { silent: true });
  if (!r.ok) toast(r.text || '無法偵測', false);
  loadModels();
}

// ---------------------------------------------------------------- 指令面板（Ctrl+K）
function commands() {
  const list = [
    { name: '全部暫停', hint: 'F8', run: pauseAll },
    { name: '前往：辦公室', run: () => go('office') }, { name: '前往：日誌', run: () => go('logs') },
    { name: '前往：設定', run: () => go('settings') },
    { name: '重新啟動後端', run: restartBackend },
  ];
  if (window.Office) list.push(...window.Office.commands());
  return list;
}

function openPalette() {
  if ($('.palette-wrap')) return;
  let sel = 0; let items = commands();
  const input = h('input', { type: 'text', placeholder: '輸入要做的事或專案名稱', 'aria-label': '指令' });
  const ul = h('ul', { role: 'listbox' });
  const wrap = h('div', { class: 'palette-wrap', onclick: (e) => { if (e.target === wrap) wrap.remove(); } }, h('div', { class: 'palette' }, input, ul));
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    items = commands().filter((c) => !q || c.name.toLowerCase().includes(q));
    sel = Math.min(sel, Math.max(0, items.length - 1));
    ul.replaceChildren(...items.slice(0, 30).map((c, i) => h('li', { role: 'option', 'aria-selected': String(i === sel),
      onclick: () => { wrap.remove(); c.run(); } }, h('span', {}, c.name), c.hint ? h('small', {}, c.hint) : null)));
  };
  input.addEventListener('input', () => { sel = 0; draw(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, items.length - 1); draw(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); draw(); e.preventDefault(); }
    else if (e.key === 'Enter' && items[sel]) { wrap.remove(); items[sel].run(); }
    else if (e.key === 'Escape') wrap.remove();
  });
  document.body.append(wrap); draw(); input.focus();
}

// ---------------------------------------------------------------- 啟動
function init() {
  $('#run-btn').addEventListener('click', pauseAll);
  $('#chip-palette').addEventListener('click', openPalette);
  const nb = $('#chip-notify');
  const syncNotify = () => { nb.textContent = S.notify ? '瀏覽器通知：開' : '瀏覽器通知：關'; };
  nb.addEventListener('click', async () => {
    if (!S.notify && 'Notification' in window && Notification.permission !== 'granted') await Notification.requestPermission();
    S.notify = !S.notify && 'Notification' in window && Notification.permission === 'granted';
    localStorage.setItem('aea.notify', S.notify ? '1' : '0'); syncNotify();
  });
  syncNotify();
  for (const btn of document.querySelectorAll('.nav button[data-view]')) btn.addEventListener('click', () => go(btn.dataset.view));
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
    else if (e.key === 'F8') { e.preventDefault(); pauseAll(); }
  });
  const hash = decodeURIComponent(location.hash.slice(1));
  go(VIEWS.includes(hash) ? hash : 'office');
  connect();
  // 開機前就有的日誌：一次補齊
  api('/api/logs?after=0').then((r) => { if (r.logs && !S.logs.length) { S.logs = r.logs; S.logAfter = r.logs.length ? r.logs[r.logs.length - 1].id : 0; if (S.view === 'logs') fillLogs(); } });
}
init();
