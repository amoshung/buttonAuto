// 像素辦公室：每個專案一間房，代理坐在桌前工作；等你的事集中在右側與收件匣。
// 執行時使用 app.js 的共用工具（h、api、act、toast、clear、short、beep、go）。這支檔案在 app.js 之前載入，
// 所以只在函式被呼叫時才用到它們。資料一律 textContent／createElement，不用 innerHTML。
'use strict';

(function () {
  const PAGE = 4;
  const RM = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  const SEATS = ['kiro', 'agy', 'claude', 'glm', 'glm_high'];
  const SEAT = {
    kiro: { c: '#a77bf3', d: '#7652c4', g: 'K', name: 'Kiro' },
    agy: { c: '#3fc8e0', d: '#228ea3', g: 'A', name: 'Antigravity' },
    claude: { c: '#e2814f', d: '#a95a33', g: 'C', name: 'Claude' },
    glm: { c: '#62d07f', d: '#3a9a56', g: 'G', name: 'GLM' },
    glm_high: { c: '#2f8f52', d: '#1d6337', g: 'G+', name: 'GLM 高思考' },
  };
  const ROLE = { po: 'PO', tech_lead: 'Tech Lead', scrum: 'Scrum Master', qa: 'QA', reviewer: 'Reviewer' };
  // 模型呼叫的角色（含派工、權限把關）與「進行中」泡泡的動詞
  const ROLE_ALL = Object.assign({ dispatcher: '派工', policy: '權限把關' }, ROLE);
  const ROLE_VERB = { policy: '判斷權限', dispatcher: '派工', po: '補充驗收重點', qa: '檢查', reviewer: '審查', scrum: '督促', tech_lead: '思考' };
  const EFFORT = { low: '低思考量', normal: '一般思考量' };
  const NA = '—（未回報）';
  const CLI_DEFAULT = 'CLI 預設（未回報）';
  const ROLE_SHIRT = { po: '#7aa7d9', tech_lead: '#c9b37e', scrum: '#b98ab0', qa: '#8fc7b8', reviewer: '#9aa5b1' };
  const STATUS = { queued: '排隊中', running: '運作中', waiting_user: '等你', paused: '已暫停', error: '出錯', done: '完成', archived: '已封存' };
  const PHASE = { goal: '目標', spec: '驗收條件', build: '實作', test: '測試', review: '審查', done: '完成' };
  const KIND = { permission: '權限', approve_spec: '驗收條件', question: '問題', merge: '合併', error: '出錯', unverified: '測試待確認', login: '需要你登入' };
  const LOGIN_OPENED = '已開啟登入視窗，登入完成後會自動繼續（每 5 秒檢查一次）';
  const AUTONOMY = [
    ['careful', '保守', '權限多數交給你；驗收條件要你按「開工」'],
    ['balanced', '平衡', '低風險權限自動允許；驗收條件要你按「開工」'],
    ['bold', '積極', '低風險權限自動允許；驗收條件自動採用，直接開工'],
  ];
  const STEPS = ['選資料夾', '工作來源', '陣容', '確認'];
  const SEGMENTS = ['目標', '驗收條件', '實作與測試', '審查與合併'];
  const SEG_OF = { goal: 0, spec: 1, build: 2, test: 2, review: 3, done: 3 };

  const O = {
    snap: null, page: 0, root: null, els: null, drawer: null, opener: null, drafts: {}, open: {}, detail: {},
    detailSig: {}, prevStatus: {}, msg: {}, sig: {}, openDetails: new Set(), confetti: {}, timer: null, scale: 2, busy: {}, lastCount: -1,
    fetching: false, swipe: null, catalog: null,
    evd: {}, evdBusy: new Set(), evdFail: new Set(), evdVer: 0, act: {}, hall: new Map(), roll: {}, rollSeen: {}, changed: {}, close: null, frames: 0, raf: 0, rafT: 0,
  };

  // ============================================================ 資料整理
  const rooms = () => (O.snap ? O.snap.rooms.filter((r) => r.status !== 'archived')
    .slice().sort((a, b) => (a.order - b.order) || String(a.created_at).localeCompare(String(b.created_at))) : []);
  const inbox = () => (O.snap ? O.snap.inbox : []);
  const roomById = (id) => (O.snap ? O.snap.rooms.find((r) => r.id === id) : null);
  // 登入項目由多間房共用（rooms 列出所有受影響的房間）
  const itemsOf = (id) => inbox().filter((i) => i.room_id === id || (Array.isArray(i.rooms) && i.rooms.includes(id)));
  const pages = () => Math.max(1, Math.ceil((rooms().length + 1) / PAGE));
  const needsYou = (r) => r.status === 'waiting_user' || itemsOf(r.id).length > 0;
  const seatLabel = (seat) => (O.snap && O.snap.fleet && O.snap.fleet[seat] ? O.snap.fleet[seat].label : (SEAT[seat] ? SEAT[seat].name : seat || '未派'));
  const pct = (r) => Math.max(0, Math.min(100, Number(r.context_pct) || 0));
  const hms = (ts) => String(ts || '').slice(11, 19);
  // 數字只來自後端；null／undefined＝CLI 或模型沒有回報
  const num = (n) => (n === null || n === undefined ? NA : Number(n).toLocaleString('en-US'));
  const modelName = (model, effort, kind) => (model === 'scripted'
    ? `示範（假模型，代 ${kind === 'd1' ? 'Decision-1' : 'glm' + (EFFORT[effort] ? ' ' + EFFORT[effort] : '')}）`
    : (model || '') + (EFFORT[effort] ? `（${EFFORT[effort]}）` : ''));
  const shortModel = (model) => (model === 'scripted' ? '示範假模型' : model || '模型');
  const costText = (cost) => {
    const parts = Object.entries(cost || {}).map(([cur, v]) => `${Number(v).toFixed(4).replace(/0+$/, '').replace(/\.$/, '')} ${cur || '（幣別未回報）'}`);
    return parts.length ? parts.join('、') : NA;
  };
  const secsSince = (iso) => { const t = Date.parse(iso); return Number.isFinite(t) ? Math.max(0, Math.round((Date.now() - t) / 1000)) : 0; };
  const dur = (n) => (n >= 3600 ? `${Math.floor(n / 3600)} 小時 ${Math.floor((n % 3600) / 60)} 分` : n >= 60 ? `${Math.floor(n / 60)} 分 ${n % 60} 秒` : `${n} 秒`);
  const thinkText = (c) => `${shortModel(c.model)} ${ROLE_VERB[c.role] || '思考'}中…（${dur(secsSince(c.since))}）`;
  function count() { return inbox().length; }
  function stats() {
    const all = rooms();
    const by = (st) => all.filter((r) => r.status === st).length;
    // active＝「全部暫停」暫停得到的房間（運作中、排隊、等你）
    return { total: all.length, running: by('running'), waiting: all.filter(needsYou).length, queued: by('queued'),
      paused: by('paused'), active: by('running') + by('queued') + by('waiting_user') };
  }

  function deskSeat(r, role) {
    const seat = role === 'impl' ? (r.impl_seat || (r.impl_cli !== 'auto' ? r.impl_cli : ''))
      : (r.review_seat || (r.review_cli !== 'auto' ? r.review_cli : ''));
    return SEAT[seat] ? seat : '';
  }
  function deskText(r, role) {
    const seat = deskSeat(r, role);
    if (!seat) return { main: role === 'impl' ? '實作：等派工' : '審查：等派工', tag: '', sub: '' };
    // 實際模型 id 只用 CLI 回報的（impl_model_actual）；沒回報就寫明，不拿設定值冒充
    const actual = role === 'impl' ? r.impl_model_actual : r.review_model_actual;
    const asked = role === 'impl' ? r.impl_model : r.review_model;
    const model = actual || (asked ? `${asked}（指定，CLI 未回報）` : CLI_DEFAULT);
    const main = seatLabel(seat) + '・' + model;
    // 標籤只看後端記錄的派工來源（Assignment.by），不猜；說明是後端寫的派工理由（Decision-1 的分數或規則）
    const by = role === 'impl' ? r.impl_by : r.review_by;
    const reason = role === 'impl' ? r.impl_reason : r.review_reason;
    let tag = '';
    let tip = '';
    let sub = '';
    if (by === 'Decision-1') { tag = 'D1 派工'; tip = '由 Decision-1 派工'; }
    else if (by === 'rule') { tag = '規則'; tip = '判斷模型不可用或固定規則，依規則派工'; }
    else if (by === 'user') { tag = '你指定'; tip = '你指定的座位'; }
    else if (by === 'fallback') sub = r.fallback_from ? `代班（原 ${seatLabel(r.fallback_from)} 額度已滿）` : '代班（原座位額度已滿）';
    if (tag && reason && reason !== '使用者指定') tip += '：' + reason;
    return { main, tag, tip, sub, reason: reason || '' };
  }
  function statusLine(r) {
    const head = STATUS[r.status] || r.status;
    return { head, text: r.activity || (r.status === 'queued' ? '等空位開工' : '') };
  }
  // API 沒有逐條的結果：依房間階段與狀態推算（測試關卡是整組一起跑）
  function criteriaState(r, c) {
    const chk = String(c.check || '').trim();
    if (!chk || chk.toLowerCase() === 'manual') return ['manual', '需你確認'];
    if (r.phase === 'review' || r.phase === 'done') return ['ok', '關卡已通過'];
    if (r.phase === 'test') return ['paused', 'error'].includes(r.status) ? ['stop', '檢查中斷'] : ['run', '檢查中'];
    return ['todo', '待驗證'];
  }

  // replaceChildren 會把 null 變成字串 "null"：先濾掉
  function put(el, ...kids) { el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false)); return el; }
  function add(el, ...kids) { el.append(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false)); return el; }
  // 重繪一塊 DOM 時，把焦點還給同一個按鈕（以 data-fk 辨識），鍵盤使用者不會被快照更新打斷
  function keepFocus(container, fn) {
    const a = document.activeElement;
    const fk = a && container.contains(a) && a.dataset ? a.dataset.fk : null;
    fn();
    if (fk) { const b = container.querySelector(`[data-fk="${CSS.escape(fk)}"]`); if (b) b.focus(); }
  }
  const hash = (s) => window.OfficeScene.hash(s);

  // ============================================================ 像素房間：場景（只用快照的真實欄位）
  const S = window.OfficeScene;
  const { LW, LH, CH, SPOT } = S;
  const ROLE_INFO = {
    po: '產品負責人：把目標寫成可驗收的條件，等你批准',
    tech_lead: '技術負責人：把驗收條件拆成實作計畫，檢查計畫有沒有漏',
    scrum: '督促實作進度；對話太長時要代理存檔、換新對話',
    qa: '品管：自己跑測試、做換回原狀檢查，抓假測試',
    reviewer: '唯讀審查另一個 CLI 的改動',
  };
  const ROLE_DEFAULT = { po: '寫驗收條件', tech_lead: '寫實作計畫', scrum: '盯實作進度', qa: '跑測試中', reviewer: '看 diff' };
  const ITEM_SAY = { permission: '等你批准指令', approve_spec: '等你確認驗收條件', merge: '做好了，等你合併', question: '有問題想問你',
    error: '卡住了，等你處理', unverified: '等你確認測試有效' };
  const BUBBLE_MAX = 14; // 全形字
  const WALK_MAX = 1200; // 房內走路最長（毫秒）
  const HALL_WALK = 800; // 走廊走路
  const ROLL_MS = 1500; // 換新對話的一次性動畫

  // 寬度：全形 1、半形 0.5；超過就截斷加「…」
  function fit(text, max = BUBBLE_MAX) {
    const cps = Array.from(String(text || '').replace(/\s+/g, ' ').trim());
    let w = 0;
    for (let i = 0; i < cps.length; i++) {
      w += cps[i].codePointAt(0) > 0xff ? 1 : 0.5;
      if (w > max) return cps.slice(0, Math.max(1, i - 1)).join('') + '…';
    }
    return cps.join('');
  }
  const firstLine = (s) => String(s || '').split('\n').map((x) => x.trim()).find(Boolean) || '';
  const seatShort = (seat) => (SEAT[seat] ? SEAT[seat].name.split(' ')[0] : seat);
  const unsign = (t) => firstLine(String(t || '').replace(/^【[^】]*】/, ''));
  // 工具事件 → 「寫 calc.py」
  function toolSay(text) {
    const t = firstLine(text);
    const files = t.match(/[\w\-.]*[A-Za-z0-9_-]\.[A-Za-z][A-Za-z0-9]{0,7}\b/g);
    const file = files && files.length ? files[files.length - 1].split('/').pop() : '';
    // 動詞只看工具標題的第一個字，猜不到就不加動詞（只寫檔名）
    const words = t.toLowerCase().split(/[\s:：(（]+/).filter(Boolean);
    const lead = words[0] || '';
    let verb = '';
    if (lead === 'test' || lead === 'pytest' || (lead === 'npm' && words[1] === 'test')) verb = '跑測試';
    else if (['write', 'edit', 'create', 'update'].includes(lead)) verb = '寫';
    else if (['read', 'view', 'open', 'search'].includes(lead)) verb = '讀';
    else if (['run', 'exec', 'bash', 'shell'].includes(lead)) verb = '跑';
    if (file) return verb ? `${verb} ${file}` : file;
    return verb === '跑測試' ? verb : t;
  }
  // 測試關卡的結果：只認 runner 自己寫的 QA 範本句（kind say、role qa、不是模型補充說明），不從模型或代理的文字猜
  function qaVerdict(e) {
    if (!e || e.kind !== 'say' || e.role !== 'qa' || e.note) return '';
    const t = String(e.text || '');
    if (t.startsWith('【QA】測試關卡通過')) return 'pass';
    if (t.startsWith('【QA】測試關卡沒過') || t.startsWith('【QA】無法驗證測試是否有效')) return 'fail';
    return '';
  }
  // 登入成功：只認辦公室自己寫的通知（kind notice、沒有 who＝不是代理說的、固定句型）
  const loginOk = (e) => e.kind === 'notice' && !e.who && / 已登入，繼續工作$/.test(String(e.text || ''));
  // 房間最近的事件（快照帶最近 5 筆，舊→新）
  function lastEv(r, pred) {
    const evs = Array.isArray(r.events) ? r.events : [];
    for (let i = evs.length - 1; i >= 0; i--) if (pred(evs[i])) return evs[i];
    return null;
  }
  const recent = (e, sec) => !!e && secsSince(e.ts) <= sec;
  const after = (e, ref) => !!e && (!ref || String(e.ts) >= String(ref.ts));
  function personaHere(r) {
    const roles = (O.snap && O.snap.roles) || {};
    return r.persona && roles[r.persona] === r.id ? r.persona : '';
  }
  function modelOf(r, role) {
    const actual = role === 'impl' ? r.impl_model_actual : r.review_model_actual;
    const asked = role === 'impl' ? r.impl_model : r.review_model;
    return actual || (asked ? `${asked}（指定）` : 'CLI 預設（未回報）');
  }
  function byText(r, role) {
    const by = role === 'impl' ? r.impl_by : r.review_by;
    if (by === 'Decision-1') return '由 Decision-1 派工';
    if (by === 'rule') return '依規則派工';
    if (by === 'user') return '你指定的座位';
    if (by === 'fallback') return r.fallback_from ? `原座位 ${seatLabel(r.fallback_from)} 額度已滿，由它接手` : '原座位額度已滿，由它接手';
    return '';
  }
  function agentCard(r, role, seat) {
    const glm = seat.startsWith('glm');
    const fb = role === 'impl' && r.impl_by === 'fallback';
    const name = `${seatShort(seat)}（${fb ? '代班' : role === 'impl' ? '實作' : '審查'}）`;
    const what = role === 'impl' ? (glm ? '負責寫程式的代理（用公司模型）' : '負責寫程式的 CLI 代理')
      : (glm ? '唯讀審查改動的代理（用公司模型）' : '唯讀審查另一個 CLI 的改動');
    const body = fb ? `${byText(r, role)}；${what}，模型 ${modelOf(r, role)}`
      : [what, `模型 ${modelOf(r, role)}`, byText(r, role)].filter(Boolean).join('，');
    return { name, body };
  }

  // 一間房此刻的樣子：誰在哪、做什麼動作、說什麼（全部來自快照）
  function sceneOf(r, now) {
    const st = r.status; const ph = r.phase;
    const items = itemsOf(r.id);
    const persona = personaHere(r);
    const implSeat = deskSeat(r, 'impl'); const revSeat = deskSeat(r, 'review');
    const running = st === 'running';
    const phaseEv = lastEv(r, (e) => e.kind === 'phase');
    const qaSay = lastEv(r, (e) => qaVerdict(e) !== '');
    const confettiOn = (O.confetti[r.id] || 0) > now;
    const roll = O.roll[r.id] && now - O.roll[r.id].start < ROLL_MS + 4000;
    const merge = items.find((i) => i.kind === 'merge');
    const scene = { lit: st !== 'paused', bang: st === 'waiting_user' || items.length > 0, confetti: confettiOn, actors: [],
      frame: st === 'waiting_user' || items.length ? '#f08a24' : '', screens: {}, board: null };
    // 螢幕
    const off = ['queued', 'paused'].includes(st);
    scene.screens.impl = !implSeat || off ? 'off' : st === 'error' ? 'err' : st === 'waiting_user' ? 'wait' : st === 'done' ? 'ok'
      : ph === 'build' ? 'code' : ph === 'spec' ? 'doc' : 'idle';
    scene.screens.rev = !revSeat || off ? 'off' : st === 'done' ? 'ok' : running && ph === 'review' ? 'diff' : 'idle';
    // 測試機：綠／紅只看最近一次 QA 測試關卡的結果事件；其他情況是中性（跑中或關著）
    const verdict = qaSay ? qaVerdict(qaSay) : '';
    if (running && ph === 'test' && !after(qaSay, phaseEv)) scene.screens.test = 'run';
    else scene.screens.test = verdict === 'pass' ? 'green' : verdict === 'fail' ? 'red' : 'off';
    // 白板：驗收條件（現有的誠實狀態）＋便利貼＝實際 commit 數（只有合併項目帶 commit 清單；沒有就不畫）
    const crit = (r.criteria || []).map((c) => criteriaState(r, c)[0]);
    const commits = merge ? parseMerge(merge.detail).commits.length : 0;
    if (crit.length || commits) scene.board = { crit, commits, writing: running && ['po', 'tech_lead'].includes(persona) };

    // 誰在說話：進行中的模型呼叫先配給對應的人
    const fl = Array.isArray(r.inflight) ? r.inflight : [];
    const thinkFor = {};
    for (const c of fl) {
      const key = persona && c.role === persona ? 'role:' + persona : c.role === 'reviewer' && revSeat ? 'rev' : implSeat ? 'impl' : persona ? 'role:' + persona : '';
      if (key && !thinkFor[key]) thinkFor[key] = c;
    }
    const say = (text, tone) => (text ? { text: String(text), tone: tone || '' } : null);
    const toolEv = (who) => lastEv(r, (e) => e.kind === 'tool' && (!who || !e.who || e.who === who) && after(e, phaseEv));
    const quotaEv = lastEv(r, (e) => e.kind === 'quota');
    const loginEv = lastEv(r, loginOk);
    const rollEv = lastEv(r, (e) => e.kind === 'rollover');
    const item = items[0];
    const statusSay = (main) => {
      if (st === 'waiting_user' || item) {
        if (!main) return null; // 只讓實作代理舉手開口，其他人不重複喊
        if (item && item.kind === 'login') return say(`等你登入 ${seatShort(item.seat || implSeat)}`, 'wait');
        return say(item ? ITEM_SAY[item.kind] || '等你決定' : '等你決定', 'wait');
      }
      if (st === 'error') return say(main ? '出錯：' + firstLine(r.error || r.activity) : '先停下來', 'err');
      if (st === 'queued') return say(main ? '排隊等空位' : '排隊中');
      if (st === 'paused') return say(r.resume_requested ? '等停下後繼續' : '已暫停');
      if (st === 'done') {
        if (!main) return say('收工');
        return say(/^已合併/.test(r.activity || '') ? r.activity : '完成！', 'ok');
      }
      return null;
    };
    // 實作代理
    if (implSeat) {
      let spot = 'implSeat'; let pose = 'sit'; let markK = '';
      if (off) { spot = 'sofaA'; pose = 'sofa'; }
      else if (st === 'error') { pose = 'head'; markK = 'err'; }
      else if (st === 'waiting_user') { spot = 'implStand'; pose = 'hand'; }
      else if (st === 'done') { if (confettiOn) { spot = 'implStand'; pose = 'cheer'; } else { spot = 'coffeeA'; pose = 'mug'; } }
      else if (running) {
        if (ph === 'spec') { spot = 'boardSide'; pose = 'stand'; }
        else if (ph === 'build') pose = 'type';
      }
      let b = statusSay(true);
      if (!b && running) {
        const tool = toolEv(seatLabel(implSeat));
        if (roll || recent(rollEv, 20)) b = say('換新對話');
        else if (r.impl_by === 'fallback' && recent(quotaEv, 90)) b = say(`額度滿，${seatShort(implSeat)} 代班`);
        else if (recent(loginEv, 60)) b = say('已登入，繼續工作');
        else if (tool && ['build', 'spec'].includes(ph)) b = say(toolSay(tool.text));
        else b = say({ build: '寫程式中', spec: persona === 'tech_lead' ? '寫實作計畫' : '起草驗收條件', test: '等測試結果',
          review: '等審查意見', goal: '讀專案中' }[ph] || r.activity);
      }
      const card = agentCard(r, 'impl', implSeat);
      scene.actors.push({ key: 'impl', kind: 'agent', seat: implSeat, spot, pose, mark: markK, shirt: SEAT[implSeat].c,
        look: S.lookOf(r.id + implSeat + (r.rollovers || 0)), tag: r.impl_by === 'fallback' ? '代班' : '', deleg: !!r.delegate, think: thinkFor.impl,
        bubble: b, name: card.name, body: card.body });
    }
    // 審查代理
    if (revSeat) {
      let spot = 'revSeat'; let pose = 'sit';
      if (off) { spot = 'sofaB'; pose = 'sofa'; }
      else if (st === 'done') { if (confettiOn) { spot = 'revStand'; pose = 'cheer'; } else { spot = 'coffeeB'; pose = 'mug'; } }
      else if (running && ph === 'review') pose = 'type';
      let b = statusSay(false);
      if (!b) {
        const tool = running && ph === 'review' ? toolEv(seatLabel(revSeat)) : null;
        b = running && ph === 'review' ? say(tool ? toolSay(tool.text) : '審查 diff') : say(ph === 'review' ? '審查到一半' : '等著審查');
      }
      const card = agentCard(r, 'review', revSeat);
      scene.actors.push({ key: 'rev', kind: 'agent', seat: revSeat, spot, pose, shirt: SEAT[revSeat].c,
        look: S.lookOf(r.id + revSeat + 'rev'), think: thinkFor.rev, bubble: b, name: card.name, body: card.body });
    }
    // 角色（PO、Tech Lead、Scrum Master、QA、Reviewer）
    if (persona) {
      const waiting = st === 'waiting_user';
      const spot = { po: 'board', tech_lead: 'board', scrum: 'boardSide', qa: 'test', reviewer: 'implSide' }[persona] || 'boardSide';
      const pose = spot === 'board' && !waiting ? 'write' : 'stand';
      const lastSay = lastEv(r, (e) => e.kind === 'say' && e.role === persona);
      let b = waiting ? say('等你決定') : null;
      if (!b && lastSay && after(lastSay, phaseEv)) {
        const t = unsign(lastSay.text).replace('測試關卡通過', '測試通過').replace('測試關卡沒過', '測試沒過');
        const v = qaVerdict(lastSay);
        b = say(t, v === 'fail' ? 'err' : v === 'pass' ? 'ok' : '');
      }
      if (!b) b = say(ROLE_DEFAULT[persona]);
      scene.actors.push({ key: 'role:' + persona, kind: 'role', role: persona, spot, pose, shirt: ROLE_SHIRT[persona] || '#9aa5b1',
        look: S.lookOf('role:' + persona), think: thinkFor['role:' + persona], bubble: b,
        name: ROLE[persona] || persona, body: ROLE_INFO[persona] || '' });
    }
    return scene;
  }

  // ---- 走路（純畫面狀態；目標永遠來自 sceneOf）
  const motionOK = () => !RM.matches;
  function pathTo(a, tx, ty) {
    const pts = [];
    if (Math.abs(a.y - S.AISLE_Y) > 1 && Math.abs(a.x - tx) > 1) pts.push({ x: a.x, y: S.AISLE_Y });
    if (Math.abs(a.x - tx) > 1) pts.push({ x: tx, y: pts.length ? S.AISLE_Y : a.y });
    pts.push({ x: tx, y: ty });
    return pts;
  }
  function walk(a, pts, maxMs) {
    let len = 0; let px0 = a.x; let py0 = a.y;
    for (const p of pts) { len += Math.abs(p.x - px0) + Math.abs(p.y - py0); px0 = p.x; py0 = p.y; }
    if (len < 1) { a.path = []; return; }
    a.path = pts;
    a.speed = len / Math.min(maxMs, 250 + len * 9);
  }
  function step(a, dt) {
    let left = a.speed * dt;
    while (left > 0 && a.path.length) {
      const p = a.path[0];
      const dx = p.x - a.x; const dy = p.y - a.y; const d = Math.abs(dx) + Math.abs(dy);
      if (d <= left) {
        a.x = p.x; a.y = p.y; left -= d; a.path.shift();
        if (p.swap) { a.old = null; a.carry = false; }
      } else {
        const k = left / d; a.x += dx * k; a.y += dy * k; left = 0;
      }
    }
  }
  // 讓畫面上的人跟上場景：新來的從門口走進來、離開的走出門
  function syncActors(r, scene, now) {
    let rs = O.act[r.id];
    const first = !rs;
    if (!rs) rs = O.act[r.id] = { m: new Map() };
    const motion = motionOK();
    const keep = new Set();
    for (const sp of scene.actors) {
      keep.add(sp.key);
      const spot = SPOT[sp.spot];
      let a = rs.m.get(sp.key);
      if (!a) {
        const enter = !first && motion;
        a = { key: r.id + sp.key, x: enter ? SPOT.door.x : spot.x, y: enter ? SPOT.door.y : spot.y, path: [], speed: 0, tx: spot.x, ty: spot.y };
        rs.m.set(sp.key, a);
        if (enter) walk(a, pathTo(a, spot.x, spot.y), WALK_MAX);
      } else if (a.tx !== spot.x || a.ty !== spot.y || a.leaving) {
        a.leaving = false;
        if (motion) walk(a, pathTo(a, spot.x, spot.y), WALK_MAX);
        else { a.x = spot.x; a.y = spot.y; a.path = []; }
      }
      a.tx = spot.x; a.ty = spot.y;
      a.spec = sp;
    }
    for (const [k, a] of rs.m) {
      if (keep.has(k)) continue;
      if (!motion) { rs.m.delete(k); continue; }
      if (!a.leaving) { a.leaving = true; walk(a, pathTo(a, SPOT.door.x, SPOT.door.y), WALK_MAX); }
      if (!a.path.length) rs.m.delete(k);
    }
    // 換新對話：實作代理拿資料夾走出門，換一個人走進來（一次性）
    const rollEv = lastEv(r, (e) => e.kind === 'rollover');
    const seenRoll = O.rollSeen[r.id];
    if (rollEv && seenRoll !== undefined && seenRoll !== rollEv.ts && recent(rollEv, 30)) {
      const a = rs.m.get('impl');
      if (a && motion) {
        O.roll[r.id] = { start: now };
        a.old = S.lookOf(r.id + a.spec.seat + Math.max(0, (r.rollovers || 1) - 1));
        a.carry = true;
        const out = pathTo(a, SPOT.door.x, SPOT.door.y);
        out[out.length - 1].swap = true;
        const back = pathTo({ x: SPOT.door.x, y: SPOT.door.y }, a.tx, a.ty);
        walk(a, out.concat(back), ROLL_MS);
      }
    }
    if (rollEv) O.rollSeen[r.id] = rollEv.ts; else if (seenRoll === undefined) O.rollSeen[r.id] = '';
    return rs;
  }
  // 畫布要的人：位置＋姿勢＋外觀
  function drawables(rs) {
    const out = [];
    for (const a of rs.m.values()) {
      if (!a.spec) continue;
      const walking = a.path.length > 0;
      const pose = walking ? (a.carry ? 'carry' : 'walk') : a.spec.pose;
      out.push({ key: a.key, x: a.x, y: a.y, pose, shirt: a.spec.shirt, look: a.old || a.spec.look, mark: walking ? '' : a.spec.mark });
    }
    return out;
  }

  // ---- 走廊：沒有出勤的角色在這裡；被叫到房間時沿走廊走過去
  function hallSlot(i) { return 34 + i * 46; }
  function syncHall(idle) {
    const motion = motionOK();
    const lw = O.els.corridorLW || 360;
    const roles = (O.snap && O.snap.roles) || {};
    idle.forEach((role, i) => {
      let a = O.hall.get(role);
      if (!a || a.leaving) {
        a = { key: 'hall:' + role, x: hallSlot(i), y: CH - 4, path: [], speed: 0 };
        O.hall.set(role, a);
      } else if (Math.abs(a.x - hallSlot(i)) > 1) {
        if (motion) walk(a, [{ x: hallSlot(i), y: a.y }], HALL_WALK); else a.x = hallSlot(i);
      }
    });
    for (const [role, a] of O.hall) {
      if (idle.includes(role)) continue;
      if (!motion || !O.hallSeen) { O.hall.delete(role); continue; }
      if (!a.leaving) {
        a.leaving = true;
        // 走向那間房所在的欄（在別頁就走到走廊盡頭）
        const idx = rooms().findIndex((x) => x.id === roles[role]);
        const col = idx >= O.page * PAGE && idx < O.page * PAGE + PAGE ? (idx - O.page * PAGE) % 2 : 2;
        const tx = col === 2 ? lw + 10 : Math.round(lw * (col ? 0.75 : 0.25));
        walk(a, [{ x: tx, y: a.y }], HALL_WALK);
      }
      if (!a.path.length) O.hall.delete(role);
    }
    O.hallSeen = true;
  }

  // ---- 畫面上的人（DOM）：每個像素人一個可聚焦的熱區，泡泡在熱區裡（鍵盤、螢幕閱讀器都讀得到）
  let uid = 0;
  function whoEl(view, key) {
    let w = view.who.get(key);
    if (w) return w;
    const id = 'o-w' + (++uid);
    const name = h('span', { class: 'o-wname' });
    const bub = h('span', { class: 'o-bub hidden', id: id + 'b' });
    const tag = h('span', { class: 'o-wtag hidden' });
    const deleg = h('span', { class: 'o-wdel hidden' }, 'AI 代管');
    const desc = h('span', { class: 'sr', id: id + 'd' });
    const btn = h('button', { class: 'o-who', type: 'button', 'aria-describedby': id + 'd', 'data-actor': key,
      'data-fk': `who:${view.id}:${key}` }, name, tag, deleg, bub);
    // 點像素人＝點房間：同一條延後判斷的路徑（單擊開抽屜、雙擊開特寫、觸控立刻開抽屜）
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (view.mode === 'cell' && view.room) roomClick(e, view.room.id);
    });
    btn.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      clearTimeout(O.clickT);
      if (view.mode === 'cell' && view.room) openCloseup(view.room.id, view.btn);
    });
    btn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && view.mode === 'cell' && view.room) { e.preventDefault(); openCloseup(view.room.id, view.btn); }
    });
    bindCard(btn);
    view.layer.append(btn, desc);
    w = { btn, name, bub, tag, deleg, desc, sig: '', pos: '' };
    view.who.set(key, w);
    return w;
  }
  // 泡泡裡不放秒數（不會每秒跳動）；秒數在說明卡與滑過提示
  const thinkShort = (c) => `${shortModel(c.model)} ${ROLE_VERB[c.role] || '思考'}中…`;
  function bubbleOf(sp) {
    if (sp.think) return { text: thinkShort(sp.think), tone: 'think', think: sp.think };
    return sp.bubble;
  }
  // 依人的位置決定泡泡往哪邊長（l＝往右長、r＝往左長、c＝置中），避免撞到牆邊與彼此
  const ALIGN = { board: 'r', boardSide: 'l', implSide: 'l', test: 'l', coffeeA: 'r', coffeeB: 'r', implSeat: 'c', implStand: 'c',
    revSeat: 'c', revStand: 'c', sofaA: 'r', sofaB: 'l', door: 'r' };
  function paintWho(view, key, a, roomName) {
    const sp = a.spec;
    const w = whoEl(view, key);
    const b = bubbleOf(sp);
    const full = b ? b.text : '';
    const align = ALIGN[sp.spot] || 'c';
    const sig = [sp.name, full, b && b.tone, sp.tag, sp.deleg, align, sp.body, roomName].join('|');
    if (w.sig !== sig) {
      const changed = w.text !== full;
      w.sig = sig; w.text = full;
      w.name.textContent = sp.name;
      w.tag.textContent = sp.tag || '';
      w.tag.classList.toggle('hidden', !sp.tag);
      w.deleg.classList.toggle('hidden', !sp.deleg);
      w.btn.classList.toggle('deleg', !!sp.deleg);
      w.bub.className = 'o-bub' + (b ? '' : ' hidden') + (b && b.tone ? ' t-' + b.tone : '') + ' a-' + align;
      if (b && b.think) {
        put(w.bub, h('span', { class: 'o-thk', 'data-since': b.think.since, 'data-model': b.think.model, 'data-role': b.think.role }, fit(full)));
      } else w.bub.textContent = fit(full);
      const long = b && b.think ? `${thinkText(b.think)}：${b.think.purpose || ''}` : full;
      if (full) w.bub.title = long; else w.bub.removeAttribute('title');
      w.btn.dataset.card = JSON.stringify({ name: sp.name, body: sp.body, now: long, where: roomName });
      w.desc.textContent = `${sp.body}${full ? '。現在：' + full : ''}`;
      if (O.cardFor === w.btn) showCard(w.btn); // 說明卡開著：跟著換內容
      // 只有句子換了才跳一下（同一句不重複閃）
      if (changed && full && motionOK() && w.shown) { w.bub.classList.remove('pop'); void w.bub.offsetWidth; w.bub.classList.add('pop'); }
      w.shown = true;
    }
    const pos = `${Math.round(a.x)},${Math.round(a.y)}`;
    if (w.pos !== pos) {
      w.pos = pos;
      w.btn.style.left = ((a.x - 7) / LW * 100) + '%';
      w.btn.style.top = ((a.y - 21) / LH * 100) + '%';
      if (O.cardFor === w.btn) showCard(w.btn); // 跟著走路的人移動
    }
    return w;
  }
  function paintPeople(view, rs, roomName) {
    const live = new Set();
    for (const [k, a] of rs.m) {
      if (!a.spec || a.leaving) continue;
      live.add(k);
      paintWho(view, k, a, roomName);
    }
    for (const [k, w] of view.who) {
      if (live.has(k)) continue;
      if (O.cardFor === w.btn) hideCard();
      w.btn.remove(); w.desc.remove(); view.who.delete(k);
    }
  }

  // ---- 說明卡（滑過或聚焦像素人時顯示）
  function cardEl() {
    let c = document.getElementById('o-card');
    if (!c) {
      c = h('div', { id: 'o-card', class: 'o-card hidden', role: 'tooltip' });
      // 滑鼠移到卡片上時卡片不消失（WCAG 1.4.13）
      c.addEventListener('pointerenter', () => clearTimeout(O.cardT));
      c.addEventListener('pointerleave', () => { if (!O.cardFor || document.activeElement !== O.cardFor) hideCard(); });
      document.body.append(c);
    }
    return c;
  }
  function showCard(btn) {
    let d;
    try { d = JSON.parse(btn.dataset.card || '{}'); } catch (e) { d = {}; }
    if (!d.name) return;
    const c = cardEl();
    put(c, h('p', { class: 'o-ch' }, h('b', {}, d.name), d.body ? ' — ' + d.body : ''),
      d.now ? h('p', { class: 'o-cnow' }, '現在：' + d.now) : null,
      d.where ? h('p', { class: 'o-cwhere' }, d.where) : null);
    c.classList.remove('hidden');
    O.cardFor = btn;
    const r = btn.getBoundingClientRect();
    const cw = c.offsetWidth; const chh = c.offsetHeight;
    let left = r.left + r.width / 2 - cw / 2;
    left = Math.max(8, Math.min(window.innerWidth - cw - 8, left));
    let top = r.bottom + 8;
    if (top + chh > window.innerHeight - 8) top = Math.max(8, r.top - chh - 8);
    c.style.left = left + 'px'; c.style.top = top + 'px';
  }
  function hideCard() { clearTimeout(O.cardT); const c = document.getElementById('o-card'); if (c) c.classList.add('hidden'); O.cardFor = null; }
  function bindCard(btn) {
    btn.addEventListener('pointerenter', () => { clearTimeout(O.cardT); showCard(btn); });
    btn.addEventListener('pointerleave', () => {
      if (document.activeElement === btn) return;
      clearTimeout(O.cardT);
      O.cardT = setTimeout(() => { const c = document.getElementById('o-card'); if (O.cardFor === btn && !(c && c.matches(':hover'))) hideCard(); }, 300);
    });
    btn.addEventListener('focus', () => showCard(btn));
    btn.addEventListener('blur', () => { if (O.cardFor === btn) hideCard(); });
  }

  // ============================================================ 主畫面
  function render(main) {
    O.root = h('section', { class: 'office', 'aria-label': '辦公室' });
    const pager = h('div', { class: 'o-pager', role: 'group', 'aria-label': '換頁' });
    const inboxBtn = h('button', { class: 'btn', type: 'button', onclick: (e) => openDrawer('inbox', null, e.currentTarget) }, '收件匣');
    const head = h('header', { class: 'o-head' },
      h('div', { class: 'o-title' }, h('h1', {}, '辦公室'), h('p', { class: 'o-sum', 'aria-live': 'polite' }, '讀取中…')),
      pager,
      h('div', { class: 'o-tools' },
        h('button', { class: 'btn', type: 'button', onclick: (e) => openDrawer('seats', null, e.currentTarget) }, '座位'),
        h('button', { class: 'btn', type: 'button', 'aria-label': '用量：模型呼叫與 token', onclick: (e) => openDrawer('usage', null, e.currentTarget) }, '用量'),
        inboxBtn,
        h('button', { class: 'btn primary', type: 'button', onclick: (e) => openWizard(e.currentTarget) }, '派工')));
    const notice = h('div', { class: 'o-notice hidden', role: 'status' });
    const grid = h('div', { class: 'o-grid' });
    const corridor = h('div', { class: 'o-corridor' }, h('canvas', { 'aria-hidden': 'true' }),
      h('div', { class: 'o-hallwho', role: 'group', 'aria-label': '走廊：待命的角色' }), h('div', { class: 'o-idle' }));
    const empty = h('div', { class: 'o-empty hidden' });
    const floor = h('div', { class: 'o-floor' }, empty, grid, corridor);
    const rail = h('aside', { class: 'o-rail', 'aria-label': '等你處理' });
    O.root.append(head, notice, h('div', { class: 'o-body' }, floor, rail));
    O.els = { pager, inboxBtn, sum: head.querySelector('.o-sum'), notice, grid, floor, corridor, rail, empty, cells: [] };
    O.hallView = { id: 'hall', mode: 'hall', layer: corridor.querySelector('.o-hallwho'), who: new Map() };
    rail.addEventListener('focusout', () => setTimeout(() => { if (rail.dataset.stale && !inField(rail)) renderRail(); }, 0));
    clear(main).append(O.root);
    bindSwipe(grid);
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(() => { if (!O.root || !O.root.isConnected) { ro.disconnect(); return; } layout(); });
      ro.observe(floor);
    }
    if (!O.snap) loadState(); else update();
    startTimers();
  }
  async function loadState() {
    if (O.fetching) return;
    O.fetching = true;
    const s = await api('/api/team/state');
    O.fetching = false;
    // SSE 可能已經先送來更新的快照；這時不要用比較舊的回應蓋掉它（串流不會重送沒變的快照）
    if (s && s.rooms) { O.retry = 0; if (!O.snap) onState(s); return; }
    if (O.snap) return; // 串流已經送來快照
    // 讀不到又還沒有串流快照：1、2、5 秒…最多 30 秒後再試
    const steps = [1000, 2000, 5000, 10000, 20000, 30000];
    const wait = steps[Math.min(O.retry || 0, steps.length - 1)];
    O.retry = (O.retry || 0) + 1;
    if (O.els) O.els.sum.textContent = `${(s && s.text) || '讀不到辦公室狀態'}（${Math.round(wait / 1000)} 秒後重試）`;
    clearTimeout(O.retryT);
    O.retryT = setTimeout(() => { if (!O.snap) loadState(); }, wait);
  }
  function visible() { return !!(O.root && O.root.isConnected); }

  function onState(snap) {
    const before = O.snap;
    O.snap = snap;
    const live = new Set();
    for (const r of snap.rooms) {
      live.add(r.id);
      const prev = O.prevStatus[r.id];
      if (prev && prev !== 'done' && r.status === 'done') O.confetti[r.id] = Date.now() + 3000;
      O.prevStatus[r.id] = r.status;
      const csig = [r.status, r.phase, r.persona, r.updated_at].join('|');
      if (!O.changed[r.id] || O.changed[r.id].sig !== csig) O.changed[r.id] = { sig: csig, at: Date.now() };
    }
    // 不在快照裡的房間：釋放畫面狀態與背景快取
    for (const m of [O.act, O.roll, O.rollSeen, O.detail, O.detailSig, O.confetti, O.prevStatus, O.changed]) {
      for (const id of Object.keys(m)) if (!live.has(id)) { delete m[id]; S.forget(id); }
    }
    const n = count();
    if (before && n > O.lastCount && O.lastCount >= 0) {
      const fresh = inbox()[inbox().length - 1];
      if (typeof notifyNeeds === 'function') notifyNeeds(fresh ? fresh.title : '');
      else if (typeof beep === 'function') beep();
    }
    O.lastCount = n;
    for (const id of Object.keys(O.msg)) if (id[0] === 'i' && !inbox().some((i) => i.id === id)) delete O.msg[id];
    O.page = Math.min(O.page, pages() - 1);
    if (typeof refreshTitle === 'function') refreshTitle();
    if (visible()) update();
    refreshDrawer(false);
    refreshCloseup();
  }

  function update() {
    if (!visible() || !O.snap) return;
    const all = rooms();
    const nWait = all.filter(needsYou).length;
    const nRun = all.filter((r) => r.status === 'running').length;
    const nQ = all.filter((r) => r.status === 'queued').length;
    const nErr = all.filter((r) => r.status === 'error').length;
    const nDone = all.filter((r) => r.status === 'done').length;
    const parts = [];
    if (nWait) parts.push(`${nWait} 間等你`);
    if (nErr) parts.push(`${nErr} 間出錯`);
    parts.push(`${nRun} 間運作中（上限 ${O.snap.max_running}）`);
    if (nQ) parts.push(`${nQ} 間排隊`);
    if (nDone) parts.push(`${nDone} 間完成`);
    const sum = all.length ? parts.join('，') : '還沒有房間';
    if (O.els.sum.textContent !== sum) O.els.sum.textContent = sum;
    const n = count();
    O.els.inboxBtn.className = 'btn' + (n ? ' hot' : '');
    if (O.els.inboxBtn.textContent !== `收件匣（${n}）`) O.els.inboxBtn.textContent = `收件匣（${n}）`;
    const ready = O.snap.usage && O.snap.usage.models_ready;
    O.els.notice.classList.toggle('hidden', ready !== false);
    if (ready === false) {
      put(O.els.notice, h('span', {}, '判斷模型沒有設定（litellm.local.json），代理的權限請求會全部交給你。'));
    }
    renderPager();
    renderGrid();
    renderRail();
    layout();
    frame(performance.now(), true);
  }

  function renderPager() {
    const total = pages();
    const p = O.els.pager;
    const all = rooms();
    const hots = [];
    for (let i = 0; i < total; i++) hots.push(all.slice(i * PAGE, i * PAGE + PAGE).some(needsYou) ? 1 : 0);
    const sig = `${total}|${O.page}|${hots.join('')}`;
    if (p.dataset.sig === sig) return;
    p.dataset.sig = sig;
    keepFocus(p, () => fillPager(p, total, hots));
  }
  function fillPager(p, total, hots) {
    const dots = [];
    for (let i = 0; i < total; i++) {
      const hot = !!hots[i];
      dots.push(h('button', {
        class: 'o-dot' + (hot ? ' hot' : '') + (i === O.page ? ' now' : ''), type: 'button', 'data-fk': 'dot:' + i,
        'aria-label': `第 ${i + 1} 頁` + (hot ? '（有等你的房間）' : ''), 'aria-current': i === O.page ? 'page' : null,
        onclick: () => setPage(i),
      }));
    }
    put(p,
      h('button', { class: 'btn ghost', type: 'button', 'data-fk': 'prev', 'aria-label': '上一頁', disabled: O.page === 0, onclick: () => setPage(O.page - 1) }, '◀'),
      h('span', { class: 'o-pnum' }, `第 ${O.page + 1}／${total} 頁`),
      h('span', { class: 'o-dots' }, dots),
      h('button', { class: 'btn ghost', type: 'button', 'data-fk': 'next', 'aria-label': '下一頁', disabled: O.page >= total - 1, onclick: () => setPage(O.page + 1) }, '▶'));
  }
  function setPage(i) {
    const total = pages();
    const next = Math.max(0, Math.min(total - 1, i));
    if (next === O.page) return;
    O.page = next;
    update();
  }

  // 每格：{kind:'room'|'dispatch', room}；空格一律是「＋ 派工」
  function cellsOfPage() {
    const all = rooms();
    const out = [];
    for (let k = 0; k < PAGE; k++) {
      const idx = O.page * PAGE + k;
      if (idx < all.length) out.push({ kind: 'room', room: all[idx] });
      else out.push({ kind: 'dispatch', slot: idx });
    }
    return out;
  }
  const pos = (el, x, y) => { el.style.left = (x / LW * 100) + '%'; el.style.top = (y / LH * 100) + '%'; return el; };

  function renderGrid() {
    const grid = O.els.grid;
    const cells = cellsOfPage();
    const sig = cells.map((c) => c.kind + (c.room ? c.room.id : '')).join('|');
    if (grid.dataset.sig !== sig) {
      // 版面變了才重建，避免搶走焦點
      const a = document.activeElement;
      const focusRoom = a && a.dataset ? a.dataset.room : null;
      const focusFk = a && a.dataset && grid.contains(a) ? a.dataset.fk : null;
      grid.dataset.sig = sig;
      hideCard();
      O.els.cells = cells.map((c) => buildCell(c));
      put(grid, ...O.els.cells.map((c) => c.wrap));
      const b = (focusFk && grid.querySelector(`[data-fk="${CSS.escape(focusFk)}"]`)) || (focusRoom && grid.querySelector(`[data-room="${focusRoom}"]`));
      if (b) b.focus();
    }
    cells.forEach((c, i) => fillCell(O.els.cells[i], c));
    const empty = O.els.empty;
    empty.classList.toggle('hidden', rooms().length > 0);
    if (!rooms().length && !empty.firstChild) {
      empty.append(h('div', {},
        h('b', {}, '辦公室還是空的'),
        h('p', {}, '派一個工作給代理：選專案資料夾、說要做什麼，它會自己寫驗收條件、實作、測試，最後等你按「合併」。'),
        h('div', { class: 'row' },
          h('button', { class: 'btn hot', type: 'button', onclick: startDemo }, '試玩示範'),
          h('button', { class: 'btn', type: 'button', onclick: (e) => openWizard(e.currentTarget) }, '派工')),
        h('p', { class: 'hint' }, '示範用假代理在沙盒 repo 跑完整流程，不會動到你的專案。')));
    }
  }
  function segsEl() {
    return h('span', { class: 'o-segs' }, SEGMENTS.map(() => h('i', {})));
  }
  function paintSegs(el, r) {
    const idx = r.status === 'done' ? 4 : (SEG_OF[r.phase] || 0);
    const now = r.status === 'waiting_user' ? 'heat' : r.status === 'error' ? 'err' : ['paused', 'queued'].includes(r.status) ? 'idle' : 'now';
    [...el.children].forEach((s, i) => { s.className = r.status === 'done' ? 'done' : i < idx ? 'past' : i === idx ? now : ''; });
    el.setAttribute('aria-label', `階段：${r.status === 'done' ? '完成' : SEGMENTS[idx] || ''}（${Math.min(idx + 1, 4)}／4）`);
    el.setAttribute('role', 'img');
  }
  // 一個房間的畫面（總覽的格子或特寫共用）
  function makeView(id, mode) {
    const canvas = h('canvas', { 'aria-hidden': 'true' });
    const layer = h('div', { class: 'o-layer' });
    const stage = h('div', { class: 'o-stage' }, canvas);
    return { id, mode, canvas, stage, layer, who: new Map() };
  }
  function buildCell(c) {
    const view = makeView(c.room ? c.room.id : 'slot' + c.slot, 'cell');
    const cap = h('p', { class: 'o-cap' });
    const wrap = h('div', { class: 'o-cell ' + c.kind });
    Object.assign(view, { wrap, cap, kind: c.kind });
    if (c.kind === 'room') {
      view.plate = h('span', { class: 'o-plate' });
      view.segs = segsEl();
      view.headRow = h('div', { class: 'o-rhead' }, view.plate, view.segs);
      view.impl = pos(h('div', { class: 'o-desk' }), 14, 60);
      view.rev = pos(h('div', { class: 'o-desk' }), 96, 60);
      view.badge = h('div', { class: 'o-badge hidden' });
      view.btn = h('button', { class: 'o-hit', type: 'button', 'data-room': c.room.id, 'aria-keyshortcuts': 'Enter',
        onclick: (e) => roomClick(e, c.room.id) });
      view.btn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); openCloseup(c.room.id, view.btn); } });
      view.stage.addEventListener('dblclick', (e) => { e.preventDefault(); clearTimeout(O.clickT); openCloseup(c.room.id, view.btn); });
      view.stage.append(view.btn, view.impl, view.rev, view.badge, view.layer);
      wrap.append(view.headRow, view.stage, cap);
    } else {
      view.btn = h('button', { class: 'o-hit o-dispatch', type: 'button', onclick: (e) => openWizard(e.currentTarget) },
        h('b', {}, '＋ 派工'), h('span', {}, '空房間：選專案、交代工作'));
      view.stage.append(view.btn);
      view.headRow = h('div', { class: 'o-rhead' }, h('span', { class: 'o-plate dim' }, '空房間'));
      wrap.append(view.headRow, view.stage, cap);
    }
    return view;
  }
  // 單擊開房間抽屜；雙擊（或 Enter）開特寫。用滑鼠時等一下，確定不是雙擊
  function roomClick(e, id) {
    const opener = e.currentTarget;
    if (e.detail > 1) return;
    // 鍵盤（detail 0）與觸控：沒有雙擊，立刻開
    if (e.detail === 0 || e.pointerType === 'touch' || O.lastPointer === 'touch') { clearTimeout(O.clickT); openDrawer('room', id, opener); return; }
    clearTimeout(O.clickT);
    O.clickT = setTimeout(() => { if (!O.close) openDrawer('room', id, opener); }, 230);
  }
  function fillCell(view, c) {
    if (c.kind !== 'room') return;
    const r = c.room;
    view.room = r;
    put(view.plate, h('span', { class: 'o-pname' }, r.name || r.slug), r.demo ? h('span', { class: 'o-demo' }, '示範') : null,
      r.delegate ? delegBadge(r) : null);
    view.plate.title = r.name || r.slug;
    paintSegs(view.segs, r);
    fillDesks(view, r);
    // 角色在別間房：白板下寫「等 PO」
    fillCellRest(view, r);
  }
  function fillDesks(view, r) {
    const p = pct(r);
    for (const [role, el] of [['impl', view.impl], ['review', view.rev]]) {
      const d = deskText(r, role);
      // 一行：座位・模型、派工來源、對話用量（%）；代班時第二行寫原因
      const use = role === 'impl' && deskSeat(r, 'impl') && r.status !== 'queued'
        ? h('span', { class: 'o-use' + (p >= 70 ? ' hi' : p >= 50 ? ' mid' : ''), title: `對話用量 ${Math.round(p)}%${r.context_estimated ? '（用字數估計）' : ''}${p >= 70 ? '，快要換新對話' : ''}` },
          `${Math.round(p)}%`, r.context_estimated ? h('span', { class: 'o-est' }, '估') : null) : null;
      put(el, h('span', { class: 'o-line' }, h('span', { class: 'o-seat', title: d.main }, d.main),
        d.tag ? h('span', { class: 'o-pick', title: d.tip }, d.tag) : null, use),
      d.sub ? h('span', { class: 'o-sub', title: d.sub }, d.sub) : null);
      el.classList.toggle('none', !deskSeat(r, role));
    }
  }
  function fillCellRest(view, r) {
    const roles = (O.snap && O.snap.roles) || {};
    const where = r.persona ? roles[r.persona] : '';
    view.badge.className = 'o-badge' + (where && where !== r.id ? ' wait' : ' hidden');
    view.badge.textContent = where && where !== r.id ? '等 ' + (ROLE[r.persona] || r.persona) + '（在別間）' : '';
    pos(view.badge, 88, 29);
    const s = statusLine(r);
    const items = itemsOf(r.id);
    put(view.cap, items.length && r.status === 'waiting_user' ? null : h('span', { class: 'tag st-' + r.status }, s.head),
      items.length ? h('span', { class: 'tag heat' }, `${items.length} 件等你`) : null,
      h('span', { class: 'o-act', title: s.text }, s.text));
    view.wrap.className = 'o-cell room st-' + r.status + (items.length ? ' needs' : '');
    const di = deskText(r, 'impl'); const dv = deskText(r, 'review');
    const fl = Array.isArray(r.inflight) ? r.inflight : [];
    const desks = `實作 ${di.main}${di.tag ? '（' + di.tip + '）' : ''}${di.sub ? '，' + di.sub : ''}；審查 ${dv.main}${dv.tag ? '（' + dv.tip + '）' : ''}`
      + (fl.length ? '；' + fl.map(thinkText).join('、') : '');
    view.btn.setAttribute('aria-label', `${r.name}：${s.head}，${s.text}。${desks}。開啟房間（Enter 特寫）`);
    view.btn.title = `${r.name}\n${desks}\n${s.text}\n雙擊或按 Enter 看特寫`;
  }

  // 收件匣、座位、右側清單的內容只取決於這些：沒變就不重畫（按鈕按到一半不會被換掉）
  function inboxSig() {
    const s = O.snap || {};
    const rs = (s.rooms || []).map((r) => [r.id, r.name, r.criteria, r.test_cmd]);
    return JSON.stringify([s.inbox, s.fleet, s.roles, s.clis, rs, O.msg, O.open]);
  }
  function renderRail(force) {
    const rail = O.els.rail;
    if (rail.contains(document.activeElement) && /TEXTAREA|INPUT/.test(document.activeElement.tagName)) { rail.dataset.stale = '1'; return; }
    const sig = inboxSig() + '|' + rooms().length;
    if (!force && !rail.dataset.stale && O.sig.rail === sig) return;
    O.sig.rail = sig;
    rail.dataset.stale = '';
    keepFocus(rail, () => fillRail(rail));
  }
  function fillRail(rail) {
    const items = inbox();
    const kids = [h('h2', {}, items.length ? `等你（${items.length}）` : '等你')];
    if (!items.length) {
      kids.push(h('p', { class: 'o-quiet' }, h('span', { class: 'light' }), rooms().length ? '沒有等你的事，代理在推進中。' : '有事要你決定時會出現在這裡。'));
    } else {
      const list = h('div', { class: 'o-items' });
      items.slice(0, 6).forEach((it) => list.append(inboxItem(it, { compact: true })));
      kids.push(list);
      if (items.length > 6) kids.push(h('button', { class: 'btn ghost', type: 'button', 'data-fk': 'rail:all', onclick: (e) => openDrawer('inbox', null, e.currentTarget) }, `看全部 ${items.length} 件`));
    }
    // 座位一覽
    const fleet = (O.snap && O.snap.fleet) || {};
    kids.push(h('h2', { class: 'o-seats-h' }, '座位'),
      h('ul', { class: 'o-seatlist' }, SEATS.map((s) => {
        const f = fleet[s] || {};
        const st = seatState(f, s);
        return h('li', { title: st.long }, h('span', { class: 'o-sw', style: null, 'data-seat': s }, SEAT[s].g), h('span', { class: 'grow' }, f.label || SEAT[s].name),
          h('span', { class: 'o-sst ' + st.cls }, st.short));
      })),
      h('button', { class: 'btn ghost', type: 'button', 'data-fk': 'rail:seats', onclick: (e) => openDrawer('seats', null, e.currentTarget) }, '管理座位'));
    put(rail, ...kids);
    for (const sw of rail.querySelectorAll('.o-sw')) paintSwatch(sw, sw.dataset.seat);
  }
  function paintSwatch(el, seat) { el.style.background = SEAT[seat].c; el.style.borderColor = SEAT[seat].d; }
  function seatState(f, seat) {
    const st = seatState0(f, seat);
    const q = f.quota || {};
    if (q.state === 'exhausted' && q.reason === 'login') {
      return { cls: st.cls, short: '未登入', long: st.long.replace(/^已滿/, '未登入，已暫時排除') };
    }
    return st;
  }
  function seatState0(f, seat) {
    const q = f.quota || {};
    if (q.state === 'exhausted') {
      const untilMs = Date.parse(q.until);
      const raw = (untilMs - Date.now()) / 1000;
      // 時間不合理（已過、超過 7 天、或年份不對）就不倒數
      if (!Number.isFinite(untilMs) || new Date(untilMs).getFullYear() < 2000 || raw <= 0 || raw > 7 * 86400) {
        return { cls: 'full', short: '已滿', long: '已滿，等待下次試探' };
      }
      const left = raw;
      const hh = Math.floor(left / 3600); const mm = Math.floor((left % 3600) / 60); const ss = Math.floor(left % 60);
      const wait = hh >= 48 ? `${Math.floor(hh / 24)} 天 ${hh % 24} 小時` : hh ? `${hh} 小時 ${mm} 分` : `${mm}:${String(ss).padStart(2, '0')}`;
      const today = new Date(Date.parse(q.until)).toDateString() === new Date().toDateString();
      const at = today ? String(q.until).slice(11, 16) : String(q.until).slice(5, 16).replace('T', ' ');
      return { cls: 'full', short: '已滿', long: `已滿，${wait} 後自動試探（${at}）` };
    }
    if (f.available) return { cls: 'ok', short: f.rooms && f.rooms.length ? `使用中 ${f.rooms.length}` : '可用', long: '可用' };
    // 只有 CLI 座位且偵測不到 CLI 時才說「未安裝」；其餘（例如 GLM 沒有模型設定）是「不可用」
    const clis = (O.snap && O.snap.clis) || {};
    if (['kiro', 'agy', 'claude'].includes(seat) && !(seat in clis)) return { cls: 'off', short: '未安裝', long: '未安裝：這台電腦找不到這個 CLI' };
    return { cls: 'off', short: '不可用', long: seat && seat.startsWith('glm') ? '不可用：litellm.local.json 沒有 glm 的設定' : '不可用：未登入或啟動失敗' };
  }

  // ---- 版面
  function layout() {
    if (!visible()) return;
    const dpr = window.devicePixelRatio || 1;
    const floor = O.els.floor;
    const gridW = floor.clientWidth;
    if (!gridW) return;
    const gap = 14;
    const cellW = (gridW - gap) / 2;
    const top = O.els.grid.getBoundingClientRect().top + window.scrollY;
    // 每格上方標題列＋下方狀態列約 54px；走廊 CH 再加名字
    const availH = window.innerHeight - Math.max(0, top - window.scrollY) - 2 * 64 - CH * 2 - 46;
    const sW = Math.floor(cellW * dpr / LW);
    const sH = Math.floor(availH / 2 * dpr / LH);
    const s = Math.max(Math.round(2 * dpr), Math.min(sW, sH));
    O.scale = s;
    const cssW = LW * s / dpr; const cssH = LH * s / dpr;
    O.els.grid.style.gridTemplateColumns = `repeat(2, ${cssW}px)`;
    O.els.grid.style.setProperty('--o-k', String(s / dpr / 2));
    for (const c of O.els.cells) {
      if (c.canvas.width !== LW * s) { c.canvas.width = LW * s; c.canvas.height = LH * s; }
      c.stage.style.width = cssW + 'px'; c.stage.style.height = cssH + 'px';
      c.wrap.style.width = cssW + 'px';
    }
    const cw = cssW * 2 + gap;
    const cv = O.els.corridor.querySelector('canvas');
    const lw = Math.floor(cw * dpr / s);
    if (cv.width !== lw * s) { cv.width = lw * s; cv.height = CH * s; }
    cv.style.width = (lw * s / dpr) + 'px'; cv.style.height = (CH * s / dpr) + 'px';
    O.els.corridor.style.width = (lw * s / dpr) + 'px';
    O.els.corridorLW = lw;
    frame(performance.now(), true);
  }
  function ctxOf(canvas, scale) {
    const ctx = canvas.getContext('2d');
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.imageSmoothingEnabled = false;
    return ctx;
  }

  // ---- 繪圖迴圈：只有在有東西在動時才跑；隱藏分頁停；降低動態時只在狀態改變時畫一次
  function paintRoomView(view, r, now) {
    const scene = sceneOf(r, now);
    const rs = syncActors(r, scene, now);
    const ctx = ctxOf(view.canvas, view.scale || O.scale);
    S.drawRoom(ctx, r, scene, drawables(rs), now, motionOK());
    paintPeople(view, rs, r.name || r.slug);
    return scene;
  }
  function advanceAll(dt) {
    let moving = false;
    for (const rs of Object.values(O.act)) {
      for (const [k, a] of rs.m) {
        if (!a.path.length) continue;
        step(a, dt);
        moving = true;
        if (a.leaving && !a.path.length) rs.m.delete(k);
      }
    }
    for (const [role, a] of O.hall) {
      if (!a.path.length) continue;
      step(a, dt); moving = true;
      if (a.leaving && !a.path.length) O.hall.delete(role);
    }
    return moving;
  }
  function frame(now, once) {
    if (!once) O.raf = 0;
    if (!visible() || !O.snap) return;
    const dt = O.lastT ? Math.min(100, now - O.lastT) : 16;
    O.lastT = now;
    const wall = Date.now();
    if (!once) { advanceAll(dt); O.frames = (O.frames || 0) + 1; }
    // ambient：0＝不用動、1＝只有等你的「!」在晃（約 4 fps，10 秒沒變化就停）、2＝打字／螢幕／彩帶（約 8 fps）
    let ambient = 0;
    const need = (r, sc) => {
      if (r.status === 'running' || sc.confetti) return 2;
      if (sc.bang && wall - ((O.changed[r.id] || {}).at || 0) < 10000) return 1;
      return 0;
    };
    if (O.close) {
      // 特寫開著：總覽暫停，只畫特寫
      const r = roomById(O.close.id);
      if (r) ambient = need(r, paintRoomView(O.close.view, r, wall));
    } else {
      for (const c of O.els.cells) {
        const ctx = ctxOf(c.canvas, O.scale);
        if (c.kind === 'room' && c.room) ambient = Math.max(ambient, need(c.room, paintRoomView(c, c.room, wall)));
        else S.drawEmpty(ctx, c.id, wall);
      }
      paintCorridor(wall);
    }
    schedule(isMoving(), ambient);
  }
  function isMoving() {
    return Object.values(O.act).some((rs) => [...rs.m.values()].some((a) => a.path.length)) || [...O.hall.values()].some((a) => a.path.length);
  }
  function schedule(moving, ambient) {
    const want = motionOK() && !document.hidden && visible() && (moving || ambient > 0);
    if (O.root) O.root.dataset.loop = want ? 'on' : 'off';
    if (!want || O.raf || O.rafT) return;
    if (moving) O.raf = requestAnimationFrame((t) => frame(t));
    else O.rafT = setTimeout(() => { O.rafT = 0; O.raf = requestAnimationFrame((t) => frame(t)); }, ambient > 1 ? 110 : 250);
  }
  function paintCorridor(now) {
    const roles = (O.snap && O.snap.roles) || {};
    const idle = Object.keys(ROLE).filter((k) => !roles[k]);
    syncHall(idle);
    const cv = O.els.corridor.querySelector('canvas');
    if (!cv.width) return;
    const ctx = ctxOf(cv, O.scale);
    const lw = O.els.corridorLW || 360;
    ctx.clearRect(0, 0, lw, CH);
    const people = [];
    for (const [role, a] of O.hall) {
      people.push({ key: a.key, x: a.x, y: a.y, pose: a.path.length ? 'walk' : 'stand', shirt: ROLE_SHIRT[role] || '#9aa5b1', look: S.lookOf('role:' + role) });
    }
    S.drawCorridor(ctx, lw, people, now, motionOK());
    // 熱區與名字
    const view = O.hallView;
    const k = O.scale / (window.devicePixelRatio || 1);
    const live = new Set();
    for (const [role, a] of O.hall) {
      if (a.leaving) continue;
      live.add(role);
      const w = whoEl(view, role);
      const sig = role + '|' + k;
      if (w.sig !== sig) {
        w.sig = sig;
        w.name.textContent = ROLE[role];
        w.btn.classList.add('named');
        w.btn.dataset.card = JSON.stringify({ name: ROLE[role], body: ROLE_INFO[role], where: '在走廊待命：現在沒有房間需要它' });
        w.desc.textContent = ROLE_INFO[role] + '。在走廊待命';
      }
      const p = `${Math.round(a.x)}|${k}`;
      if (w.pos !== p) {
        w.pos = p;
        w.btn.style.left = ((a.x - 9) * k) + 'px'; w.btn.style.top = '0px';
        w.btn.style.width = (18 * k) + 'px'; w.btn.style.height = (CH * k) + 'px';
        if (O.cardFor === w.btn) showCard(w.btn);
      }
    }
    for (const [key, w] of view.who) {
      if (live.has(key)) continue;
      if (O.cardFor === w.btn) hideCard();
      w.btn.remove(); w.desc.remove(); view.who.delete(key);
    }
    const names = O.els.corridor.querySelector('.o-idle');
    const sig = live.size ? 'some' : 'none';
    if (names.dataset.sig !== sig) {
      names.dataset.sig = sig;
      put(names, h('span', { class: 'o-hall' }, '走廊'), live.size ? null : h('span', { class: 'o-allout' }, '所有角色都在房間裡'));
    }
  }
  // 每秒：泡泡裡「審查中…（N 秒）」的秒數、時鐘、日夜
  function tickThink() {
    for (const el of document.querySelectorAll('.o-thk[data-since]')) {
      if (el.closest('.o-bub')) continue; // 泡泡不放秒數
      const t = thinkText({ model: el.dataset.model, role: el.dataset.role, since: el.dataset.since });
      if (el.textContent !== t) el.textContent = t;
    }
    // 說明卡開著時更新秒數
    if (O.cardFor && O.cardFor.isConnected && /中…（/.test(O.cardFor.dataset.card || '')) {
      const bub = O.cardFor.querySelector('.o-thk');
      if (bub) {
        const d = JSON.parse(O.cardFor.dataset.card);
        d.now = d.now.replace(/^[^：]*/, thinkText({ model: bub.dataset.model, role: bub.dataset.role, since: bub.dataset.since }));
        O.cardFor.dataset.card = JSON.stringify(d);
        showCard(O.cardFor);
      }
    }
  }
  function startTimers() {
    if (O.timer) return;
    O.timer = setInterval(() => {
      if (!visible()) { clearInterval(O.timer); O.timer = null; return; }
      tickThink();
      // 時鐘每分鐘、降低動態時仍要更新
      const m = new Date().getMinutes();
      if (m !== O.minute) { O.minute = m; if (!document.hidden) frame(performance.now(), true); }
    }, 1000);
  }
  document.addEventListener('pointerdown', (e) => { O.lastPointer = e.pointerType; }, true);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { if (O.raf) cancelAnimationFrame(O.raf); O.raf = 0; clearTimeout(O.rafT); O.rafT = 0; O.lastT = 0; if (O.root) O.root.dataset.loop = 'off'; }
    else if (visible()) frame(performance.now(), true);
  });
  if (RM.addEventListener) RM.addEventListener('change', () => { if (visible()) frame(performance.now(), true); });

  // ============================================================ 特寫：全畫面一間房＋右側即時時間軸（Esc 返回）
  function openCloseup(id, opener) {
    const r = roomById(id);
    if (!r) return;
    clearTimeout(O.clickT);
    closeCloseup(true);
    hideCard();
    const view = makeView('close:' + id, 'close');
    const head = h('header', { class: 'o-xhead' });
    const tl = h('div', { class: 'o-xtl' });
    tl.addEventListener('toggle', onDetailsToggle, true);
    const wb = h('ol', { class: 'o-wb', 'aria-label': '白板：驗收條件' });
    view.impl = pos(h('div', { class: 'o-desk' }), 14, 60);
    view.rev = pos(h('div', { class: 'o-desk' }), 96, 60);
    view.stage.append(wb, view.impl, view.rev, view.layer);
    const box = h('div', { class: 'o-xstagebox' }, view.stage);
    const aside = h('aside', { class: 'o-xside', 'aria-label': '即時時間軸' }, h('h3', {}, '即時時間軸'), tl);
    const root = h('div', { class: 'o-xview', role: 'dialog', 'aria-modal': 'true', 'aria-label': `特寫：${r.name}`, tabindex: '-1' },
      head, h('div', { class: 'o-xbody' }, box, aside));
    document.body.append(root);
    // 特寫開著時，後面的主畫面不能被 Tab 到
    const shell = document.querySelector('.shell');
    if (shell) shell.inert = true;
    O.close = { id, view, root, head, tl, wb, box, opener: opener || document.activeElement, sig: '' };
    loadDetail(id, true);
    closeLayout();
    refreshCloseup();
    root.focus();
  }
  function closeCloseup(silent) {
    if (!O.close) return;
    const c = O.close;
    O.close = null;
    hideCard();
    c.root.remove();
    const shell = document.querySelector('.shell');
    if (shell) shell.inert = false;
    if (!silent && c.opener && c.opener.isConnected) c.opener.focus();
    frame(performance.now(), true);
  }
  function closeLayout() {
    const c = O.close;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    const w = c.box.clientWidth - 24; const hh = c.box.clientHeight - 24;
    const s = Math.max(2, Math.floor(Math.min(w * dpr / LW, hh * dpr / LH)));
    c.view.scale = s;
    if (c.view.canvas.width !== LW * s) { c.view.canvas.width = LW * s; c.view.canvas.height = LH * s; }
    c.view.stage.style.width = (LW * s / dpr) + 'px'; c.view.stage.style.height = (LH * s / dpr) + 'px';
    c.view.stage.style.setProperty('--o-k', String(s / dpr / 2));
  }
  window.addEventListener('resize', () => { if (O.close) { closeLayout(); frame(performance.now(), true); } });
  function refreshCloseup() {
    const c = O.close;
    if (!c) return;
    const r = roomById(c.id);
    if (!r || r.status === 'archived') { closeCloseup(); return; }
    const ev = r.events && r.events.length ? r.events[r.events.length - 1] : {};
    const dsig = r.updated_at + '|' + ev.ts + '|' + ev.text;
    if (O.detailSig[r.id] !== dsig) loadDetail(r.id, false);
    const s = statusLine(r);
    const items = itemsOf(r.id);
    const hsig = JSON.stringify([r.name, r.status, r.phase, s.text, items.length, r.demo, r.delegate]);
    if (c.headSig !== hsig) {
      c.headSig = hsig;
      const segs = segsEl(); paintSegs(segs, r);
      keepFocus(c.head, () => put(c.head,
        h('div', { class: 'grow' }, h('h2', {}, r.name, r.demo ? h('span', { class: 'o-demo' }, '示範') : null, r.delegate ? delegBadge(r) : null),
          h('p', { class: 'o-xstat' }, h('span', { class: 'tag st-' + r.status }, STATUS[r.status] || r.status),
            items.length ? h('span', { class: 'tag heat' }, `${items.length} 件等你`) : null, ' ', s.text)),
        segs,
        h('button', { class: 'btn', type: 'button', 'data-fk': 'x:detail', onclick: (e) => openDrawer('room', r.id, e.currentTarget) }, '開啟詳情'),
        h('button', { class: 'btn ghost', type: 'button', 'data-fk': 'x:close', onclick: () => closeCloseup() }, '返回總覽', h('kbd', {}, 'Esc'))));
    }
    fillDesks(c.view, r);
    // 白板上的字（特寫才看得清楚）
    const crit = r.criteria || [];
    const wsig = JSON.stringify([crit, r.phase, r.status]);
    if (c.wbSig !== wsig) {
      c.wbSig = wsig;
      put(c.wb, crit.slice(0, 5).map((x) => { const [cls, label] = criteriaState(r, x); return h('li', { class: 'o-c ' + cls, title: `${x.id} ${x.text}：${label}` }, h('b', {}, x.id), ' ', x.text); }));
      c.wb.classList.toggle('hidden', !crit.length);
    }
    renderCloseTimeline();
    frame(performance.now(), true);
  }
  function renderCloseTimeline() {
    const c = O.close;
    if (!c) return;
    const r = roomById(c.id);
    const det = O.detail[c.id];
    const evs = (det ? det.events : (r && r.events) || []).slice().reverse();
    const sig = JSON.stringify([O.evdVer || 0, evs.slice(0, 80).map((e) => [e.ts, e.kind, e.text, e.id || '', e.status || ''])]);
    if (c.sig === sig || selecting(c.tl)) return;
    c.sig = sig;
    keepFocus(c.tl, () => put(c.tl, evs.length ? timelineList(evs, 60, c.id) : h('p', { class: 'dim' }, '還沒有紀錄。')));
  }

  // ---- 換頁：方向鍵與滑動
  function bindSwipe(el) {
    el.addEventListener('pointerdown', (e) => { O.swipe = { x: e.clientX, y: e.clientY, moved: false }; });
    el.addEventListener('pointerup', (e) => {
      if (!O.swipe) return;
      const dx = e.clientX - O.swipe.x; const dy = e.clientY - O.swipe.y;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) { O.swipe.moved = true; setPage(O.page + (dx < 0 ? 1 : -1)); }
      setTimeout(() => { O.swipe = null; }, 0);
    });
    el.addEventListener('click', (e) => { if (O.swipe && O.swipe.moved) { e.stopPropagation(); e.preventDefault(); } }, true);
  }
  document.addEventListener('keydown', (e) => {
    // Esc 先只關說明卡，再按一次才關抽屜或特寫
    if (e.key === 'Escape' && O.cardFor) { e.preventDefault(); hideCard(); return; }
    if (e.key === 'Escape' && O.drawer) { e.preventDefault(); closeDrawer(); return; }
    if (e.key === 'Escape' && O.close) { e.preventDefault(); closeCloseup(); return; }
    if (!visible() || O.drawer || O.close || e.altKey || e.ctrlKey || e.metaKey) return;
    const t = e.target;
    if (t && (/TEXTAREA|INPUT|SELECT/.test(t.tagName) || t.isContentEditable)) return;
    if (document.querySelector('.palette-wrap')) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); setPage(O.page + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); setPage(O.page - 1); }
  });

  async function startDemo(e) {
    const btn = e && e.currentTarget;
    if (btn) btn.disabled = true;
    const r = await act('/api/team/demo', {});
    if (btn) btn.disabled = false;
    if (r.ok && r.room) jumpTo(r.room.id);
  }
  function jumpTo(id) {
    const idx = rooms().findIndex((r) => r.id === id);
    if (idx >= 0) { O.page = Math.floor(idx / PAGE); update(); }
  }

  // ============================================================ 收件匣項目（右側、收件匣、房間抽屜共用）
  function parseMerge(detail) {
    const out = { note: '', branch: '', commits: [], stat: '' };
    const d = String(detail || '');
    const ci = d.indexOf('commit：'); const si = d.indexOf('改動：');
    const head = ci >= 0 ? d.slice(0, ci) : d;
    const bm = head.match(/分支：(.*)/);
    out.branch = bm ? bm[1].trim() : '';
    out.note = head.replace(/分支：.*/, '').trim();
    if (ci >= 0) out.commits = d.slice(ci + 7, si >= 0 ? si : undefined).split('\n').map((x) => x.trim()).filter(Boolean);
    if (si >= 0) out.stat = d.slice(si + 3).replace(/^\n+/, '').replace(/\s+$/, '');
    return out;
  }
  function permCmd(detail) {
    const m = String(detail || '').match(/指令：([\s\S]*?)(?:\n種類：|$)/);
    return m ? m[1].trim() : '';
  }
  function optClass(it, o, i) {
    if (/reject|deny/.test(o.id)) return 'btn danger';
    if (o.id === 'edit') return 'btn';
    if (/later|pause|switch/.test(o.id)) return 'btn ghost';
    return i === 0 ? 'btn hot' : 'btn';
  }
  async function answer(it, optId, text, btns, out, label) {
    if (O.busy[it.id]) return;
    O.busy[it.id] = true;
    btns.forEach((b) => { b.disabled = true; });
    delete O.msg[it.id]; // 再按一次：先清掉上次的訊息
    out.className = 'o-out';
    out.textContent = it.kind === 'merge' && optId === 'merge' ? '合併中…' : '';
    const r = await api('/api/team/answer', { item_id: it.id, answer: optId, text: text || '' });
    O.busy[it.id] = false;
    if (r.ok && it.kind === 'login') {
      // 登入項目在登入完成前不會消失：按鈕恢復可按，結果留在項目上
      O.msg[it.id] = { ok: true, text: r.text || '' };
      btns.forEach((b) => { b.disabled = false; });
      out.textContent = O.msg[it.id].text;
      out.className = 'o-out';
      toast(r.text || '已送出', true);
      rerenderItems();
      return;
    }
    if (r.ok) {
      delete O.drafts['edit:' + it.id]; delete O.open['edit:' + it.id];
      toast(it.kind === 'merge' && optId === 'merge' ? (r.text || '已合併') : `已回覆「${label || optId}」：${short(it.title, 30)}`, true);
      out.textContent = '';
    } else {
      // 錯誤訊息留著，直到項目解決或你再按一次（快照更新不會把它洗掉）
      O.msg[it.id] = { ok: false, text: r.text || '沒有成功' };
      btns.forEach((b) => { b.disabled = false; });
      out.textContent = O.msg[it.id].text;
      out.className = 'o-err';
    }
  }
  function msgEl(key) {
    const m = O.msg[key];
    return h('p', { class: m && m.ok === false ? 'o-err' : 'o-out', role: 'status', 'data-msg': key }, m ? m.text : '');
  }
  function inboxItem(it, opts = {}) {
    const r = roomById(it.room_id);
    const out = msgEl(it.id);
    const btns = [];
    const body = [];
    if (it.kind === 'permission') {
      const cmd = permCmd(it.detail);
      if (cmd) body.push(h('pre', { class: 'cmd' }, cmd));
      const rest = String(it.detail || '').split('\n').filter((l) => !l.startsWith('指令：')).join('\n').trim();
      if (rest && !opts.compact) body.push(h('p', { class: 'o-detail' }, rest));
    } else if (it.kind === 'approve_spec') {
      const crit = r && r.criteria ? r.criteria : [];
      if (crit.length) {
        body.push(h('ul', { class: 'o-crit' }, crit.slice(0, opts.compact ? 3 : 50).map((c) => h('li', {},
          h('b', {}, c.id), ' ', c.text, opts.compact ? null : h('code', { class: 'o-chk' }, String(c.check || 'manual'))))));
        if (opts.compact && crit.length > 3) body.push(h('p', { class: 'hint' }, `還有 ${crit.length - 3} 條，開房間看全部`));
        if (!opts.compact && r.test_cmd) body.push(h('p', { class: 'o-detail' }, '測試指令：', h('code', {}, r.test_cmd)));
      } else body.push(h('pre', { class: 'md' }, it.detail));
    } else if (it.kind === 'merge') {
      const m = parseMerge(it.detail);
      if (m.note) body.push(h('p', { class: 'o-warn' }, m.note));
      if (m.commits.length) {
        body.push(h('ul', { class: 'o-commits' }, m.commits.slice(0, opts.compact ? 3 : 40).map((c) => {
          const sp = c.indexOf(' ');
          return h('li', {}, h('code', {}, sp > 0 ? c.slice(0, sp) : c), sp > 0 ? ' ' + c.slice(sp + 1) : '');
        })));
        if (opts.compact && m.commits.length > 3) body.push(h('p', { class: 'hint' }, `共 ${m.commits.length} 個 commit`));
      }
      if (m.stat) body.push(h('pre', { class: 'cmd o-stat' }, opts.compact ? m.stat.split('\n').slice(-1)[0] : m.stat));
    } else if (it.kind === 'login') {
      const others = (it.rooms || []).filter((id) => id !== it.room_id).map((id) => (roomById(id) || {}).name || id);
      if (others.length) body.push(h('p', { class: 'o-detail' }, `也在等這次登入：${others.join('、')}`));
      const m = O.msg[it.id];
      if (it.state === 'opened' && !(m && m.text === LOGIN_OPENED)) body.push(h('p', { class: 'o-login-state', role: 'status' }, LOGIN_OPENED));
      if (it.note) body.push(h('p', { class: 'o-warn' }, it.note));
      body.push(h('pre', { class: 'md o-pre' }, opts.compact ? short(it.detail, 200) : it.detail));
    } else if (it.detail) {
      body.push(h('pre', { class: 'md o-pre' }, opts.compact ? short(it.detail, 260) : it.detail));
    }
    // 寫修改意見（edit 選項）
    const editKey = 'edit:' + it.id;
    let editBox = null;
    const hasEdit = it.options.some((o) => o.id === 'edit');
    if (hasEdit && O.open[editKey]) {
      const ta = h('textarea', { rows: 3, 'aria-label': '修改意見', placeholder: '例如：C1 要檢查負數；不要動 README' }, O.drafts[editKey] || '');
      ta.addEventListener('input', () => { O.drafts[editKey] = ta.value; });
      const send = h('button', { class: 'btn primary', type: 'button', onclick: () => {
        if (!ta.value.trim()) { O.msg[it.id] = { ok: false, text: '請先寫下要怎麼改' }; out.textContent = O.msg[it.id].text; out.className = 'o-err'; ta.focus(); return; }
        answer(it, 'edit', ta.value, btns, out, '修改意見');
      } }, '送出修改意見');
      btns.push(send);
      editBox = h('div', { class: 'o-edit' }, ta, h('div', { class: 'row' }, send,
        h('button', { class: 'btn ghost', type: 'button', onclick: () => { delete O.open[editKey]; rerenderItems(); } }, '取消')));
    }
    const opened = it.kind === 'login' && (it.state === 'opened' || (O.msg[it.id] && O.msg[it.id].text === LOGIN_OPENED));
    const shown = opened ? it.options.filter((o) => o.id !== 'open').concat(it.options.filter((o) => o.id === 'open')) : it.options;
    const optBtns = shown.map((o, i) => {
      let label = o.id === 'edit' && it.kind === 'approve_spec' ? '寫修改意見' : o.label;
      if (opened && o.id === 'open') label = '再開一次登入視窗';
      const cls = opened && o.id === 'open' ? 'btn ghost' : optClass(it, o, i);
      const b = h('button', { class: cls, type: 'button', 'data-fk': `${it.id}:${o.id}`, 'aria-expanded': o.id === 'edit' ? String(!!O.open[editKey]) : null,
        onclick: () => {
          if (o.id === 'edit') {
            O.open[editKey] = !O.open[editKey];
            const inDrawer = O.drawer && O.drawer.body && O.drawer.body.contains(b);
            rerenderItems();
            if (O.open[editKey]) {
              const scope = inDrawer ? O.drawer.body : O.els.rail;
              const t = scope.querySelector(`[data-item="${it.id}"] textarea`);
              if (t) t.focus();
            }
            return;
          }
          answer(it, o.id, '', btns, out, label);
        } }, label);
      btns.push(b);
      return b;
    });
    const name = r ? r.name : it.room_id;
    const title = opts.inRoom ? h('b', { class: 'o-ititle' }, it.title)
      : h('button', { class: 'o-link', type: 'button', 'data-fk': it.id + ':link', onclick: () => { closeDrawer(); jumpTo(it.room_id); openDrawer('room', it.room_id); } }, it.title);
    return h('article', { class: 'o-item k-' + it.kind, 'data-item': it.id },
      h('div', { class: 'o-imeta' }, h('span', { class: 'tag heat' }, KIND[it.kind] || it.kind),
        opts.inRoom ? null : h('span', { class: 'o-room' }, name), h('time', {}, hms(it.created_at))),
      title, body, h('div', { class: 'row' }, optBtns), editBox, out);
  }
  function rerenderItems() {
    if (visible()) { O.els.rail.dataset.stale = ''; renderRail(true); }
    if (O.drawer && (O.drawer.type === 'inbox' || O.drawer.type === 'room')) renderDrawerBody(true);
  }

  // ============================================================ 抽屜
  function drawerRoot() {
    let root = document.getElementById('office-drawer');
    if (!root) { root = h('div', { id: 'office-drawer' }); document.body.append(root); }
    return root;
  }
  function openDrawer(type, id, opener) {
    hideCard(); // 抽屜蓋住像素人：說明卡一起收掉
    O.opener = opener || document.activeElement;
    O.drawer = { type, id };
    const label = { room: '房間', inbox: '收件匣', seats: '座位', wizard: '派工', usage: '用量' }[type];
    const head = h('header', { class: 'o-dhead' });
    const body = h('div', { class: 'body', tabindex: '-1' });
    const catchUp = () => setTimeout(() => {
      if (O.drawer && O.drawer.stale && !inField(body) && !selecting(body)) renderDrawerBody(true);
    }, 0);
    body.addEventListener('focusout', catchUp);
    body.addEventListener('mouseup', () => setTimeout(catchUp, 400));
    // 打開的「全文」保持打開
    body.addEventListener('toggle', onDetailsToggle, true);
    put(drawerRoot(), 
      h('div', { class: 'scrim', onclick: closeDrawer }),
      h('aside', { class: 'drawer o-drawer o-' + type, role: 'dialog', 'aria-modal': 'true', 'aria-label': label, tabindex: '-1' }, head, body));
    O.drawer.head = head; O.drawer.body = body;
    if (type === 'room') { loadDetail(id, true); loadModels(); }
    if (type === 'usage') { O.usageScope = O.usageScope || 'today'; loadUsage(); }
    if (type === 'seats') {
      loadCatalog();
      O.drawer.tick = setInterval(() => {
        if (!O.drawer || O.drawer.type !== 'seats') return;
        const fleet = (O.snap && O.snap.fleet) || {};
        for (const el of O.drawer.body.querySelectorAll('.o-sst[data-seat]')) {
          const t = seatState(fleet[el.dataset.seat] || {}, el.dataset.seat).long;
          if (el.textContent !== t) el.textContent = t;
        }
      }, 1000);
    }
    renderDrawerHead();
    renderDrawerBody(true);
    // 焦點放在對話框本身（不是按鈕）：從指令面板按 Enter 打開時，同一個 Enter 不會誤按到「關閉」
    drawerRoot().querySelector('aside').focus();
  }
  function closeDrawer() {
    if (!O.drawer) return;
    if (O.drawer.tick) clearInterval(O.drawer.tick);
    O.drawer = null;
    drawerRoot().replaceChildren();
    if (O.opener && O.opener.isConnected) O.opener.focus();
    O.opener = null;
  }
  const inField = (el) => el.contains(document.activeElement) && /TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName);
  function refreshDrawer() {
    if (!O.drawer || O.drawer.type === 'wizard') return;
    if (O.drawer.type === 'usage') loadUsage();
    if (O.drawer.type === 'room') {
      const r = roomById(O.drawer.id);
      if (!r || r.status === 'archived') { roomGone(); return; }
      const ev = r.events && r.events.length ? r.events[r.events.length - 1] : {};
      const sig = r.updated_at + '|' + ev.ts + '|' + ev.text;
      if (O.detailSig[r.id] !== sig) loadDetail(r.id, false);
    }
    renderDrawerHead();
    renderDrawerBody(false);
  }
  // 抽屜內容的簽章：只有它變了才重畫
  function drawerSig(d) {
    if (d.type === 'inbox') return inboxSig();
    if (d.type === 'seats') {
      const s = O.snap || {};
      return JSON.stringify([s.fleet, s.certs, s.clis, (s.rooms || []).map((r) => [r.id, r.name]), O.catalog, O.seatMsg]);
    }
    if (d.type === 'usage') return JSON.stringify([O.usage, O.usageScope, O.usageErr]);
    if (d.type === 'room') {
      const r = roomById(d.id);
      const det = O.detail[d.id];
      return JSON.stringify([r, det && det.events, itemsOf(d.id), O.msg, O.open, O.snap && O.snap.roles, O.models, O.modelsErr, O.drafts, O.evdVer || 0]);
    }
    return '';
  }
  // 使用者在抽屜裡選取文字（要複製）時先不重畫
  function selecting(el) {
    const sel = window.getSelection ? window.getSelection() : null;
    return !!(sel && !sel.isCollapsed && sel.rangeCount && el.contains(sel.getRangeAt(0).commonAncestorContainer));
  }
  function renderDrawerBody(force) {
    const d = O.drawer;
    if (!d || !d.body) return;
    if (!force && (inField(d.body) || selecting(d.body))) { d.stale = true; return; }
    const sig = drawerSig(d);
    if (!force && !d.stale && d.sig === sig) return;
    d.sig = sig;
    d.stale = false;
    const keep = d.body.scrollTop;
    const view = { room: roomBody, inbox: inboxBody, seats: seatsBody, wizard: wizardBody, usage: usageBody }[d.type]();
    keepFocus(d.body, () => put(d.body, view));
    d.body.scrollTop = keep;
  }
  function closeBtn() { return h('button', { class: 'btn ghost o-close', type: 'button', 'data-fk': 'close', onclick: closeDrawer }, '關閉'); }
  function renderDrawerHead() {
    const d = O.drawer;
    if (!d || !d.head) return;
    const sig = d.type === 'room' ? JSON.stringify([roomById(d.id), itemsOf(d.id).map((i) => i.kind)])
      : d.type === 'inbox' ? String(count()) : d.type === 'wizard' ? 'w' + O.wiz.step : d.type === 'usage' ? 'u' : 's';
    if (d.headSig === sig) return;
    d.headSig = sig;
    keepFocus(d.head, () => fillDrawerHead(d));
  }
  function fillDrawerHead(d) {
    if (d.type === 'inbox') {
      put(d.head, h('div', { class: 'title' }, h('div', { class: 'grow' }, h('h2', {}, `收件匣（${count()}）`),
        h('p', { class: 'dim' }, '所有房間等你決定的事，舊的在上面。')), closeBtn()));
    } else if (d.type === 'usage') {
      put(d.head, h('div', { class: 'title' }, h('div', { class: 'grow' }, h('h2', {}, '用量'),
        h('p', { class: 'dim' }, '每次問 Decision-1／glm-5.3 與每個 CLI 回合的實際紀錄。沒有回報的數字寫「—（未回報）」，不估算。')), closeBtn()));
    } else if (d.type === 'seats') {
      put(d.head, h('div', { class: 'title' }, h('div', { class: 'grow' }, h('h2', {}, '座位'),
        h('p', { class: 'dim' }, '每個座位是一種代理。額度用完會暫停派工，到時間自動試探；也可以手動試探。')), closeBtn()));
    } else if (d.type === 'wizard') {
      const w = O.wiz;
      put(d.head, h('div', { class: 'title' }, h('div', { class: 'grow' }, h('h2', {}, '派工')), closeBtn()),
        h('ol', { class: 'o-steps' }, STEPS.map((s, i) => h('li', { class: i < w.step ? 'past' : i === w.step ? 'now' : '', 'aria-current': i === w.step ? 'step' : null }, s))));
    } else if (d.type === 'room') {
      const r = roomById(d.id);
      if (!r) return;
      const s = statusLine(r);
      put(d.head, 
        h('div', { class: 'title' },
          h('div', { class: 'grow' }, h('h2', {}, r.name, r.demo ? h('span', { class: 'tag o-demotag' }, '示範（假代理）') : null),
            h('div', { class: 'loc' }, r.repo), r.branch ? h('div', { class: 'loc' }, '分支 ' + r.branch + (r.target_branch ? ` → ${r.target_branch}` : '')) : null,
            delegateRow(r)),
          roomControls(r), closeBtn()),
        h('p', { class: 'statusline' }, h('span', { class: 'tag st-' + r.status }, s.head), ' ', s.text),
        stageBar(r));
    }
  }
  // Task 20：代管開關（房間抽屜頂部）
  function delegBadge(r) {
    return h('span', { class: 'o-deleg', title: r.demo ? '代管中（示範：假模型決定）' : '代管中：原本要你按的事改由 Decision-1 與審查模型決定' },
      r.demo ? '代管・示範' : '代管');
  }
  function delegateRow(r) {
    const on = !!r.delegate;
    const b = h('button', { class: 'btn o-delbtn' + (on ? ' on' : ''), type: 'button', 'data-fk': 'ctl:delegate', 'aria-pressed': on ? 'true' : 'false',
      disabled: r.status === 'archived',
      onclick: async () => {
        b.disabled = true;
        await act('/api/team/rooms/delegate', { id: r.id, on: !on });
        b.disabled = false;
      } }, on ? '代管：開' : '代管：關');
    return h('div', { class: 'o-delrow' + (on ? ' on' : '') }, b,
      h('span', { class: 'o-delhint' }, '開啟後，原本要你按的事改由 Decision-1 與審查模型決定；合併與登入仍由你決定；危險指令與專案外路徑會直接拒絕',
        r.demo ? '（示範房用假模型）' : ''));
  }
  function stageBar(r) {
    const idx = r.status === 'done' ? 4 : (SEG_OF[r.phase] || 0);
    const tint = r.status === 'waiting_user' ? 'needs' : r.status === 'running' ? 'auto' : 'off';
    return h('div', { class: 'tint ' + tint + ' o-stagebar' }, h('div', { class: 'rail', 'aria-label': '階段：' + (PHASE[r.phase] || r.phase) },
      SEGMENTS.map((name, i) => h('div', { class: 'station ' + (i < idx ? 'past' : i === idx ? 'now' : '') }, name,
        i === 2 && (r.phase === 'build' || r.phase === 'test') ? h('span', { class: 'sub' }, PHASE[r.phase]) : null))));
  }
  function roomControls(r) {
    const btns = [];
    const call = async (what, b) => {
      if (what === 'archive' && !confirm(`封存「${r.name}」？房間會從辦公室移除，工作目錄刪掉、分支保留。`)) return;
      b.disabled = true;
      const res = await act('/api/team/rooms/' + what, { id: r.id }, { silent: what === 'archive' });
      b.disabled = false;
      if (what === 'archive') {
        if (res.ok) roomGone(true);
        else toast(res.text || '沒有封存', false);
      }
    };
    if (['running', 'queued', 'waiting_user'].includes(r.status)) {
      const b = h('button', { class: 'btn', type: 'button', 'data-fk': 'ctl:pause', onclick: () => call('pause', b) }, '暫停'); btns.push(b);
    }
    if (['paused', 'error'].includes(r.status)) {
      const b = h('button', { class: 'btn go', type: 'button', 'data-fk': 'ctl:resume', disabled: r.resume_requested, onclick: () => call('resume', b) }, r.resume_requested ? '等它停下後繼續' : '繼續'); btns.push(b);
    }
    if (['paused', 'error', 'done'].includes(r.status) && !itemsOf(r.id).some((i) => i.kind === 'merge')) {
      const b = h('button', { class: 'btn ghost', type: 'button', 'data-fk': 'ctl:archive', onclick: () => call('archive', b) }, '封存'); btns.push(b);
    }
    return h('div', { class: 'row' }, btns);
  }
  // 抽屜裡的房間不在了（封存）：關抽屜、提示、焦點移到換頁列
  function roomGone(announce) {
    if (!O.drawer || O.drawer.type !== 'room' || O.drawer.gone) return;
    O.drawer.gone = true;
    if (announce !== false) toast('已封存', true);
    O.opener = visible() ? O.els.pager.querySelector('button:not([disabled])') : null;
    closeDrawer();
  }
  async function loadDetail(id, first) {
    const r = roomById(id);
    if (r) {
      const ev = r.events && r.events.length ? r.events[r.events.length - 1] : {};
      O.detailSig[id] = r.updated_at + '|' + ev.ts + '|' + ev.text;
    }
    clearTimeout(loadDetail['t' + id]);
    loadDetail['t' + id] = setTimeout(async () => {
      const d = await api('/api/team/room?id=' + encodeURIComponent(id));
      if (d && d.room) {
        O.detail[id] = d;
        if (O.drawer && O.drawer.type === 'room' && O.drawer.id === id) renderDrawerBody(false);
        if (O.close && O.close.id === id) renderCloseTimeline();
      }
    }, first ? 0 : 250);
  }

  // ---- 房間抽屜
  function roomBody() {
    const r = roomById(O.drawer.id);
    const f = document.createDocumentFragment();
    if (!r) return f;
    const items = itemsOf(r.id);
    if (items.length) {
      add(f, h('section', { class: 'sect' }, h('h3', {}, '等你決定', h('small', {}, '回答後代理就會繼續')),
        h('div', { class: 'o-items' }, items.map((it) => inboxItem(it, { inRoom: true })))));
    }
    if (r.status === 'error' && r.error) {
      add(f, h('section', { class: 'sect' }, h('h3', {}, '出錯了'), h('div', { class: 'box pause' }, h('p', { class: 'o-pre' }, r.error),
        h('p', { class: 'hint' }, '照上面的建議處理後，按右上「繼續」。'))));
    }
    if (r.unverified) {
      add(f, h('section', { class: 'sect' }, h('h3', {}, 'QA 無法驗證測試'), h('div', { class: 'box heatbox' }, h('p', { class: 'o-pre' }, r.unverified))));
    }
    // 對代理說話
    const key = 'talk:' + r.id;
    const canSay = !['done', 'archived'].includes(r.status) && !['test', 'review', 'done'].includes(r.phase);
    const ta = h('textarea', { rows: 3, id: 'o-talk', 'aria-label': '對代理說話', placeholder: '例如：測試請用 pytest，不要用 unittest；先別動 README' }, O.drafts[key] || '');
    ta.addEventListener('input', () => { O.drafts[key] = ta.value; });
    const mk = 'room:' + r.id;
    const out = msgEl(mk);
    const say = (m) => { O.msg[mk] = m; out.className = m.ok ? 'o-out' : 'o-err'; out.textContent = m.text; };
    const send = h('button', { class: 'btn primary', type: 'button', 'data-fk': 'talk:send', onclick: async () => {
      if (!ta.value.trim()) { say({ ok: false, text: '請先寫要說的話' }); return; }
      send.disabled = true;
      const res = await api('/api/team/send', { id: r.id, text: ta.value });
      send.disabled = false;
      if (res.ok) { delete O.drafts[key]; ta.value = ''; say({ ok: true, text: res.text || '已轉告' }); }
      else say({ ok: false, text: res.text || '沒有送出' });
    } }, '送出');
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send.click(); });
    if (canSay) add(f, h('section', { class: 'sect' }, h('h3', {}, '對代理說話', h('small', {}, 'Ctrl+Enter 送出；下一回合轉告')), ta, h('div', { class: 'row' }, send), out));
    // 陣容
    const di = deskText(r, 'impl'); const dr = deskText(r, 'review');
    const p = Math.round(pct(r));
    const seatUse = (role) => {
      const seat = deskSeat(r, role);
      const t = seat && r.tokens ? r.tokens[seat] : null;
      if (!t) return null;
      return h('span', { class: 'o-subline', 'data-tokens': seat }, `本房累計：${num(t.turns)} 回合，輸入 ${num(t.in)}／輸出 ${num(t.out)} token`,
        t.cost !== null && t.cost !== undefined ? `，費用 ${costText({ [t.currency || '']: t.cost })}` : '');
    };
    const why = (d) => (d.reason && d.reason !== '使用者指定' ? h('span', { class: 'o-subline o-why' }, '派工理由：' + d.reason) : null);
    const fl = Array.isArray(r.inflight) ? r.inflight : [];
    add(f, h('section', { class: 'sect' }, h('h3', {}, '陣容'),
      h('dl', { class: 'kv' },
        h('dt', {}, '實作'), h('dd', {}, di.main, di.tag ? h('span', { class: 'o-pick', title: di.tip }, di.tag) : null, di.sub ? h('span', { class: 'o-subline' }, di.sub) : null, why(di), seatUse('impl')),
        h('dt', {}, '審查（唯讀）'), h('dd', {}, dr.main, dr.tag ? h('span', { class: 'o-pick', title: dr.tip }, dr.tag) : null, dr.sub ? h('span', { class: 'o-subline' }, dr.sub) : null, why(dr), seatUse('review')),
        fl.length ? [h('dt', {}, '模型進行中'), h('dd', {}, fl.map((c) => h('span', { class: 'o-subline' },
          h('span', { class: 'o-thk', 'data-since': c.since, 'data-model': c.model, 'data-role': c.role }, thinkText(c)), `：${ROLE_ALL[c.role] || c.role}・${c.purpose}`)))] : null,
        modelPickerRoom(r),
        h('dt', {}, '對話用量'), h('dd', {}, h('span', { class: 'o-meter' }, h('i', { class: p >= 70 ? 'hi' : p >= 50 ? 'mid' : 'lo', style: null })),
          ` ${p}%${r.context_estimated ? '（估）' : ''}；已換新對話 ${r.rollovers || 0} 次`),
        h('dt', {}, '自主程度'), h('dd', {}, (AUTONOMY.find((a) => a[0] === r.autonomy) || [0, r.autonomy])[1]),
        h('dt', {}, '可修改範圍'), h('dd', {}, r.allowed_paths && r.allowed_paths.length ? r.allowed_paths.map((x) => h('code', { class: 'o-path' }, x)) : '整個 repo'),
        h('dt', {}, '目標'), h('dd', {}, r.goal),
        r.source ? [h('dt', {}, '工作來源'), h('dd', {}, r.source)] : null)));
    const meter = f.querySelector('.o-meter i');
    if (meter) meter.style.width = Math.min(100, p) + '%';
    // 驗收條件
    add(f, h('section', { class: 'sect' }, h('h3', {}, '驗收條件', h('small', {}, r.criteria.length ? `${r.criteria.length} 條` : 'PO 還沒提出')),
      r.criteria.length && ['review', 'done'].includes(r.phase) ? h('p', { class: 'o-gate' }, '測試關卡已通過：整套測試與每條的檢查指令都由助理在 worktree 跑過。') : null,
      r.criteria.length ? h('div', { class: 'box' }, h('ul', { class: 'checks' }, r.criteria.map((c) => {
        const [cls, label] = criteriaState(r, c);
        return h('li', { class: 'o-c ' + cls }, h('span', { class: 'mark' }, { ok: '✓', run: '…', stop: '!', manual: '?', todo: '○' }[cls]),
          h('div', {}, h('div', {}, h('b', {}, c.id), ' ', c.text), h('div', { class: 'meta' }, label, '｜檢查：', h('code', {}, c.check || 'manual'))));
      })), r.test_cmd ? h('p', { class: 'hint' }, '整套測試：', h('code', {}, r.test_cmd)) : null)
        : h('p', { class: 'dim' }, '開工後 PO 會先讀專案，提出每條都附檢查指令的驗收條件。')));
    // 時間軸
    const det = O.detail[r.id];
    const evs = (det ? det.events : r.events || []).slice().reverse();
    add(f, h('section', { class: 'sect' }, h('h3', {}, '最近發生的事', h('small', {}, det ? `最近 ${evs.length} 筆` : '讀取中…')),
      evs.length ? timelineList(evs, 80, r.id) : h('p', { class: 'dim' }, '還沒有紀錄。')));
    return f;
  }
  // 時間軸：同一個工具的「用工具」與結果合併成一列（以 tool_call_id 對應）
  // ---- 細節：清單只帶 has_detail；展開時才依事件 id 讀（/api/team/room/events），讀過的留在 O.evd
  function detOf(e) {
    if (!e) return null;
    if (e.detail && typeof e.detail === 'object') return e.detail;
    return e.id && O.evd[e.id] ? O.evd[e.id] : null;
  }
  const wants = (e) => !!e && ((e.detail && typeof e.detail === 'object') || !!e.has_detail);
  const missing = (e) => !!e && !e.detail && !!e.has_detail && !!e.id && !(e.id in O.evd);
  async function ensureDetails(rid, ids) {
    const need = ids.filter((id) => id && !(id in O.evd) && !O.evdBusy.has(id) && !O.evdFail.has(id));
    if (!rid || !need.length) return;
    need.forEach((id) => O.evdBusy.add(id));
    const r = await api(`/api/team/room/events?id=${encodeURIComponent(rid)}&ids=${encodeURIComponent(need.join(','))}`);
    need.forEach((id) => O.evdBusy.delete(id));
    if (!r || !Array.isArray(r.events)) {
      // 讀不到：標成失敗，不再自動重讀；列上顯示「點此重試」
      need.forEach((id) => O.evdFail.add(id));
      O.evdVer = (O.evdVer || 0) + 1;
      rerenderTimelines(rid);
      return;
    }
    for (const e of r.events) if (e && e.id) O.evd[e.id] = e.detail || {};
    for (const id of need) if (!(id in O.evd)) O.evd[id] = {}; // 已經被輪替掉
    O.evdVer = (O.evdVer || 0) + 1;
    const keys = Object.keys(O.evd);
    if (keys.length > 400) keys.slice(0, keys.length - 400).forEach((k) => delete O.evd[k]);
    rerenderTimelines(rid);
  }
  function rerenderTimelines(rid) {
    if (O.drawer && O.drawer.type === 'room' && O.drawer.id === rid) renderDrawerBody(false);
    if (O.close && O.close.id === rid) { O.close.sig = ''; renderCloseTimeline(); }
  }
  function retryDetails(rid, ids) {
    ids.forEach((id) => O.evdFail.delete(id));
    O.evdVer = (O.evdVer || 0) + 1;
    rerenderTimelines(rid);
    ensureDetails(rid, ids);
  }
  function onDetailsToggle(e) {
    const t = e.target;
    const k = t && t.dataset ? t.dataset.dk : '';
    if (!k) return;
    if (t.open) O.openDetails.add(k); else O.openDetails.delete(k);
    if (t.open && t.dataset.need) ensureDetails(t.dataset.room, t.dataset.need.split(','));
  }
  // 時間軸：同一個工具的「用工具」、更新與結果合併成一列（以座位＋對話＋toolCallId 的 tool_key 對應）
  const toolKey = (e) => e.tool_key || (e.tool_call_id ? `${e.who || ''}|${e.tool_call_id}` : '');
  function timelineList(evs, limit, rid) {
    const groups = {};
    for (const e of evs) { // 新→舊
      const k = ['tool', 'tool_update', 'tool_result'].includes(e.kind) ? toolKey(e) : '';
      if (!k) continue;
      const g = groups[k] || (groups[k] = { tool: null, updates: [], result: null });
      if (e.kind === 'tool' && !g.tool) g.tool = e;
      else if (e.kind === 'tool_result' && !g.result) g.result = e;
      else if (e.kind === 'tool_update') g.updates.unshift(e); // 舊→新，後面的蓋前面的
    }
    const rows = [];
    for (const e of evs) {
      if (rows.length >= limit) break;
      const k = ['tool', 'tool_update', 'tool_result'].includes(e.kind) ? toolKey(e) : '';
      const g = k ? groups[k] : null;
      if (g && e.kind !== 'tool' && g.tool) continue; // 併進工具那一列
      if (g && e.kind === 'tool_update' && g.result) continue;
      rows.push(eventRow(e, e.kind === 'tool' ? g || { tool: e, updates: [], result: null } : null, rid));
    }
    return h('ul', { class: 'timeline o-tl' }, rows);
  }
  const TOOL_STATE = { completed: ['完成', 'ok'], failed: ['失敗', 'bad'] };
  function eventRow(e, group, rid) {
    const result = group ? group.result : null;
    let text = e.text;
    let more = null;
    let cls = 'k-' + e.kind;
    let tag = null;
    let state = null;
    if (e.kind === 'model') {
      const k = e.model === 'scripted' ? 'demo' : e.model_kind === 'd1' ? 'd1' : 'glm';
      tag = h('span', { class: 'o-mtag ' + k, title: modelName(e.model, e.effort, e.model_kind) }, { demo: '示範', d1: 'D1', glm: 'GLM' }[k]);
      text = String(e.text || '').replace(/^(D1|GLM|示範) /, '');
      if (!e.ok) cls += ' bad';
    } else if (e.kind === 'cli_usage') {
      tag = h('span', { class: 'o-mtag cli' }, 'CLI');
    } else if (e.kind === 'decision' || e.kind === 'delegate' || (e.kind === 'permission' && e.delegate)) {
      tag = h('span', { class: 'o-mtag deleg' }, e.demo ? '代管・示範' : '代管');
    }
    if (e.kind === 'rollover') text = `換新對話（用量 ${Math.round(Number(e.pct) || 0)}%）`;
    else if (e.kind === 'prompt') { text = `交代給 ${e.who || '代理'}${e.role && ROLE[e.role] ? '（' + ROLE[e.role] + '）' : ''}`; if (!e.detail) more = e.text; }
    else if (e.kind === 'agent' || e.kind === 'tool') {
      const full = String(e.text || '');
      text = `${e.who || '代理'}${e.kind === 'tool' ? ' 用工具' : ''}：${short(full, 140)}`;
      if (full.length > 140) more = full;
      if (e.kind === 'tool' && e.tool_call_id) {
        const st = result ? result.status || (result.detail && result.detail.status) || '' : '';
        const [label, c] = TOOL_STATE[st] || ['進行中', 'run'];
        state = h('span', { class: 'o-tstate ' + c }, label);
      }
    } else if (e.kind === 'tool_result') {
      text = `${e.who || '代理'} 工具結果：${short(String(e.text || ''), 140)}`;
    } else if (e.kind === 'say' && e.nudge) text = String(e.text || '').replace(/^【[^】]*】/, 'Scrum Master 督促：');
    else if (e.kind === 'permission' && e.outcome === 'escalate') cls += ' hot';
    else if (String(text || '').length > 220) { more = text; text = short(text, 220); }
    // 有細節的事件（含合併進來的更新與結果）：還沒讀到的先顯示「讀取中」，展開時才去讀
    const members = [e].concat(group ? group.updates : [], result ? [result] : []);
    const has = members.some(wants);
    const need = members.filter(missing).map((x) => x.id);
    let moreNode = null;
    if (has) {
      cls += ' has-more';
      const dk = `${e.ts}|${e.kind}|${e.id || toolKey(e) || hash(String(e.text || ''))}`;
      const failed = need.filter((id) => O.evdFail.has(id));
      const body = failed.length
        ? h('button', { class: 'btn ghost o-dretry', type: 'button', 'data-fk': 'retry:' + dk, onclick: () => retryDetails(rid, need) }, '細節讀取失敗，點此重試')
        : need.length ? h('p', { class: 'dim o-dload', role: 'status' }, '讀取細節中…') : detailBody(e, group);
      moreNode = moreEl(dk, body || h('p', { class: 'dim' }, '沒有更多細節。'), need, rid);
      if (need.length && !failed.length && O.openDetails.has(dk)) setTimeout(() => ensureDetails(rid, need), 0);
    }
    return h('li', { class: cls }, h('time', {}, hms(e.ts)),
      h('div', {}, h('span', { class: 'o-tlh' }, tag, tag ? ' ' : null, h('span', {}, text), state ? ' ' : null, state),
        moreNode || (more ? detailsEl(`${e.ts}|${e.kind}|${hash(more)}`, more) : null)));
  }
  function detailsEl(dk, more) {
    return h('details', { 'data-dk': dk, open: O.openDetails.has(dk) }, h('summary', {}, '全文'), h('pre', { class: 'md' }, more));
  }
  // 展開後的細節：依事件類型排版；內容都是後端遮蔽過的
  function moreEl(dk, body, need, rid) {
    return h('details', { class: 'o-more', 'data-dk': dk, open: O.openDetails.has(dk),
      'data-need': need && need.length ? need.join(',') : null, 'data-room': rid || null },
      h('summary', { 'aria-label': '展開細節' }, h('span', { class: 'o-mopen' }, '▸ 展開'), h('span', { class: 'o-mclose' }, '▾ 收合')), body);
  }
  function block(label, text, cls) {
    if (text === undefined || text === null || text === '') return null;
    const pre = h('pre', { class: 'o-dpre' + (cls ? ' ' + cls : '') }, String(text));
    const copy = h('button', { class: 'btn ghost o-copy', type: 'button', 'aria-label': `複製${label}`, onclick: async () => {
      try { await navigator.clipboard.writeText(String(text)); copy.textContent = '已複製'; }
      catch (err) { const r = document.createRange(); r.selectNodeContents(pre); const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r); copy.textContent = '已選取，按 Ctrl+C'; }
      setTimeout(() => { copy.textContent = '複製'; }, 1500);
    } }, '複製');
    return h('div', { class: 'o-dblock' }, h('div', { class: 'o-dhead' }, h('b', {}, label), copy), pre);
  }
  function kv(pairs) {
    const rows = pairs.filter(([, v]) => v !== undefined && v !== null && v !== '');
    if (!rows.length) return null;
    return h('dl', { class: 'kv o-dkv' }, rows.map(([k, v]) => [h('dt', {}, k), h('dd', {}, String(v))]));
  }
  const DECISION = { allow: '自動允許', reject: '自動拒絕', escalate: '交給你決定' };
  // 長路徑：中間省略，完整路徑在滑過提示
  const shortPath = (p) => { const t = String(p); return t.length <= 80 ? t : t.slice(0, 24) + '…' + t.slice(-52); };
  function detailBody(e, group) {
    let d = detOf(e);
    const result = group ? group.result : null;
    if (e.kind === 'tool' && group) {
      // 工具：原本的細節＋更新補上的欄位（只蓋有的）＋結果
      const fromResult = {};
      const rdet = detOf(result) || {};
      for (const k of ['title', 'kind', 'command', 'paths', 'input']) if (rdet[k]) fromResult[k] = rdet[k];
      d = Object.assign({}, d || {}, ...group.updates.map((u) => detOf(u) || {}), fromResult);
      const rd = Object.assign({}, detOf(result) || {}, result && result.status ? { status: result.status } : {});
      return h('div', { class: 'o-detail-box' },
        kv([['工具', d && d.title], ['種類', d && d.kind], ['狀態', rd.status ? (TOOL_STATE[rd.status] || [rd.status])[0] : '進行中（還沒有結果）']]),
        d ? block('指令', d.command, 'mono') : null,
        d && d.paths && d.paths.length ? h('div', { class: 'o-dblock' }, h('div', { class: 'o-dhead' }, h('b', {}, '檔案')),
          h('ul', { class: 'o-dpaths' }, d.paths.map((p) => h('li', { title: p }, h('code', {}, shortPath(p)))))) : null,
        d ? block('其他參數', d.input, 'mono') : null,
        block('輸出', rd.output, 'mono'));
    }
    if (!d) return null;
    if (e.kind === 'tool_update') return h('div', { class: 'o-detail-box' }, kv([['工具', d.title], ['種類', d.kind]]), block('指令', d.command, 'mono'),
      d.paths && d.paths.length ? kv([['檔案', d.paths.map(shortPath).join('、')]]) : null);
    if (e.kind === 'tool_result') return h('div', { class: 'o-detail-box' }, kv([['狀態', (TOOL_STATE[d.status] || [d.status])[0]]]), block('輸出', d.output, 'mono'));
    if (e.kind === 'permission') {
      return h('div', { class: 'o-detail-box' }, block('指令', d.command, 'mono'),
        d.paths && d.paths.length ? kv([['檔案', d.paths.join('、')]]) : null,
        kv([['結果', DECISION[d.decision] || d.decision], ['allow', d.allow], ['risk', d.risk], ['判斷的模型', d.by], ['各模型數字', d.models], ['原因', d.reason]]));
    }
    if (e.kind === 'model') {
      const gen = e.model_kind !== 'd1' && !d.state_excerpt;
      return h('div', { class: 'o-detail-box' }, block(gen ? '提示' : '題目', d.question),
        block('送出的狀態摘要（前 2000 字）', d.state_excerpt), block(gen ? '生成文字' : '答案', d.answer),
        kv([['門檻', d.threshold], ['結論', d.verdict]]));
    }
    if (e.kind === 'decision') {
      return h('div', { class: 'o-detail-box' },
        kv([['項目', d.item], ['題目', d.title], ['決定', d.choice], ['門檻', d.threshold]]),
        block('理由', d.reason), block('各模型的答案', d.models), block('錯誤', d.error));
    }
    if (e.kind === 'say' && (d.generated || d.sent)) {
      return h('div', { class: 'o-detail-box' }, block('模型產生的原文', d.generated), block('實際送給代理的訊息', d.sent));
    }
    if (e.kind === 'prompt' && d.sent) return h('div', { class: 'o-detail-box' }, block('送給代理的完整訊息', d.sent));
    return null;
  }

  // ---- 收件匣抽屜
  function inboxBody() {
    const items = inbox();
    if (!items.length) return h('div', { class: 'empty' }, h('b', {}, '收件匣是空的'), '代理需要你允許、確認或合併時，會出現在這裡。');
    return h('div', { class: 'o-items' }, items.map((it) => inboxItem(it)));
  }

  // ---- 座位抽屜
  async function loadCatalog() {
    const r = await api('/api/team/fleet');
    if (r && r.catalog) { O.catalog = r.catalog; if (O.drawer && O.drawer.type === 'seats') renderDrawerBody(false); }
  }
  function certLine() {
    const c = (O.snap && O.snap.certs) || {};
    const text = c.state === 'ok' ? `公司網路憑證：已匯出 Windows 憑證 ${c.path}${c.reason ? '（' + c.reason + '）' : ''}`
      : c.state === 'user' ? `公司網路憑證：使用你設定的 ${c.path}`
      : c.state === 'failed' ? `公司網路憑證：匯出失敗（${c.reason || '原因不明'}）`
      : c.state === 'off' ? '公司網路憑證：不需要（不是 Windows）'
      : '公司網路憑證：還沒匯出（第一次啟動 CLI 時匯出）';
    return h('p', { class: 'meta o-certs' + (c.state === 'failed' ? ' o-err' : ''), 'data-certs': c.state || 'unknown' }, text);
  }
  function seatsBody() {
    return h('div', {}, certLine(), seatRows());
  }
  function seatRows() {
    const fleet = (O.snap && O.snap.fleet) || {};
    return h('ul', { class: 'o-seatrows' }, SEATS.map((s) => {
      const f = fleet[s] || {};
      const st = seatState(f, s);
      const sw = h('span', { class: 'o-sw big' }, SEAT[s].g);
      paintSwatch(sw, s);
      O.seatMsg = O.seatMsg || {};
      const out = h('span', { class: 'o-out', role: 'status' }, O.seatMsg[s] || '');
      const say = (r) => { O.seatMsg[s] = r.text || ''; out.textContent = O.seatMsg[s]; out.className = r.ok === false ? 'o-err' : 'o-out'; };
      const probe = h('button', { class: 'btn', type: 'button', 'data-fk': 'probe:' + s, onclick: async () => {
        probe.disabled = true;
        say(await api('/api/team/fleet/probe', { seat: s }));
        probe.disabled = false;
      } }, '立即試探');
      const reset = h('button', { class: 'btn ghost', type: 'button', 'data-fk': 'reset:' + s, onclick: async () => {
        say(await api('/api/team/fleet/reset', { seat: s }));
      } }, '標為可用');
      const usedBy = (f.rooms || []).map((id) => roomById(id)).filter(Boolean);
      const models = O.catalog && O.catalog[s] ? O.catalog[s].map((m) => m.id || 'CLI 預設').join('、') : '';
      return h('li', { class: 'o-seatrow ' + st.cls, 'data-seat': s }, sw,
        h('div', { class: 'grow' },
          h('div', {}, h('b', {}, f.label || SEAT[s].name), ' ', h('span', { class: 'o-sst ' + st.cls, 'data-seat': s }, st.long)),
          h('div', { class: 'meta' }, usedBy.length ? ['使用中：', usedBy.map((r, i) => [i ? '、' : '', h('button', { class: 'o-link', type: 'button',
            onclick: () => { closeDrawer(); jumpTo(r.id); openDrawer('room', r.id); } }, r.name)])] : '沒有房間在用'),
          models ? h('div', { class: 'meta' }, '模型：' + models) : null, out),
        h('div', { class: 'row' }, probe, st.cls === 'full' ? reset : null));
    }));
  }

  // ---- 用量抽屜：數字一律來自 /api/team/usage
  async function loadUsage() {
    const now = Date.now();
    if (O.usageBusy || now - (O.usageAt || 0) < 1500) {
      // 太頻繁：稍後再抓一次最新的
      clearTimeout(O.usageT);
      O.usageT = setTimeout(() => { if (O.drawer && O.drawer.type === 'usage') loadUsage(); }, 1600);
      return;
    }
    O.usageBusy = true;
    O.usageAt = now;
    const u = await api('/api/team/usage');
    O.usageBusy = false;
    if (u && u.today) { O.usage = u; O.usageErr = ''; } else O.usageErr = (u && u.text) || '讀不到用量';
    if (O.drawer && O.drawer.type === 'usage') renderDrawerBody(false);
  }
  function tokenCell(m) {
    if (m.tokens === 'not_counted') return '不計 token（Decision-1 不回報）';
    if (m.tokens === 'demo') return '示範不計 token';
    if (m.tokens === 'unreported') return NA;
    return `${num(m.total_tokens)}（輸入 ${num(m.prompt_tokens)}／輸出 ${num(m.completion_tokens)}）${m.partial ? '，部分呼叫未回報' : ''}`;
  }
  function table(caption, cols, rows, empty) {
    if (!rows.length) return h('p', { class: 'dim o-uempty' }, empty);
    return h('table', { class: 'o-utable' }, h('caption', { class: 'sr' }, caption),
      h('thead', {}, h('tr', {}, cols.map((c) => h('th', { scope: 'col' }, c)))),
      h('tbody', {}, rows));
  }
  function usageBody() {
    const f = document.createDocumentFragment();
    const u = O.usage;
    if (!u) return h('p', { class: 'dim' }, O.usageErr || '讀取中…');
    const scope = O.usageScope === 'session' ? 'session' : 'today';
    const d = u[scope] || { models: [], cli: [], rooms: [], roles: [] };
    const pick = (k, label) => h('button', { class: 'btn' + (scope === k ? ' primary' : ''), type: 'button', 'data-fk': 'scope:' + k,
      'aria-pressed': scope === k ? 'true' : 'false', onclick: () => { O.usageScope = k; renderDrawerBody(true); } }, label);
    add(f, h('div', { class: 'row o-uscope', role: 'group', 'aria-label': '範圍' }, pick('today', `今日（${u.today.date}）`),
      pick('session', `本次啟動（${String(u.started_at || '').replace('T', ' ').slice(0, 16)} 起）`)));
    if (u.models_ready === false) add(f, h('p', { class: 'o-notice' }, '判斷模型沒有設定（litellm.local.json）：Decision-1 與 glm-5.3 都不會被呼叫。'));
    // 進行中
    const fl = u.inflight || [];
    if (fl.length) {
      add(f, h('section', { class: 'sect' }, h('h3', {}, '進行中的模型呼叫'), h('ul', { class: 'o-ulist' }, fl.map((c) => {
        const r = roomById(c.room_id);
        return h('li', {}, h('span', { class: 'o-thk', 'data-since': c.since, 'data-model': c.model, 'data-role': c.role }, thinkText(c)),
          ' ', h('span', { class: 'dim' }, `${r ? r.name + '・' : ''}${ROLE_ALL[c.role] || c.role || '辦公室'}：${c.purpose}`));
      }))));
    }
    // 模型
    add(f, h('section', { class: 'sect' }, h('h3', {}, '判斷與寫作模型', h('small', {}, `設定：${u.d1_model}、${u.glm_model}`)),
      table('模型用量', ['模型', '呼叫', '失敗', '平均延遲', 'token'], d.models.map((m) =>
        h('tr', { 'data-model': `${m.model}|${m.effort}` },
          h('th', { scope: 'row' }, h('span', { class: 'o-mtag ' + (m.model === 'scripted' ? 'demo' : m.kind) }, m.model === 'scripted' ? '示範' : m.kind === 'd1' ? 'D1' : 'GLM'), ' ', m.label),
          h('td', { 'data-k': 'calls' }, num(m.calls)), h('td', { 'data-k': 'fail' }, num(m.fail)),
          h('td', { 'data-k': 'latency' }, `${num(m.avg_latency_ms)} ms`), h('td', { 'data-k': 'tokens' }, tokenCell(m)))),
      '這段期間還沒有呼叫判斷模型。')));
    // CLI
    add(f, h('section', { class: 'sect' }, h('h3', {}, '代理 CLI', h('small', {}, '每回合結束時 CLI 回報的數字')),
      table('CLI 用量', ['座位', '實際模型', '回合', '輸入 token', '輸出 token', '費用'], d.cli.map((c) =>
        h('tr', { 'data-seat': c.seat + (c.demo ? ':demo' : '') },
          h('th', { scope: 'row' }, seatLabel(c.seat), c.demo ? h('span', { class: 'o-demo' }, '示範') : null),
          h('td', {}, c.models.length ? c.models.join('、') : CLI_DEFAULT),
          h('td', { 'data-k': 'turns' }, num(c.turns), c.reported_turns < c.turns && c.reported_turns ? h('small', { class: 'dim' }, `（${c.reported_turns} 回合有回報）`) : null),
          h('td', { 'data-k': 'in' }, num(c.in)), h('td', { 'data-k': 'out' }, num(c.out)), h('td', { 'data-k': 'cost' }, costText(c.cost)))),
      '這段期間還沒有 CLI 回合。')));
    // 房間排行
    add(f, h('section', { class: 'sect' }, h('h3', {}, '依房間'),
      table('房間用量', ['房間', '模型呼叫', '模型 token', 'CLI 輸入', 'CLI 輸出', '費用'], d.rooms.map((r) =>
        h('tr', { 'data-room': r.room_id }, h('th', { scope: 'row' }, r.name),
          h('td', { 'data-k': 'calls' }, num(r.model_calls)), h('td', { 'data-k': 'mtok' }, r.model_tokens ? num(r.model_tokens) : NA),
          h('td', { 'data-k': 'in' }, num(r.cli_in)), h('td', { 'data-k': 'out' }, num(r.cli_out)), h('td', {}, costText(r.cost)))),
      '這段期間還沒有房間用到模型或 CLI。')));
    // 角色
    if (d.roles.length) {
      add(f, h('section', { class: 'sect' }, h('h3', {}, '依角色'), h('ul', { class: 'o-ulist' }, d.roles.map((x) =>
        h('li', { 'data-role': x.role }, h('b', {}, ROLE_ALL[x.role] || x.label || x.role), `：呼叫 ${num(x.calls)} 次`, x.total_tokens ? `，token ${num(x.total_tokens)}` : '')))));
    }
    return f;
  }

  // ============================================================ 模型選擇：派工精靈第 3 步與房間抽屜
  // 選項只來自 GET /api/team/models（偵測到或允許的模型）；值是「auto」或「座位|模型」（模型空＝依設定頁）。
  const AUTO_DEFAULT = '自動（Decision-1 決定）';
  // 伺服器給的「自動」說明（例：自動（Decision-1 在 Claude／GLM 中選））；還沒讀到就用預設
  const autoPick = () => (O.models && O.models.auto_label) || AUTO_DEFAULT;
  async function loadModels(force) {
    if (O.modelsBusy || (!force && O.models && Date.now() - (O.modelsAt || 0) < 15000)) return;
    O.modelsBusy = true;
    const v = await api('/api/team/models');
    O.modelsBusy = false;
    O.modelsAt = Date.now();
    if (v && v.seats) { O.models = v; O.modelsErr = ''; } else O.modelsErr = (v && v.text) || '讀不到模型清單';
    // 只重畫用得到模型清單的畫面（精靈其他步驟重畫會打斷正在輸入的欄位）
    if (O.drawer && ((O.drawer.type === 'wizard' && O.wiz && O.wiz.step === 2) || O.drawer.type === 'room')) renderDrawerBody(O.drawer.type === 'wizard');
  }
  const pickVal = (seat, model) => (!seat || seat === 'auto' ? 'auto' : `${seat}|${model || ''}`);
  const parsePick = (v) => { if (!v || v === 'auto') return { seat: 'auto', model: '' }; const i = v.indexOf('|'); return { seat: v.slice(0, i), model: v.slice(i + 1) }; };
  function seatName(seat) {
    const m = O.models && O.models.seats && O.models.seats[seat];
    return (m && m.label) || seatLabel(seat);
  }
  function pickLabel(p) {
    if (!p || p.seat === 'auto' || !p.seat) return autoPick();
    return p.model ? `${seatName(p.seat)}・${p.model}` : `${seatName(p.seat)}（模型依設定頁）`;
  }
  // 一個下拉：自動在最前；每個可用座位一組（座位、該座位可用的模型）
  function modelSelect(id, cur, label) {
    const v = O.models;
    const groups = [];
    const order = v ? v.order || Object.keys(v.seats) : Object.keys((O.snap && O.snap.clis) || {});
    for (const seat of order) {
      const info = v ? v.seats[seat] : { label: seatLabel(seat), available: true, models: [], known: false };
      if (!info || !info.available) continue;
      const models = (info.models || []).filter((m) => !m.missing);
      const opts = [h('option', { value: pickVal(seat, ''), selected: cur === pickVal(seat, '') }, `${info.label || seatLabel(seat)}（模型依設定頁）`)];
      // 還沒偵測到清單的 ACP 座位只能選座位，伺服器也不收別的模型
      if (info.known !== false || !['kiro', 'agy'].includes(seat)) {
        for (const m of models) {
          opts.push(h('option', { value: pickVal(seat, m.id), selected: cur === pickVal(seat, m.id) },
            `${info.label || seatLabel(seat)}・${m.id}${m.name && m.name !== m.id ? '（' + m.name + '）' : ''}`));
        }
      }
      groups.push(h('optgroup', { label: info.label || seatLabel(seat) }, opts));
    }
    return h('select', { id, 'aria-label': label, 'data-fk': 'pick:' + id }, h('option', { value: 'auto', selected: cur === 'auto' }, autoPick()), groups);
  }
  function undetected() {
    const v = O.models;
    if (!v) return [];
    return ['kiro', 'agy'].filter((s) => v.seats[s] && v.seats[s].available && v.seats[s].known === false).map((s) => v.seats[s].label || seatLabel(s));
  }
  // 精靈第 3 步：實作／審查各一個「座位＋模型」
  function modelPickerWizard(w) {
    if (!O.models && !O.modelsBusy) loadModels();
    const cur = (role) => pickVal(w[role], w[role + 'Model']);
    const sel = (id, role, label) => {
      const el = modelSelect(id, cur(role), label);
      el.addEventListener('change', () => {
        const p = parsePick(el.value);
        w[role] = p.seat; w[role + 'Model'] = p.model;
        const other = role === 'impl' ? 'review' : 'impl';
        if (p.seat !== 'auto' && w[other] === p.seat && role === 'impl') { w.review = 'auto'; w.reviewModel = ''; }
        renderDrawerBody(true);
        const again = O.drawer.body.querySelector('#' + id); if (again) again.focus();
      });
      return el;
    };
    const none = O.models && !Object.values(O.models.seats).some((x) => x.available);
    const und = undetected();
    return h('div', { class: 'o-picks' },
      h('div', { class: 'form' },
        h('div', { class: 'field' }, h('label', { for: 'o-impl' }, '實作'), sel('o-impl', 'impl', '實作的座位與模型')),
        h('div', { class: 'field' }, h('label', { for: 'o-rev' }, '審查（唯讀）'), sel('o-rev', 'review', '審查的座位與模型'))),
      O.modelsErr ? h('p', { class: 'o-err' }, O.modelsErr + '（只能選自動）') : null,
      !O.models && !O.modelsErr ? h('p', { class: 'hint' }, '讀取可用的模型中…') : null,
      none ? h('p', { class: 'hint' }, '這台電腦沒有可用的座位，只能交給 Decision-1 派工。') : null,
      und.length ? h('p', { class: 'hint' }, `${und.join('、')} 還沒偵測到模型清單，只能選座位（到設定頁「模型」按重新偵測）。`) : null);
  }
  // 房間抽屜：目前設定＋改模型（下一回合生效）
  function modelPickerRoom(r) {
    const prefOf = (role) => {
      const pref = role === 'impl' ? r.impl_pref : r.review_pref;
      if (pref && pref.seat) return { seat: pref.seat, model: pref.model || '' };
      const cli = role === 'impl' ? r.impl_cli : r.review_cli;
      return { seat: cli && cli !== 'auto' ? cli : 'auto', model: '' };
    };
    if (r.demo) {
      return [h('dt', {}, '模型'), h('dd', { class: 'o-mpick' }, h('p', { class: 'hint' }, '示範房使用假代理，不能換模型。'))];
    }
    const ended = ['done', 'archived'].includes(r.status);
    if (!O.models && !O.modelsBusy && !ended) loadModels();
    const rows = ['impl', 'review'].map((role) => {
      const what = role === 'impl' ? '實作' : '審查';
      const now = prefOf(role);
      const key = `model:${r.id}:${role}`;
      const draft = O.drafts[key] !== undefined ? O.drafts[key] : pickVal(now.seat, now.model);
      const out = msgEl(key);
      const row = h('div', { class: 'o-mrow', 'data-role': role },
        h('div', { class: 'o-mnow' }, `${what}：`, h('b', {}, pickLabel(now))));
      if (!ended) {
        const sel = modelSelect(`o-m-${role}`, draft, `${what}的座位與模型`);
        sel.addEventListener('change', () => { O.drafts[key] = sel.value; renderDrawerBody(true); });
        const apply = h('button', { class: 'btn', type: 'button', 'data-fk': 'mapply:' + role, disabled: draft === pickVal(now.seat, now.model) || !!O.busy[key],
          onclick: async () => {
            const p = parsePick(O.drafts[key] !== undefined ? O.drafts[key] : sel.value);
            O.busy[key] = true; apply.disabled = true; delete O.msg[key];
            const res = await api('/api/team/rooms/model', { id: r.id, role, seat: p.seat, model: p.model });
            O.busy[key] = false;
            if (res && res.ok) { delete O.drafts[key]; O.msg[key] = { ok: true, text: res.text || '已更新，下一回合生效' }; }
            else O.msg[key] = { ok: false, text: (res && res.text) || '沒有更新' }; // 錯誤留著，直到你再按一次
            renderDrawerBody(true);
          } }, '套用');
        row.append(h('div', { class: 'row' }, sel, apply));
      }
      row.append(out);
      return row;
    });
    const und = undetected();
    return [h('dt', {}, '模型'), h('dd', { class: 'o-mpick' }, rows,
      ended ? h('p', { class: 'hint' }, '這間房已經結束，不能再改模型。') : h('p', { class: 'hint' }, '改了之後下一回合生效；額度用完時仍會由 GLM 代班。'),
      !ended && O.modelsErr ? h('p', { class: 'o-err' }, O.modelsErr) : null,
      !ended && und.length ? h('p', { class: 'hint' }, `${und.join('、')} 還沒偵測到模型清單，只能選座位。`) : null)];
  }

  // ============================================================ 派工精靈
  function openWizard(opener) {
    O.wiz = { step: 0, path: '', projects: null, scan: null, source: 'goal', doc: '', goal: '', goalTouched: false, name: '',
      impl: 'auto', review: 'auto', implModel: '', reviewModel: '', autonomy: 'balanced', paths: '', err: '', busy: false };
    loadModels(true);
    openDrawer('wizard', null, opener);
    setTimeout(() => { const el = document.getElementById('o-path'); if (el) el.focus(); }, 0);
    api('/api/team/projects').then((r) => { if (O.wiz) { O.wiz.projects = (r && r.projects) || []; if (O.drawer && O.drawer.type === 'wizard' && O.wiz.step === 0) renderDrawerBody(true); } });
  }
  function wizGo(step) {
    O.wiz.step = step; O.wiz.err = '';
    renderDrawerHead(); renderDrawerBody(true);
    const first = O.drawer && O.drawer.body.querySelector('input, textarea, select, button.o-proj');
    if (first) first.focus();
  }
  function goalFor(w) {
    const s = w.scan || {};
    if (w.source === 'progress') return s.progress && s.progress.task ? s.progress.task : '';
    if (w.source === 'doc') {
      const d = (s.docs || []).find((x) => x.relpath === w.doc);
      return d ? `依照文件 ${d.relpath}（${d.title}）完成其中還沒完成的工作` : '';
    }
    return '';
  }
  async function scanPath() {
    const w = O.wiz;
    const el = document.getElementById('o-path');
    if (el && w.step === 0) w.path = el.value; // 以畫面上的輸入框為準（清單載入時輸入框可能剛被重畫）
    const path = w.path.trim();
    if (!path) { w.err = '請選一個專案或貼上資料夾的完整路徑'; renderDrawerBody(true); return; }
    w.busy = true; renderDrawerBody(true);
    const r = await api('/api/team/scan?path=' + encodeURIComponent(path));
    w.busy = false;
    if (!r || r.ok === false || r.problem === undefined) { w.err = (r && r.text) || '檢查不了這個資料夾'; renderDrawerBody(true); return; }
    w.scan = r;
    w.name = r.name || '';
    w.source = r.progress && r.progress.task ? 'progress' : 'goal';
    w.doc = r.docs && r.docs.length ? r.docs[0].relpath : '';
    if (!w.goalTouched) w.goal = goalFor(w);
    wizGo(1);
  }
  function wizardBody() {
    const w = O.wiz;
    const f = document.createDocumentFragment();
    const err = w.err ? h('p', { class: 'o-err', role: 'alert' }, w.err) : null;
    if (w.step === 0) {
      const input = h('input', { type: 'text', id: 'o-path', value: w.path, spellcheck: 'false', placeholder: '例如 D:\\Projects\\MyApp' });
      input.addEventListener('input', () => { w.path = input.value; });
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') scanPath(); });
      const list = w.projects === null ? h('p', { class: 'dim' }, '讀取專案清單中…')
        : w.projects.length ? h('div', { class: 'o-projs' }, w.projects.map((p) => h('button', {
          class: 'o-proj', type: 'button', 'aria-pressed': String(w.path === p.path), disabled: w.busy,
          onclick: () => { w.path = p.path; input.value = p.path; scanPath(); },
        }, h('b', {}, p.name), h('span', { class: 'loc' }, p.path))))
          : h('p', { class: 'dim' }, '還沒有用過的專案。把資料夾路徑貼到下面。');
      add(f, h('section', { class: 'sect' }, h('h3', {}, '最近的專案'), list),
        h('section', { class: 'sect' }, h('h3', {}, h('label', { for: 'o-path' }, '或貼上資料夾路徑'), h('small', {}, '要是 git repo，且至少有一次 commit')), input, err,
          h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', disabled: w.busy, onclick: scanPath }, w.busy ? '檢查中…' : '下一步：檢查資料夾'))));
      return f;
    }
    const s = w.scan;
    if (w.step === 1) {
      add(f, h('section', { class: 'sect' }, h('h3', {}, s.name), h('dl', { class: 'kv' },
        h('dt', {}, '路徑'), h('dd', {}, h('code', {}, s.path)),
        h('dt', {}, '分支'), h('dd', {}, s.branch || '—'),
        s.dirty && s.dirty.length ? [h('dt', {}, '未提交改動'), h('dd', { class: 'o-warn' }, `${s.dirty.length} 個檔案（不影響開工；合併前主 repo 要先乾淨）`)] : null)));
      if (s.problem) {
        add(f, h('div', { class: 'box pause' }, h('p', { class: 'o-err', role: 'alert' }, s.problem), h('p', { class: 'hint' }, '處理好再回來，或換一個資料夾。')),
          h('div', { class: 'row o-wnav' }, h('button', { class: 'btn', type: 'button', onclick: () => wizGo(0) }, '換一個資料夾'),
            h('button', { class: 'btn ghost', type: 'button', onclick: scanPath }, '重新檢查')));
        return f;
      }
      const ta = h('textarea', { id: 'o-goal', rows: 4, placeholder: '用一段話寫出要達成的成果，例如：在 calc.py 加入 mul(a, b)，含單元測試' }, w.goal);
      ta.addEventListener('input', () => { w.goal = ta.value; w.goalTouched = true; });
      const pick = (src) => { w.source = src; if (!w.goalTouched || !w.goal.trim()) { w.goal = goalFor(w); w.goalTouched = false; } renderDrawerBody(true); };
      const hasTask = s.progress && s.progress.task;
      const docs = s.docs || [];
      const docSel = h('select', { 'aria-label': '選一份文件', disabled: w.source !== 'doc' }, docs.map((d) => h('option', { value: d.relpath, selected: d.relpath === w.doc }, `${d.title}（${d.relpath}）`)));
      docSel.addEventListener('change', () => { w.doc = docSel.value; if (!w.goalTouched) w.goal = goalFor(w); ta.value = w.goal; });
      const radio = (val, label, disabled, extra) => h('label', { class: 'o-src' + (disabled ? ' dis' : '') },
        h('input', { type: 'radio', name: 'o-src', value: val, checked: w.source === val, disabled, onchange: () => pick(val) }),
        h('span', {}, h('b', {}, label), extra));
      const nameIn = h('input', { type: 'text', id: 'o-name', value: w.name });
      nameIn.addEventListener('input', () => { w.name = nameIn.value; });
      add(f, h('section', { class: 'sect' }, h('h3', {}, '工作來源'),
        h('div', { class: 'o-srcs', role: 'radiogroup', 'aria-label': '工作來源' },
          radio('progress', '接續 progress 的任務', !hasTask, hasTask ? h('span', { class: 'o-srcx' }, `${s.progress.file}：${s.progress.task}`) : h('span', { class: 'o-srcx' }, '沒找到 progress.md 的未完成任務')),
          radio('doc', '選一份文件', !docs.length, docs.length ? docSel : h('span', { class: 'o-srcx' }, '沒找到計畫文件或 Kiro spec')),
          radio('goal', '自己輸入目標', false, null))),
      h('section', { class: 'sect' }, h('h3', {}, h('label', { for: 'o-goal' }, '目標'), h('small', {}, '代理會以這段話為準')), ta),
      h('section', { class: 'sect' }, h('h3', {}, h('label', { for: 'o-name' }, '房間名稱')), nameIn), err,
      h('div', { class: 'row o-wnav' }, h('button', { class: 'btn', type: 'button', onclick: () => wizGo(0) }, '上一步'),
        h('button', { class: 'btn primary', type: 'button', onclick: () => {
          if (!w.goal.trim()) { w.err = '請寫下目標'; renderDrawerBody(true); return; }
          wizGo(2);
        } }, '下一步：陣容')));
      return f;
    }
    if (w.step === 2) {
      const same = w.impl !== 'auto' && w.impl === w.review;
      const pathsTa = h('textarea', { id: 'o-paths', rows: 4, spellcheck: 'false', placeholder: '每行一個 repo 內的相對路徑，例如\nsrc/\ntests/\n留白＝整個 repo' }, w.paths);
      pathsTa.addEventListener('input', () => { w.paths = pathsTa.value; });
      add(f, h('section', { class: 'sect' }, h('h3', {}, '陣容', h('small', {}, '每個角色選座位與模型，或交給 Decision-1')),
        modelPickerWizard(w),
        same ? h('p', { class: 'o-err' }, '實作與審查要用不同的座位') : h('p', { class: 'hint' }, '審查預設用跟實作不同家的代理，比較抓得到盲點。')),
      h('section', { class: 'sect' }, h('h3', {}, '自主程度'), h('div', { class: 'levels', role: 'group', 'aria-label': '自主程度' }, AUTONOMY.map(([k, label, desc]) => h('button', {
        class: 'level', type: 'button', 'aria-pressed': String(w.autonomy === k), onclick: () => { w.autonomy = k; renderDrawerBody(true); },
      }, h('b', {}, label), h('span', {}, desc))))),
      h('section', { class: 'sect' }, h('h3', {}, h('label', { for: 'o-paths' }, '可修改範圍'), h('small', {}, '危險指令一律交給你')), pathsTa), err,
      h('div', { class: 'row o-wnav' }, h('button', { class: 'btn', type: 'button', onclick: () => wizGo(1) }, '上一步'),
        h('button', { class: 'btn primary', type: 'button', disabled: same, onclick: () => wizGo(3) }, '下一步：確認')));
      return f;
    }
    // 步驟 4
    const who = (v, m) => (v === 'auto' ? autoPick() : pickLabel({ seat: v, model: m }));
    const paths = w.paths.split('\n').map((x) => x.trim()).filter(Boolean);
    add(f, h('section', { class: 'sect' }, h('h3', {}, '確認後開工'), h('dl', { class: 'kv' },
      h('dt', {}, '房間'), h('dd', {}, w.name || s.name),
      h('dt', {}, '專案'), h('dd', {}, h('code', {}, s.path)),
      h('dt', {}, '目標'), h('dd', {}, w.goal),
      h('dt', {}, '實作／審查'), h('dd', {}, `${who(w.impl, w.implModel)}／${who(w.review, w.reviewModel)}`),
      h('dt', {}, '自主程度'), h('dd', {}, AUTONOMY.find((a) => a[0] === w.autonomy)[1]),
      h('dt', {}, '可修改範圍'), h('dd', {}, paths.length ? paths.join('、') : '整個 repo')),
    h('p', { class: 'hint' }, `代理會在 ${s.name} 旁邊開一個獨立工作目錄（分支 ai-team/…），不會動到你正在用的資料夾；完成後等你按「合併」。`)),
    err,
    h('div', { class: 'row o-wnav' }, h('button', { class: 'btn', type: 'button', onclick: () => wizGo(2) }, '上一步'),
      h('button', { class: 'btn hot big', type: 'button', disabled: w.busy, onclick: submitWizard }, w.busy ? '派工中…' : '開工')));
    return f;
  }
  async function submitWizard() {
    const w = O.wiz;
    w.busy = true; w.err = ''; renderDrawerBody(true);
    const source = w.source === 'progress' ? `${w.scan.progress.file}：${w.scan.progress.task}` : w.source === 'doc' ? `文件：${w.doc}` : 'goal';
    const r = await api('/api/team/rooms', {
      repo: w.scan.path, goal: w.goal.trim(), source, impl_cli: w.impl, review_cli: w.review, autonomy: w.autonomy,
      impl_model: w.impl !== 'auto' ? w.implModel || '' : '', review_model: w.review !== 'auto' ? w.reviewModel || '' : '',
      allowed_paths: w.paths.split('\n').map((x) => x.trim()).filter(Boolean), name: w.name.trim(),
    });
    w.busy = false;
    if (!r.ok) { w.err = r.text || '派工沒有成功'; renderDrawerBody(true); return; }
    closeDrawer();
    toast(`已派工：${r.room.name}`, true);
    if (O.snap && !O.snap.rooms.some((x) => x.id === r.room.id)) O.snap.rooms.push(Object.assign({ events: [] }, r.room));
    jumpTo(r.room.id);
  }

  // ============================================================ 對外介面
  function commands() {
    const list = [{ name: '辦公室：派工', run: () => { if (typeof go === 'function') go('office'); openWizard(); } },
      { name: '辦公室：開啟收件匣', hint: `${count()} 件`, run: () => { if (typeof go === 'function') go('office'); openDrawer('inbox'); } },
      { name: '辦公室：座位', run: () => { if (typeof go === 'function') go('office'); openDrawer('seats'); } },
      { name: '辦公室：用量（模型呼叫與 token）', run: () => { if (typeof go === 'function') go('office'); openDrawer('usage'); } }];
    for (const r of rooms()) list.push({ name: `房間：${r.name}`, hint: STATUS[r.status], run: () => { if (typeof go === 'function') go('office'); jumpTo(r.id); openDrawer('room', r.id); } });
    return list;
  }
  window.Office = { render, onState, count, stats, commands, debug: (id) => ({ scene: id && roomById(id) ? sceneOf(roomById(id), Date.now()).screens : null, frames: O.frames, loop: O.root ? O.root.dataset.loop : '', moving: isMoving(),
    confetti: Object.values(O.confetti).some((t) => t > Date.now()), rooms: Object.keys(O.act).length }) };
}());
