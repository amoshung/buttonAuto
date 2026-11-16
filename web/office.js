"use strict";
// 像素辦公室（E6）：純裝飾。每個工作階段是一位同事，坐在自己的座位上；動作反映代理的狀態。
// 全部用 canvas 畫方塊，不載入外部圖片。app.js 每次收到新資料會呼叫 Office.update(agents)。
(function () {
  const SEAT_W = 80, SEAT_H = 104, WALL_H = 26;
  const SHIRT = { Kiro: "#8B5CF6", Antigravity: "#F97316" };
  const HAIR = ["#3F2A1D", "#1F2937", "#7C2D12", "#A16207", "#4B5563"];
  const MODE_COLOR = { auto: "#10B981", suggest: "#38BDF8", off: "#64748B" };
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let agents = [];
  let canvas, ctx, wrap, scale = 3, cols = 1, frame = 0, seats = [];

  function hash(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0; return Math.abs(h); }
  function px(x, y, w, h, c) { ctx.fillStyle = c; ctx.fillRect(x, y, w, h); }

  function pose(a) {
    if (a.mode === "off") return "sleep";
    if (a.needs_you.length || a.paused || a.agent_state === "waiting_approval" || a.agent_state === "asking_choice") return "raise";
    return { working: "type", done: "cheer", waiting_input: "talk", error: "error", idle: "sleep" }[a.agent_state] || "sit";
  }

  function drawRoom(w, h) {
    // 牆、窗戶（依現在時間畫白天或夜晚）、地板
    px(0, 0, w, WALL_H, "#334155");
    px(0, WALL_H - 2, w, 2, "#1E293B");
    const hr = new Date().getHours();
    const sky = hr >= 6 && hr < 17 ? "#7DD3FC" : (hr >= 17 && hr < 19 ? "#FDBA74" : "#1E3A8A");
    for (let x = 12; x + 30 < w; x += 80) {
      px(x, 5, 30, 14, "#CBD5E1");
      px(x + 1, 6, 28, 12, sky);
      px(x + 15, 6, 1, 12, "#CBD5E1");
      if (sky === "#1E3A8A") { px(x + 5, 9, 1, 1, "#FEF3C7"); px(x + 22, 12, 1, 1, "#FEF3C7"); }
    }
    for (let y = WALL_H; y < h; y += 8)
      for (let x = 0; x < w; x += 8)
        px(x, y, 8, 8, ((x + y) / 8) % 2 ? "#273449" : "#22304A");
  }

  function drawPlant(x, y) {
    px(x, y + 10, 8, 7, "#92400E");
    px(x + 1, y + 2, 2, 8, "#15803D"); px(x + 4, y, 2, 10, "#16A34A"); px(x + 6, y + 3, 2, 7, "#15803D");
  }

  function drawSeat(a, sx, sy, t) {
    const p = pose(a);
    const shirt = SHIRT[a.app] || "#64748B";
    const hair = HAIR[hash(a.name) % HAIR.length];
    const skin = "#F5C9A0";
    const bob = reduced ? 0 : t % 2;
    ctx.save();
    if (a.mode === "off") ctx.globalAlpha = 0.45;

    // 椅背
    px(sx + 33, sy + 40, 18, 20, "#0F172A");
    // 人：頭、頭髮、身體
    let hy = sy + 26, by = sy + 36;
    if (p === "cheer" && bob) { hy -= 2; by -= 2; }
    if (p === "sleep") hy += 7;
    px(sx + 34, by, 16, 14, shirt);
    px(sx + 38, hy, 8, 9, skin);
    px(sx + 37, hy - 1, 10, 3, hair);
    px(sx + 37, hy + 2, 1, 3, hair); px(sx + 46, hy + 2, 1, 3, hair);
    if (p !== "sleep") { px(sx + 40, hy + 4, 1, 1, "#111827"); px(sx + 43, hy + 4, 1, 1, "#111827"); }
    else { px(sx + 40, hy + 4, 2, 1, "#111827"); px(sx + 43, hy + 4, 2, 1, "#111827"); }
    if (p === "talk" && bob) px(sx + 41, hy + 7, 2, 1, "#7F1D1D");
    if (p === "cheer") px(sx + 40, hy + 6, 4, 1, "#7F1D1D");

    // 手臂
    if (p === "type") {
      px(sx + 32, by + 4, 3, 10, shirt); px(sx + 49, by + 4, 3, 10, shirt);
      px(sx + 33, by + 13 - bob, 3, 2, skin); px(sx + 48, by + 12 + bob, 3, 2, skin);
    } else if (p === "raise") {
      const wave = reduced ? 0 : (t % 4 < 2 ? 0 : 1);
      px(sx + 32, by + 4, 3, 10, shirt);
      px(sx + 50 + wave, by - 12, 3, 16, shirt);
      px(sx + 50 + wave, by - 15, 3, 3, skin);
    } else if (p === "cheer") {
      px(sx + 30, by - 12, 3, 14, shirt); px(sx + 51, by - 12, 3, 14, shirt);
      px(sx + 30, by - 15, 3, 3, skin); px(sx + 51, by - 15, 3, 3, skin);
    } else {
      px(sx + 32, by + 4, 3, 11, shirt); px(sx + 49, by + 4, 3, 11, shirt);
    }

    // 桌子、鍵盤、螢幕、名牌
    px(sx + 8, sy + 56, 64, 5, "#B45309");
    px(sx + 8, sy + 61, 64, 18, "#78350F");
    px(sx + 10, sy + 79, 3, 8, "#451A03"); px(sx + 67, sy + 79, 3, 8, "#451A03");
    px(sx + 36, sy + 54, 13, 2, "#94A3B8");
    px(sx + 12, sy + 37, 20, 15, "#111827");
    const screen = { type: "#064E3B", raise: "#713F12", talk: "#1E3A8A", cheer: "#065F46", error: "#7F1D1D" }[p] || "#0B1120";
    px(sx + 13, sy + 38, 18, 12, screen);
    if (p === "type") {
      for (let i = 0; i < 4; i++) {
        const len = 4 + ((hash(a.name) + i * 7 + (reduced ? 0 : t)) % 11);
        px(sx + 14, sy + 39 + i * 3, len, 1, "#34D399");
      }
    } else if (p === "cheer") {
      px(sx + 18, sy + 44, 2, 2, "#A7F3D0"); px(sx + 20, sy + 46, 2, 2, "#A7F3D0");
      px(sx + 22, sy + 44, 2, 2, "#A7F3D0"); px(sx + 24, sy + 42, 2, 2, "#A7F3D0");
    } else if (p === "error" && bob) {
      px(sx + 13, sy + 38, 18, 12, "#DC2626");
    } else if (p === "raise") {
      px(sx + 21, sy + 40, 2, 5, "#FDE68A"); px(sx + 21, sy + 46, 2, 2, "#FDE68A");
    }
    px(sx + 21, sy + 52, 2, 4, "#111827");
    px(sx + 28, sy + 65, 24, 7, MODE_COLOR[a.mode] || "#64748B");
    px(sx + 30, sy + 67, 20, 3, "#0F172A");
    // 驗收進度：名牌上的小進度條
    if (a.core) px(sx + 30, sy + 67, Math.round(20 * a.verified / a.core), 3, "#FDE68A");

    // 頭上的對話泡泡
    const bubble = (text, color) => {
      px(sx + 52, sy + 10, 16, 11, "#F8FAFC");
      px(sx + 52, sy + 21, 3, 3, "#F8FAFC");
      ctx.fillStyle = color;
      ctx.font = "bold 9px monospace";
      ctx.textAlign = "center";
      ctx.fillText(text, sx + 60, sy + 19);
    };
    if (p === "raise") bubble("!", "#DC2626");
    else if (p === "talk") bubble(".".repeat(1 + (reduced ? 2 : t % 3)), "#1E40AF");
    else if (p === "error") bubble("?", "#DC2626");
    else if (p === "sleep" && a.mode !== "off") {
      ctx.fillStyle = "#CBD5E1"; ctx.font = "bold 8px monospace"; ctx.textAlign = "left";
      ctx.fillText("z", sx + 50, sy + 26 - (reduced ? 0 : t % 3)); ctx.fillText("Z", sx + 55, sy + 20 - (reduced ? 0 : t % 3));
    }
    if (p === "cheer" && !reduced) {
      const cs = ["#F472B6", "#FBBF24", "#34D399", "#60A5FA"];
      for (let i = 0; i < 6; i++) px(sx + 20 + ((i * 13 + t * 5) % 44), sy + 6 + ((i * 7 + t * 3) % 22), 2, 2, cs[i % 4]);
    }
    ctx.restore();
  }

  function layout() {
    const n = Math.max(1, agents.length);
    const avail = Math.max(200, wrap.clientWidth - 4);
    scale = Math.max(2, Math.min(4, Math.floor(avail / (SEAT_W * Math.min(n, 4)))));
    cols = Math.max(1, Math.min(n, Math.floor(avail / (SEAT_W * scale))));
    const rows = Math.ceil(n / cols);
    const w = cols * SEAT_W, h = WALL_H + rows * SEAT_H;
    canvas.width = w * scale; canvas.height = h * scale;
    canvas.style.width = `${w * scale}px`; canvas.style.height = `${h * scale}px`;
    return { w, h };
  }

  function draw() {
    if (!canvas || wrap.hidden) return;
    const { w, h } = layout();
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.imageSmoothingEnabled = false;
    drawRoom(w, h);
    seats = [];
    if (!agents.length) {
      ctx.fillStyle = "#94A3B8"; ctx.font = "8px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("辦公室還沒有人", w / 2, WALL_H + 40);
    }
    agents.forEach((a, i) => {
      const sx = (i % cols) * SEAT_W, sy = WALL_H + Math.floor(i / cols) * SEAT_H;
      if (i % cols === cols - 1 && i % 2) drawPlant(sx + SEAT_W - 10, sy + 70);
      drawSeat(a, sx, sy, frame);
      seats.push({ a, x: sx, y: sy });
    });
    // 名字用裝置座標畫，字才清楚
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.font = "12px 'Microsoft JhengHei UI', sans-serif";
    ctx.textAlign = "center";
    for (const s of seats) {
      ctx.fillStyle = s.a.mode === "off" ? "#64748B" : "#E2E8F0";
      ctx.fillText(s.a.project, (s.x + SEAT_W / 2) * scale, (s.y + 97) * scale);
    }
  }

  function seatAt(ev) {
    const r = canvas.getBoundingClientRect();
    const x = (ev.clientX - r.left) / scale, y = (ev.clientY - r.top) / scale;
    return seats.find(s => x >= s.x && x < s.x + SEAT_W && y >= s.y && y < s.y + SEAT_H);
  }

  function init() {
    wrap = document.getElementById("office");
    canvas = document.getElementById("office-canvas");
    if (!wrap || !canvas) return;
    ctx = canvas.getContext("2d");
    const btn = document.getElementById("office-toggle");
    const show = on => {
      wrap.hidden = !on;
      btn.textContent = on ? "🏢 收起辦公室" : "🏢 辦公室";
      localStorage.setItem("aea-office", on ? "1" : "0");
      draw();
    };
    btn.addEventListener("click", () => show(wrap.hidden));
    show(localStorage.getItem("aea-office") !== "0");
    canvas.addEventListener("mousemove", ev => {
      const s = seatAt(ev);
      canvas.title = s ? `${s.a.name}\n${s.a.phase_label}\n${s.a.doing || ""}` : "";
      canvas.style.cursor = s ? "pointer" : "default";
    });
    canvas.addEventListener("click", ev => {
      const s = seatAt(ev);
      const card = s && document.querySelector(`.card[data-key="${CSS.escape(s.a.key)}"]`);
      if (card) { card.scrollIntoView({ behavior: "smooth", block: "center" }); card.classList.add("flash");
                  setTimeout(() => card.classList.remove("flash"), 1200); }
    });
    window.addEventListener("resize", draw);
    if (!reduced) setInterval(() => { frame++; draw(); }, 250);
  }

  window.Office = { update(list) { agents = list || []; draw(); } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
