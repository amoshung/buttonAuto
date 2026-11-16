"use strict";
// 所有資料都用 textContent 放進畫面，不用 innerHTML（代理的文字可能含任何內容）
const TOKEN = document.querySelector('meta[name="aea-token"]').content;
const MODE_TEXT = { auto: "自動駕駛", suggest: "只建議", off: "關閉" };
const STATE_TEXT = { working: "工作中", waiting_approval: "等你允許", asking_choice: "在問選哪個",
                     waiting_input: "停下來等指示", done: "說做完了", error: "出錯", idle: "閒置" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}

function renderAgents(agents) {
  const box = document.getElementById("agents");
  box.replaceChildren();
  if (!agents.length) { box.append(el("div", "empty", "還沒有工作階段")); return; }
  for (const a of agents) {
    const hand = (a.needs_you.length || a.paused) && a.mode !== "off";
    const card = el("div", `card ${a.mode}${hand ? " hand" : ""}`);
    const who = el("div", "who");
    const av = el("div", "avatar", a.avatar);
    av.append(el("span", "mood", hand ? "🙋" : a.mood));
    const head = el("div");
    const nm = el("span", "name", a.name);
    head.append(nm, el("span", `mode ${a.mode}`, MODE_TEXT[a.mode] || a.mode));
    head.append(el("div", "phase", `${a.phase_label}｜${STATE_TEXT[a.agent_state] || a.agent_state || "—"}`));
    who.append(av, head);
    card.append(who);
    card.append(el("div", "line", `🎯 ${a.goal || "（尚未設定目標）"}`));
    if (a.doing) card.append(el("div", "line dim", `正在做：${a.doing}`));
    if (a.chat) card.append(el("div", "line dim", `對話：${a.chat}`));
    if (a.core) {
      const bar = el("div", "bar"); const fill = el("i");
      fill.style.width = `${Math.round(100 * a.verified / a.core)}%`;
      bar.append(fill); card.append(bar);
      card.append(el("div", "dim", `驗收：${a.verified}/${a.core} 條有證據` +
                                   (a.manual ? `｜手動 ${a.manual_checked}/${a.manual}` : "")));
    }
    if (a.paused) card.append(el("div", "line", `⏸ ${a.paused}`));
    if (a.mode === "auto") card.append(el("div", "dim", `這小時自動送出 ${a.hour_sends}/${a.hour_cap}`));
    box.append(card);
  }
}

async function decide(key, button, btns) {
  btns.forEach(b => b.disabled = true);
  try {
    const r = await fetch("/api/decide", { method: "POST", headers: { "Content-Type": "application/json",
      "X-Token": TOKEN }, body: JSON.stringify({ key, button }) });
    const j = await r.json();
    toast(j.text || (j.ok ? "已排入" : "失敗"));
    if (!j.ok) btns.forEach(b => b.disabled = false);
  } catch (e) {
    toast("連不上助理：" + e);
    btns.forEach(b => b.disabled = false);
  }
}

function renderDecisions(items) {
  const box = document.getElementById("decisions");
  box.replaceChildren();
  if (!items.length) { box.append(el("div", "empty", "目前沒有要你決定的事")); return; }
  for (const d of items) {
    const card = el("div", `decision ${d.kind}`);
    card.append(el("div", "name", d.name));
    if (d.kind === "approval") {
      card.append(el("div", "line", "代理想執行："));
      card.append(el("pre", "", d.text));
      const btns = [];
      for (const b of d.buttons) {
        const low = b.toLowerCase();
        const cls = /deny|reject|skip|拒絕/.test(low) ? "deny" : (/always/.test(low) ? "always" : "");
        const btn = el("button", cls, /deny|reject|拒絕/.test(low) ? `拒絕（${b}）` :
                                       (/always/.test(low) ? b : `允許一次（${b}）`));
        btn.addEventListener("click", () => decide(d.key, b, btns));
        btns.push(btn); card.append(btn);
      }
    } else {
      card.append(el("div", "line", d.text));
      card.append(el("div", "dim", "在桌面版的「詳情」處理"));
    }
    box.append(card);
  }
}

function renderFeed(feed) {
  const box = document.getElementById("feed");
  box.replaceChildren();
  if (!feed.length) { box.append(el("li", "empty", "還沒有動態")); return; }
  for (const f of feed) {
    const li = el("li");
    li.append(el("span", "t", f.time.slice(11, 16)), el("span", "src", `${f.avatar} ${f.name}`), el("span", "", f.text));
    box.append(li);
  }
}

function meter(label, value, note) {
  const m = el("div", "meter");
  m.append(el("b", "", value), el("span", "", label));
  if (note) m.append(el("div", "dim", note));
  return m;
}

function renderUsage(u, agents) {
  const box = document.getElementById("usage");
  box.replaceChildren();
  const t = u.today;
  box.append(meter("次代按（助理）", t.auto_clicks), meter("次自動送出", t.auto_sends),
             meter("次你親自操作", t.user_actions), meter("次查核報告", t.verifies), meter("次交給你", t.asks));
  const r = u.reads || {};
  const reads = (r.text_reads || 0) + (r.image_reads || 0);
  if (reads) box.append(meter("次讀畫面", reads, `只讀文字 ${r.text_reads || 0}｜含截圖 ${r.image_reads || 0}（本次啟動）`));
  const models = Object.entries(u.models || {});
  if (models.length) {
    const m = el("div", "meter"); m.append(el("div", "name", "模型呼叫（本次啟動）"));
    for (const [k, v] of models.sort((a, b) => b[1] - a[1])) m.append(el("div", "dim", `${k}：${v} 次`));
    box.append(m);
  }
  for (const a of agents.filter(a => a.mode === "auto")) {
    const m = meter("自動送出上限使用率", `${Math.round(100 * a.hour_sends / Math.max(1, a.hour_cap))}%`, a.name);
    box.append(m);
  }
}

function render(s) {
  document.getElementById("clock").textContent = `更新於 ${s.time}`;
  const run = document.getElementById("running");
  run.textContent = s.running ? "🟢 運作中" : "⏸ 已全部暫停（F8）";
  run.className = "pill " + (s.running ? "ok" : "bad");
  renderAgents(s.agents);
  renderDecisions(s.decisions);
  renderFeed(s.feed);
  renderUsage(s.usage, s.agents);
}

let toastTimer = 0;
function toast(text) {
  const t = document.getElementById("toast");
  t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}

function connect() {
  const conn = document.getElementById("conn");
  const es = new EventSource("/api/events");
  es.onopen = () => { conn.textContent = "● 即時"; conn.className = "pill ok"; };
  es.onmessage = ev => render(JSON.parse(ev.data));
  es.onerror = () => { conn.textContent = "○ 重新連線中"; conn.className = "pill bad"; };
}

fetch("/api/state").then(r => r.json()).then(render).catch(() => {});
connect();
