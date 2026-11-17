// 像素辦公室的畫布美術：房間背景（離屏快取）、家具、像素人、螢幕、走廊。
// 這支檔案只負責「畫」，畫什麼由 office.js 依快照的真實欄位決定（scene）。在 office.js 之前載入。
'use strict';

(function () {
  const LW = 180; // 房間的邏輯像素寬
  const LH = 100; // 房間的邏輯像素高
  const CH = 30; // 走廊高

  // ---- 房內的固定位置（腳底座標）。office.js 的動作對應只用這些名字。
  const SPOT = {
    implSeat: { x: 45, y: 58, pose: 'sit' },
    revSeat: { x: 127, y: 58, pose: 'sit' },
    implStand: { x: 45, y: 59, pose: 'hand' },
    revStand: { x: 127, y: 59, pose: 'hand' },
    board: { x: 82, y: 45, pose: 'write' }, // 白板前寫字（背對）
    boardSide: { x: 99, y: 47, pose: 'stand' }, // 白板旁
    implSide: { x: 74, y: 60, pose: 'stand' }, // 實作桌邊（Reviewer 走過來看）
    test: { x: 29, y: 96, pose: 'stand' }, // 測試機旁
    sofaA: { x: 77, y: 93, pose: 'sofa' },
    sofaB: { x: 95, y: 93, pose: 'sofa' },
    coffeeA: { x: 159, y: 52, pose: 'mug' },
    coffeeB: { x: 168, y: 66, pose: 'mug' },
    door: { x: 172, y: 70, pose: 'walk' },
  };
  // 走路時先走到這條走道（避開桌子）
  const AISLE_Y = 70;

  const C = {
    skin: ['#f1c7a5', '#e0ac85', '#c98f68', '#a8724f'],
    eye: '#1a1f26', dark: '#141a21', pants: '#2b3340', shoe: '#11161c', white: '#f4f7fa',
    screenOff: '#0d1319', bezel: '#1d2633', heat: '#f08a24', red: '#e5534b', ok: '#57c27a', warn: '#e3b341',
  };
  const HAIR = ['#2b2118', '#4a3424', '#1c1c22', '#6b4a2e', '#8a5a2b', '#3a2a20', '#b8b0a2'];
  const STYLES = ['short', 'spiky', 'bob', 'bun', 'cap', 'short'];

  // 每間房依 id 雜湊選配色與擺設（固定）
  const FLOORS = [['#6f4c30', '#68462c', '#583922'], ['#7a5b3c', '#715335', '#5d4229'], ['#5d4a3a', '#564436', '#45372b'],
    ['#806247', '#775a40', '#634a33']];
  const WALLS = [['#2b3145', '#4c3525', '#6d4c36'], ['#2c3a3d', '#3f3426', '#5c4a36'], ['#34303f', '#4a3a2a', '#6a523b'],
    ['#2a3340', '#3d4652', '#56606c']];
  const RUGS = [['#233149', '#324566', '#4a6591'], ['#2c2742', '#3f385c', '#5e5385'], ['#243c35', '#34554b', '#4d7c6e'],
    ['#3b2f2f', '#524141', '#735a5a']];
  const SOFAS = [['#7f1d1d', '#991b1b', '#c2410c'], ['#1e3a5f', '#24507f', '#3b74b0'], ['#3f5a2a', '#4f7034', '#6b9447'],
    ['#5b3a6b', '#6d4880', '#8b62a3']];
  const BOOKS = ['#ef4444', '#3b82f6', '#10b981', '#f59e0b', '#a855f7', '#e2e8f0'];

  function hash(s) { let v = 7; for (const ch of String(s)) v = (v * 31 + ch.charCodeAt(0)) >>> 0; return v; }
  function px(ctx, x, y, w, h, col) { ctx.fillStyle = col; ctx.fillRect(x, y, w, h); }
  // 把雜湊的高位元打散（相近的 id 也會拿到不同的配色）
  function mix(v) { v ^= v >>> 16; v = Math.imul(v, 0x45d9f3b) >>> 0; v ^= v >>> 16; v = Math.imul(v, 0x45d9f3b) >>> 0; return (v ^ (v >>> 16)) >>> 0; }
  function themeOf(id) {
    const v = mix(hash(id));
    return {
      floor: FLOORS[v % FLOORS.length], wall: WALLS[(v >>> 3) % WALLS.length], rug: RUGS[(v >>> 5) % RUGS.length],
      sofa: SOFAS[(v >>> 7) % SOFAS.length], books: (v >>> 9) % BOOKS.length, plant: (v >>> 11) % 2,
      poster: (v >>> 12) % 3, cooler: (v >>> 14) % 2 === 0,
    };
  }
  function lookOf(key) {
    const v = mix(hash(key));
    return { hair: HAIR[v % HAIR.length], style: STYLES[(v >>> 4) % STYLES.length], skin: C.skin[(v >>> 7) % C.skin.length] };
  }
  const isNight = (d) => { const hh = (d || new Date()).getHours(); return hh < 6 || hh >= 18; };

  // ============================================================ 靜態背景（離屏，1 邏輯像素＝1 實際像素）
  const cache = new Map();
  function background(id, night) {
    const key = id + '|' + (night ? 'n' : 'd');
    let c = cache.get(key);
    if (c) return c;
    c = document.createElement('canvas');
    c.width = LW; c.height = LH;
    paintBackground(c.getContext('2d'), themeOf(id), night);
    if (cache.size > 64) cache.clear();
    cache.set(key, c);
    return c;
  }
  function paintBackground(ctx, th, night) {
    const [fa, fb, seam] = th.floor;
    // 木地板
    for (let y = 31; y < LH; y += 6) {
      for (let x = 0; x < LW; x += 16) {
        const off = ((y / 6) % 2) * 8;
        px(ctx, x - off, y, 16, 6, ((x + y) / 2) % 3 ? fa : fb);
        px(ctx, x - off, y + 5, 16, 1, seam);
        px(ctx, x - off + 15, y, 1, 5, seam);
      }
    }
    // 牆、護牆板
    const [wall, wains, wainsHi] = th.wall;
    px(ctx, 0, 0, LW, 31, wall);
    px(ctx, 0, 20, LW, 10, wains);
    px(ctx, 0, 20, LW, 1, wainsHi);
    for (let x = 6; x < LW; x += 12) px(ctx, x, 22, 1, 7, 'rgba(0,0,0,.18)');
    px(ctx, 0, 30, LW, 1, '#1a1512');
    px(ctx, 0, 31, LW, 2, 'rgba(0,0,0,.25)');
    // 地毯（沙發區、桌區）
    rug(ctx, 62, 78, 48, 20, th.rug);
    rug(ctx, 20, 50, 52, 14, [th.rug[0], th.rug[1], th.rug[1]]);
    rug(ctx, 102, 50, 52, 14, [th.rug[0], th.rug[1], th.rug[1]]);
    // 窗戶：白天日光、晚上星空
    windowArt(ctx, 26, 4, 28, 15, night);
    // 書櫃
    bookshelf(ctx, 4, 7, th.books);
    // 白板框（內容是動態資料，另外畫）
    px(ctx, 61, 2, 54, 23, '#475569');
    px(ctx, 62, 3, 52, 21, '#e8edf2');
    px(ctx, 66, 24, 44, 2, '#64748b'); // 筆槽
    px(ctx, 70, 24, 4, 1, '#3b82f6'); px(ctx, 76, 24, 4, 1, '#ef4444');
    // 時鐘框
    px(ctx, 119, 5, 12, 12, '#334155'); px(ctx, 120, 6, 10, 10, '#f8fafc');
    // 海報或公告
    if (th.poster === 1) { px(ctx, 135, 6, 10, 13, '#1e293b'); px(ctx, 136, 7, 8, 11, '#f59e0b'); px(ctx, 137, 9, 6, 3, '#fef3c7'); px(ctx, 137, 14, 6, 1, '#7c2d12'); }
    else if (th.poster === 2) { px(ctx, 135, 7, 11, 9, '#0f172a'); px(ctx, 136, 8, 9, 7, '#38bdf8'); px(ctx, 138, 11, 5, 3, '#0ea5e9'); px(ctx, 139, 9, 2, 2, '#fde68a'); }
    // 咖啡吧檯
    px(ctx, 150, 27, 27, 12, '#473022');
    px(ctx, 150, 27, 27, 3, '#7c553c');
    px(ctx, 150, 39, 27, 1, 'rgba(0,0,0,.35)');
    px(ctx, 154, 15, 10, 12, '#475569'); px(ctx, 155, 16, 8, 6, '#94a3b8'); px(ctx, 157, 23, 4, 3, '#0f172a');
    px(ctx, 168, 24, 3, 3, '#f8fafc'); px(ctx, 172, 23, 4, 4, '#f59e0b');
    // 門（右牆）
    px(ctx, 176, 50, 4, 26, '#3b2a1e'); px(ctx, 177, 52, 3, 22, '#0f141a'); px(ctx, 166, 74, 10, 2, 'rgba(0,0,0,.25)');
    px(ctx, 168, 71, 8, 4, '#3f3a52');
    // 側牆、底
    px(ctx, 0, 31, 2, LH - 31, '#181b26'); px(ctx, LW - 2, 31, 2, 19, '#181b26'); px(ctx, LW - 2, 76, 2, LH - 76, '#181b26');
    px(ctx, 0, LH - 2, LW, 2, '#181b26');
    // 桌子
    desk(ctx, 22, th);
    desk(ctx, 104, th);
    // 測試機（左下）
    px(ctx, 4, 86, 20, 4, '#5d4029'); px(ctx, 4, 86, 20, 1, '#8a603e'); px(ctx, 5, 90, 2, 6, '#292d3d'); px(ctx, 21, 90, 2, 6, '#292d3d');
    px(ctx, 6, 73, 14, 13, '#334155'); px(ctx, 7, 74, 12, 9, C.screenOff); px(ctx, 11, 84, 4, 2, '#1e293b');
    px(ctx, 18, 88, 4, 1, '#94a3b8');
    // 沙發
    sofa(ctx, 64, 80, th.sofa);
    // 植物
    plant(ctx, 164, 82, th.plant);
    if (th.cooler) { px(ctx, 151, 82, 8, 13, '#cbd5e1'); px(ctx, 152, 76, 6, 6, '#38bdf8'); px(ctx, 153, 77, 2, 4, '#bae6fd'); px(ctx, 153, 87, 1, 2, '#2563eb'); px(ctx, 156, 87, 1, 2, '#ef4444'); }
    else plant(ctx, 150, 84, 1 - th.plant);
  }
  function rug(ctx, x, y, w, h, [base, border, accent]) {
    px(ctx, x, y, w, h, border);
    px(ctx, x + 2, y + 2, w - 4, h - 4, base);
    px(ctx, x + 4, y + 4, 3, 1, accent); px(ctx, x + 4, y + 4, 1, 3, accent);
    px(ctx, x + w - 7, y + h - 5, 3, 1, accent); px(ctx, x + w - 5, y + h - 7, 1, 3, accent);
  }
  function windowArt(ctx, x, y, w, h, night) {
    px(ctx, x - 1, y - 1, w + 2, h + 2, '#151821');
    if (night) {
      px(ctx, x, y, w, h, '#172542'); px(ctx, x, y + 9, w, 6, '#22355c');
      px(ctx, x + 3, y + 8, 5, 7, '#10182b'); px(ctx, x + 10, y + 6, 6, 9, '#0d1424'); px(ctx, x + 19, y + 9, 6, 6, '#10182b');
      px(ctx, x + 12, y + 8, 1, 1, '#fbbf24'); px(ctx, x + 14, y + 11, 1, 1, '#fbbf24'); px(ctx, x + 21, y + 11, 1, 1, '#38bdf8');
      px(ctx, x + 22, y + 2, 2, 2, '#f8fafc'); px(ctx, x + 23, y + 2, 1, 1, '#172542'); // 月亮
    } else {
      px(ctx, x, y, w, h, '#7cc4ef'); px(ctx, x, y + 10, w, 5, '#a7dbf6');
      px(ctx, x + 4, y + 3, 3, 3, '#fde68a'); // 太陽
      px(ctx, x + 14, y + 4, 8, 2, '#f8fafc'); px(ctx, x + 16, y + 3, 4, 1, '#f8fafc');
      px(ctx, x + 2, y + 11, 6, 4, '#4f7d5b'); px(ctx, x + 18, y + 10, 8, 5, '#5c8f68');
    }
    px(ctx, x + w / 2 - 1, y, 2, h, '#333b52');
    px(ctx, x, y + h - 1, w, 2, '#505b7a');
  }
  function bookshelf(ctx, x, y, start) {
    px(ctx, x, y, 16, 34, '#5c3c24');
    px(ctx, x + 1, y + 2, 14, 30, '#3b2516');
    for (let row = 0; row < 4; row++) {
      px(ctx, x + 1, y + 9 + row * 8, 14, 1, '#5c3c24');
      for (let b = 0; b < 5; b++) {
        const tall = (row + b + start) % 3 ? 6 : 5;
        px(ctx, x + 2 + b * 2.6, y + 9 + row * 8 - tall, 2, tall, BOOKS[(row * 2 + b + start) % BOOKS.length]);
      }
    }
    px(ctx, x, y + 34, 16, 2, 'rgba(0,0,0,.3)');
  }
  // 雙螢幕桌：x 為桌子左緣（寬 46）
  function desk(ctx, x, th) {
    px(ctx, x + 1, y0(48), 46, 3, 'rgba(0,0,0,.28)');
    px(ctx, x + 2, 44, 3, 6, '#292d3d'); px(ctx, x + 41, 44, 3, 6, '#292d3d');
    px(ctx, x, 36, 46, 9, '#5d4029');
    px(ctx, x + 1, 37, 44, 6, '#8a603e');
    px(ctx, x, 36, 46, 1, '#a07450');
    // 螢幕框（畫面是動態的）
    px(ctx, x + 4, 23, 19, 13, C.bezel); px(ctx, x + 12, 35, 3, 2, '#334155');
    px(ctx, x + 26, 25, 15, 11, C.bezel); px(ctx, x + 32, 35, 3, 2, '#334155');
    // 鍵盤、滑鼠、馬克杯
    px(ctx, x + 14, 39, 12, 2, '#cbd5e1'); px(ctx, x + 28, 39, 2, 2, '#94a3b8');
    px(ctx, x + 38, 38, 3, 3, (hash(th.floor[0] + x) % 2) ? '#f8fafc' : '#fca5a5');
  }
  function y0(v) { return v; }
  function sofa(ctx, x, y, [dark, mid, cushion]) {
    px(ctx, x, y + 1, 44, 15, dark);
    px(ctx, x + 2, y + 2, 40, 6, mid);
    px(ctx, x + 3, y + 8, 18, 6, cushion); px(ctx, x + 23, y + 8, 18, 6, cushion);
    px(ctx, x + 21, y + 8, 2, 6, dark);
    px(ctx, x, y + 4, 3, 11, mid); px(ctx, x + 41, y + 4, 3, 11, mid);
    px(ctx, x + 5, y + 4, 4, 3, '#fbbf24');
    px(ctx, x + 2, y + 16, 40, 2, 'rgba(0,0,0,.3)');
  }
  function plant(ctx, x, y, kind) {
    px(ctx, x + 1, y + 10, 8, 6, '#b45309'); px(ctx, x, y + 9, 10, 2, '#d97706');
    if (kind) {
      px(ctx, x + 1, y + 2, 4, 7, '#15803d'); px(ctx, x + 5, y, 4, 9, '#16a34a'); px(ctx, x + 3, y - 2, 3, 5, '#22c55e'); px(ctx, x + 6, y + 1, 1, 2, '#4ade80');
    } else {
      px(ctx, x + 4, y - 3, 2, 12, '#3f8f5a'); px(ctx, x, y + 1, 4, 3, '#22c55e'); px(ctx, x + 6, y - 1, 4, 3, '#16a34a'); px(ctx, x + 1, y + 5, 3, 2, '#4ade80');
    }
  }

  // ============================================================ 動態部分
  // 螢幕畫面：kind＝off | code | doc | diff | wait | err | ok | idle
  function screen(ctx, x, y, w, h, kind, t) {
    if (kind === 'off') { px(ctx, x, y, w, h, C.screenOff); px(ctx, x + w - 2, y + h - 2, 1, 1, '#334155'); return; }
    if (kind === 'idle') { px(ctx, x, y, w, h, '#16213a'); px(ctx, x + 2, y + 2, w - 4, 1, '#334866'); px(ctx, x + 2, y + 4, w - 7, 1, '#334866'); return; }
    if (kind === 'wait' || kind === 'err') {
      px(ctx, x, y, w, h, kind === 'err' ? '#450a0a' : '#451a03');
      const mid = x + Math.floor(w / 2) - 1;
      px(ctx, mid, y + 2, 2, h - 6, kind === 'err' ? C.red : C.heat);
      px(ctx, mid, y + h - 3, 2, 2, kind === 'err' ? C.red : C.heat);
      return;
    }
    if (kind === 'ok') {
      px(ctx, x, y, w, h, '#052e16');
      const cx = x + Math.floor(w / 2); const cy = y + Math.floor(h / 2);
      px(ctx, cx - 3, cy, 1, 1, '#4ade80'); px(ctx, cx - 2, cy + 1, 1, 1, '#4ade80'); px(ctx, cx - 1, cy + 2, 1, 1, '#4ade80');
      px(ctx, cx, cy + 1, 1, 1, '#4ade80'); px(ctx, cx + 1, cy, 1, 1, '#4ade80'); px(ctx, cx + 2, cy - 1, 1, 1, '#4ade80'); px(ctx, cx + 3, cy - 2, 1, 1, '#4ade80');
      return;
    }
    if (kind === 'diff') {
      px(ctx, x, y, w, h, '#0f172a');
      const s = Math.floor(t / 400) % 4;
      for (let r = 0; r < h - 1; r += 2) {
        const k = (r / 2 + s) % 4;
        px(ctx, x + 1, y + 1 + r, 1, 1, k === 1 ? '#ef4444' : k === 3 ? '#22c55e' : '#475569');
        px(ctx, x + 3, y + 1 + r, Math.max(2, (w - 5) - ((r * 3 + s) % 5)), 1, k === 1 ? '#7f1d1d' : k === 3 ? '#14532d' : '#334155');
      }
      return;
    }
    // code / doc：捲動的程式碼或文件
    const doc = kind === 'doc';
    px(ctx, x, y, w, h, doc ? '#e2e8f0' : '#0f172a');
    if (!doc) px(ctx, x, y, 2, h, '#1e293b');
    const scroll = Math.floor(t / 180);
    const cols = doc ? ['#475569', '#64748b', '#3b82f6'] : ['#38bdf8', '#34d399', '#fbbf24', '#c084fc'];
    for (let r = 0; r < h - 1; r += 2) {
      const len = 3 + (((r + scroll) * 7) % Math.max(3, w - 6));
      px(ctx, x + (doc ? 1 : 3) + (((r + scroll) * 5) % 3 === 0 && !doc ? 2 : 0), y + 1 + r, Math.min(len, w - 4), 1, cols[(r / 2 + scroll) % cols.length]);
    }
  }
  function drawScreens(ctx, sc, t) {
    for (const [dx, k] of [[22, sc.impl], [104, sc.rev]]) {
      screen(ctx, dx + 5, 24, 17, 11, k || 'off', t);
      screen(ctx, dx + 27, 26, 13, 9, k === 'code' || k === 'diff' ? (k === 'code' ? 'doc' : 'code') : (k || 'off'), t + 700);
    }
    // 測試機：綠／紅／跑中／關
    const tk = sc.test || 'off';
    if (tk === 'green' || tk === 'red') {
      px(ctx, 7, 74, 12, 9, tk === 'green' ? '#14532d' : '#7f1d1d');
      const col = tk === 'green' ? '#4ade80' : '#fca5a5';
      if (tk === 'green') { px(ctx, 9, 79, 1, 1, col); px(ctx, 10, 80, 1, 1, col); px(ctx, 11, 79, 1, 1, col); px(ctx, 12, 78, 1, 1, col); px(ctx, 13, 77, 1, 1, col); }
      else { for (let i = 0; i < 5; i++) { px(ctx, 10 + i, 76 + i, 1, 1, col); px(ctx, 14 - i, 76 + i, 1, 1, col); } }
    } else if (tk === 'run') {
      px(ctx, 7, 74, 12, 9, '#0f172a');
      const n = Math.floor(t / 220) % 5;
      for (let i = 0; i < 4; i++) px(ctx, 8, 75 + i * 2, i <= n ? 10 - i * 2 : 2, 1, i === 3 ? '#fbbf24' : '#4ade80');
    } else screen(ctx, 7, 74, 12, 9, 'off', t);
  }
  // 白板：驗收條件清單（顏色＝目前的誠實狀態）、便利貼＝實際 commit 數
  function drawBoard(ctx, board, t) {
    if (!board) return;
    const crit = board.crit || [];
    const col = { ok: '#16a34a', run: '#d97706', stop: '#dc2626', manual: '#ca8a04', todo: '#94a3b8' };
    crit.slice(0, 6).forEach((c, i) => {
      const y = 5 + i * 3;
      px(ctx, 64, y, 2, 2, col[c] || col.todo);
      px(ctx, 67, y, 12 + ((i * 7) % 14), 1, '#475569');
    });
    if (crit.length > 6) px(ctx, 64, 23, 6, 1, '#94a3b8');
    const notes = Math.min(board.commits || 0, 8);
    const nc = ['#fbbf24', '#34d399', '#fb7185', '#60a5fa'];
    for (let i = 0; i < notes; i++) {
      const nx = 97 + (i % 2) * 8; const ny = 4 + Math.floor(i / 2) * 5;
      px(ctx, nx, ny, 6, 4, nc[i % nc.length]); px(ctx, nx + 1, ny + 1, 3, 1, 'rgba(0,0,0,.25)');
    }
    if (board.writing) { const k = Math.floor(t / 300) % 3; px(ctx, 80 + k * 3, 16, 2, 1, '#1d4ed8'); }
  }
  function drawClock(ctx, now) {
    const d = new Date(now);
    const cx = 125; const cy = 11;
    const hAng = ((d.getHours() % 12) + d.getMinutes() / 60) / 12 * Math.PI * 2;
    const mAng = d.getMinutes() / 60 * Math.PI * 2;
    for (let i = 1; i <= 3; i++) px(ctx, Math.round(cx + Math.sin(mAng) * i), Math.round(cy - Math.cos(mAng) * i), 1, 1, '#0f172a');
    for (let i = 1; i <= 2; i++) px(ctx, Math.round(cx + Math.sin(hAng) * i), Math.round(cy - Math.cos(hAng) * i), 1, 1, '#ef4444');
    px(ctx, cx, cy, 1, 1, '#0f172a');
  }
  function drawSteam(ctx, t) {
    const k = Math.floor(t / 250) % 4;
    px(ctx, 169, 20 - k, 1, 2, 'rgba(248,250,252,.55)');
    px(ctx, 158, 12 - ((k + 2) % 4), 1, 2, 'rgba(248,250,252,.45)');
  }
  function drawStars(ctx, t) {
    const on = Math.floor(t / 900) % 2;
    px(ctx, 29, 6, 1, 1, on ? '#fef08a' : '#64748b');
    px(ctx, 47, 5, 1, 1, on ? '#64748b' : '#e2e8f0');
    px(ctx, 36, 8, 1, 1, '#cbd5e1');
  }

  // ---- 像素人（12×18）：x＝中心、y＝腳底
  // pose：stand | walk | write（背對舉手寫字）| sit（背對坐著）| type | head（抱頭）| hand（舉手）| sofa | mug | cheer | carry
  function person(ctx, p, t) {
    const x = Math.round(p.x) - 6;
    const pose = p.pose;
    const seated = pose === 'sit' || pose === 'type' || pose === 'head';
    const back = seated || pose === 'write';
    let y = Math.round(p.y) - 18;
    if (pose === 'sofa') y += 3;
    let bob = 0;
    if (pose === 'walk' || pose === 'carry') bob = Math.floor(t / 110) % 2 ? -1 : 0;
    else if (pose === 'cheer') bob = Math.floor(t / 160) % 2 ? -2 : 0;
    y += bob;
    const sh = p.shirt; const skin = p.look.skin; const hc = p.look.hair;
    // 影子
    if (!seated) px(ctx, x + 2, Math.round(p.y) - 1, 9, 2, 'rgba(0,0,0,.3)');
    // 腿
    if (pose === 'sofa') {
      px(ctx, x + 2, y + 13, 8, 2, C.pants); px(ctx, x + 2, y + 15, 2, 1, C.shoe); px(ctx, x + 8, y + 15, 2, 1, C.shoe);
    } else if (!seated) {
      const stride = (pose === 'walk' || pose === 'carry') ? (Math.floor(t / 110) % 2 ? 1 : -1) : 0;
      px(ctx, x + 3, y + 14 + (stride > 0 ? -1 : 0), 2, 3, C.pants); px(ctx, x + 7, y + 14 + (stride < 0 ? -1 : 0), 2, 3, C.pants);
      px(ctx, x + 3, y + 17 + (stride > 0 ? -1 : 0), 2, 1, C.shoe); px(ctx, x + 7, y + 17 + (stride < 0 ? -1 : 0), 2, 1, C.shoe);
    }
    // 身體
    px(ctx, x + 2, y + 8, 8, 6, sh);
    px(ctx, x + 2, y + 12, 8, 2, 'rgba(0,0,0,.2)');
    // 手臂
    const arm = (ax, ay, w, hh) => px(ctx, x + ax, y + ay, w, hh, sh);
    const hand = (ax, ay) => px(ctx, x + ax, y + ay, 2, 2, skin);
    if (pose === 'type') {
      const l = Math.floor(t / 90) % 2; const r = Math.floor(t / 120) % 2;
      arm(0, 8, 2, 3); arm(10, 8, 2, 3); hand(0, 6 - l); hand(10, 6 - r);
    } else if (pose === 'sit') {
      arm(0, 8, 2, 3); arm(10, 8, 2, 3); hand(1, 7); hand(9, 7);
    } else if (pose === 'head') {
      arm(0, 3, 2, 6); arm(10, 3, 2, 6); hand(1, 1); hand(9, 1);
    } else if (pose === 'write') {
      const k = Math.floor(t / 200) % 2;
      arm(10, 2 + k, 2, 7); hand(10, 0 + k); arm(0, 9, 2, 4);
      px(ctx, x + 11, y - 1 + k, 1, 2, '#1d4ed8');
    } else if (pose === 'hand') {
      arm(0, 9, 2, 4); hand(0, 13);
      arm(10, 1, 2, 8); hand(10, -1);
    } else if (pose === 'cheer') {
      arm(0, 2, 2, 7); arm(10, 2, 2, 7); hand(0, 0); hand(10, 0);
    } else if (pose === 'mug' || pose === 'sofa') {
      arm(0, 9, 2, 4); arm(9, 9, 3, 3);
      px(ctx, x + 11, y + 9, 3, 3, '#f8fafc'); px(ctx, x + 12, y + 9, 1, 1, '#78350f');
    } else if (pose === 'carry') {
      arm(0, 9, 2, 4); arm(9, 9, 2, 3);
      px(ctx, x + 8, y + 9, 6, 5, '#f59e0b'); px(ctx, x + 8, y + 9, 6, 1, '#b45309');
    } else {
      arm(0, 9, 2, 4); arm(10, 9, 2, 4); hand(0, 13); hand(10, 13);
    }
    // 頭
    px(ctx, x + 2, y + 1, 8, 7, skin);
    const st = p.look.style;
    if (st === 'cap') { px(ctx, x + 2, y, 8, 3, p.capColor || '#dc2626'); px(ctx, x + (back ? 2 : 1), y + 2, back ? 8 : 10, 1, '#7f1d1d'); }
    else if (st === 'spiky') { px(ctx, x + 2, y, 8, 3, hc); px(ctx, x + 3, y - 1, 2, 1, hc); px(ctx, x + 7, y - 1, 2, 1, hc); }
    else if (st === 'bob') { px(ctx, x + 1, y, 10, 3, hc); px(ctx, x + 1, y + 3, 1, 4, hc); px(ctx, x + 10, y + 3, 1, 4, hc); }
    else if (st === 'bun') { px(ctx, x + 2, y, 8, 3, hc); px(ctx, x + 4, y - 2, 4, 2, hc); }
    else px(ctx, x + 2, y, 8, 3, hc);
    if (back) px(ctx, x + 2, y + 2, 8, 5, st === 'cap' ? hc : hc);
    else {
      px(ctx, x + 4, y + 4, 1, 2, C.eye); px(ctx, x + 7, y + 4, 1, 2, C.eye);
      if (pose === 'cheer') px(ctx, x + 5, y + 6, 2, 1, '#7f1d1d');
    }
    // 椅背（坐著時蓋住下半身）
    if (seated) {
      px(ctx, x + 1, y + 11, 10, 6, '#1e293b'); px(ctx, x + 2, y + 12, 8, 4, '#334155');
      px(ctx, x + 5, y + 17, 2, 2, '#1e293b'); px(ctx, x + 2, y + 19, 8, 1, '#1e293b');
    }
  }
  // 空椅子（人不在座位時）
  function chair(ctx, cx, cy) {
    const x = cx - 6; const y = cy - 18;
    px(ctx, x + 1, y + 9, 10, 8, '#1e293b'); px(ctx, x + 2, y + 10, 8, 6, '#334155');
    px(ctx, x + 5, y + 17, 2, 2, '#1e293b'); px(ctx, x + 2, y + 19, 8, 1, '#1e293b');
  }
  // 頭上的記號：! 紅／橘框
  function mark(ctx, x, y, kind, t, motion) {
    const up = motion && Math.floor(t / 300) % 2 ? -1 : 0;
    const fill = kind === 'err' ? C.red : C.heat;
    const bx = Math.round(x) - 3; const by = Math.round(y) - 9 + up;
    px(ctx, bx - 1, by - 1, 8, 9, C.dark); px(ctx, bx, by, 6, 7, fill);
    px(ctx, bx + 2, by + 1, 2, 3, '#fff'); px(ctx, bx + 2, by + 5, 2, 1, '#fff');
  }
  // 房間頭上的大「!」（等你）
  function roomBang(ctx, t, motion) {
    const up = motion && Math.floor(t / 400) % 2 ? -1 : 0;
    px(ctx, 136, 1 + up, 12, 15, C.dark); px(ctx, 137, 2 + up, 10, 13, C.heat);
    px(ctx, 141, 4 + up, 2, 6, '#fff'); px(ctx, 141, 11 + up, 2, 2, '#fff');
  }
  function confetti(ctx, id, t) {
    const cols = [C.heat, C.ok, '#7aa7d9', '#a77bf3', C.warn, '#3fc8e0'];
    for (let i = 0; i < 46; i++) {
      const sx = hash(id + i) % LW;
      const sy = ((hash(i + id) % LH) + Math.floor(t / 40) * (1 + (i % 3))) % LH;
      px(ctx, sx, sy, 2, 1 + (i % 2), cols[i % cols.length]);
    }
  }

  // 一間房的完整畫面。scene 由 office.js 算好（全部是真實欄位推出來的）
  function drawRoom(ctx, room, scene, actors, now, motion) {
    const t = motion ? now : 0;
    const night = isNight(new Date(now));
    ctx.drawImage(background(room.id, night), 0, 0);
    if (night && motion) drawStars(ctx, t);
    drawClock(ctx, now);
    drawBoard(ctx, scene.board, t);
    drawScreens(ctx, scene.screens, t);
    if (scene.lit && motion) drawSteam(ctx, t);
    // 空椅子
    const seated = new Set(actors.filter((a) => a.pose === 'sit' || a.pose === 'type' || a.pose === 'head').map((a) => Math.round(a.x)));
    if (!seated.has(SPOT.implSeat.x)) chair(ctx, SPOT.implSeat.x, SPOT.implSeat.y);
    if (!seated.has(SPOT.revSeat.x)) chair(ctx, SPOT.revSeat.x, SPOT.revSeat.y);
    // 由後往前畫（y 小的先畫）
    const list = actors.slice().sort((a, b) => a.y - b.y);
    for (const a of list) person(ctx, a, t + (hash(a.key) % 500));
    for (const a of list) if (a.mark) mark(ctx, a.x + 7, a.y - 18, a.mark, t, motion);
    if (!scene.lit) { ctx.fillStyle = 'rgba(4, 8, 12, .55)'; ctx.fillRect(0, 0, LW, LH); }
    if (scene.bang) roomBang(ctx, t, motion);
    if (scene.confetti && motion) confetti(ctx, room.id, t);
    if (scene.frame) {
      ctx.fillStyle = scene.frame;
      ctx.fillRect(0, 0, LW, 2); ctx.fillRect(0, LH - 2, LW, 2); ctx.fillRect(0, 0, 2, LH); ctx.fillRect(LW - 2, 0, 2, LH);
    }
  }
  // 空房間：沒有人的同一間辦公室（燈半暗）
  function drawEmpty(ctx, key, now) {
    ctx.drawImage(background('empty:' + key, isNight(new Date(now))), 0, 0);
    ctx.fillStyle = 'rgba(8, 12, 18, .5)'; ctx.fillRect(0, 0, LW, LH);
  }
  function drawCorridor(ctx, w, people, now, motion) {
    const t = motion ? now : 0;
    for (let x = 0; x < w; x += 8) {
      px(ctx, x, 0, 8, CH, (x / 8) % 2 ? '#2a2f3d' : '#2d3241');
      px(ctx, x, 0, 8, 2, '#181b26');
    }
    for (let x = 6; x < w; x += 24) px(ctx, x, CH - 4, 12, 1, '#363c4d');
    for (const p of people) person(ctx, p, t + (hash(p.key) % 500));
  }

  // 房間不在了：丟掉它的背景快取
  function forget(id) { for (const k of [...cache.keys()]) if (k.startsWith(id + '|')) cache.delete(k); }

  window.OfficeScene = { LW, LH, CH, SPOT, AISLE_Y, hash, themeOf, lookOf, isNight, drawRoom, drawEmpty, drawCorridor, forget };
}());
