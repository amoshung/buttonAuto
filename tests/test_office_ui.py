"""像素辦公室前端（Playwright）：示範房跑到合併、收件匣、座位、換頁、派工精靈、輸入中不被重繪覆蓋。

沒有 Playwright 或 Chromium 時整個檔案略過。伺服器用 test_server_team.py 的示範模式 fixture。
"""
import json
import os

import pytest

pw = pytest.importorskip("playwright.sync_api")

from test_server_team import TOKEN, J, git_repo, msrv, srv, wait  # noqa: E402,F401  （srv、msrv 是 fixture）


@pytest.fixture()
def browser():
    try:
        p = pw.sync_playwright().start()
    except Exception as e:  # pragma: no cover - 環境沒有 Playwright 驅動
        pytest.skip("Playwright 無法啟動：%s" % e)
    try:
        b = p.chromium.launch()
    except Exception as e:  # pragma: no cover - 沒有安裝 Chromium
        p.stop()
        pytest.skip("Chromium 無法啟動：%s" % e)
    yield b
    b.close()
    p.stop()


def open_console(browser, app, width=1366, height=768):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
    page.on("console", lambda m: errors.append("console: %s" % m.text) if m.type == "error" else None)
    page.goto("http://127.0.0.1:%d/?t=%s" % (app.port, TOKEN))
    page.wait_for_selector(".office canvas", timeout=10000)
    return page, errors


def test_demo_room_to_merge(srv, browser):  # noqa: F811
    page, errors = open_console(browser, srv)
    assert page.locator("main h1").inner_text() == "辦公室"
    assert page.locator('.nav button[data-view="office"]').get_attribute("aria-current") == "page"
    assert page.locator(".office canvas").count() >= 1
    page.screenshot(path="/tmp/office-empty.png")

    page.get_by_role("button", name="試玩示範").click()
    page.wait_for_function("() => [...document.querySelectorAll('.o-plate')].some(e => e.textContent.includes('示範'))",
                           timeout=30000)

    page.locator(".o-hit[data-room]").first.click()
    page.wait_for_selector("#office-drawer .o-stagebar .station", timeout=5000)
    assert page.locator("#office-drawer .o-demotag").inner_text() == "示範（假代理）"
    page.keyboard.press("Escape")
    assert page.locator("#office-drawer .drawer").count() == 0

    page.wait_for_selector(".o-rail .o-item.k-merge", timeout=60000)
    page.get_by_role("button", name="收件匣").click()
    merge = page.locator("#office-drawer .o-item.k-merge")
    merge.wait_for(timeout=5000)
    assert merge.locator("pre.o-stat").count() == 1 and merge.locator(".o-commits li").count() >= 1
    page.wait_for_timeout(400)  # 抽屜滑入動畫
    page.screenshot(path="/tmp/office-inbox.png")
    merge.get_by_role("button", name="合併", exact=True).click()
    page.wait_for_function("() => [...document.querySelectorAll('.o-act')].some(e => e.textContent.includes('已合併'))",
                           timeout=30000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.screenshot(path="/tmp/office.png")

    page.get_by_role("button", name="座位", exact=True).click()
    page.wait_for_selector("#office-drawer .o-seatrow", timeout=5000)
    assert page.locator("#office-drawer .o-seatrow").count() == 5
    page.wait_for_timeout(400)
    page.screenshot(path="/tmp/office-seats.png")
    page.keyboard.press("Escape")

    # 封存：確認對話框 → 提示「已封存」，焦點移到換頁列
    page.on("dialog", lambda d: d.accept())
    page.locator(".o-hit[data-room]").first.click()
    page.locator("#office-drawer").get_by_role("button", name="封存").click()
    page.wait_for_function("() => [...document.querySelectorAll('.toast')].some(t => t.textContent === '已封存')", timeout=10000)
    assert page.locator("#office-drawer .drawer").count() == 0
    assert page.evaluate("!!document.activeElement.closest('.o-pager')")
    page.wait_for_selector(".o-empty:not(.hidden)", timeout=10000)
    assert errors == []


def test_paging_wizard_and_drafts(srv, browser, tmp_path):  # noqa: F811
    for _ in range(4):
        assert J(srv, "/api/team/demo", {"autonomy": "careful"})[1]["ok"] is True
    page, errors = open_console(browser, srv)
    page.wait_for_function("() => document.querySelectorAll('.o-rail .o-item.k-approve_spec').length >= 1",
                           timeout=30000)
    assert page.locator(".o-pnum").inner_text() == "第 1／2 頁"
    assert page.locator(".o-dot.hot").count() >= 1
    page.screenshot(path="/tmp/office-busy.png")

    # 輸入中收到新快照：textarea 內容與焦點都要留著
    page.locator(".o-hit[data-room]").first.click()
    talk = page.locator("#o-talk")
    talk.click()
    talk.type("先別動 README")
    page.evaluate("async () => { const r = await fetch('/api/team/state'); window.Office.onState(await r.json()); }")
    assert talk.input_value() == "先別動 README"
    assert page.evaluate("document.activeElement && document.activeElement.id") == "o-talk"
    page.keyboard.press("Escape")

    # 從右側一鍵開工
    first = page.locator(".o-rail .o-item.k-approve_spec").first
    item_id = first.get_attribute("data-item")
    first.get_by_role("button", name="開工", exact=True).click()
    page.wait_for_function("(id) => !document.querySelector(`.o-rail [data-item=\"${id}\"]`)", arg=item_id, timeout=15000)

    # 換頁：方向鍵
    page.locator("main").click(position={"x": 5, "y": 5})
    page.keyboard.press("ArrowRight")
    assert page.locator(".o-pnum").inner_text() == "第 2／2 頁"
    page.keyboard.press("ArrowLeft")
    assert page.locator(".o-pnum").inner_text() == "第 1／2 頁"
    page.keyboard.press("ArrowRight")

    # 派工精靈：先給不是 git repo 的資料夾 → 紅字停在步驟 2
    plain = tmp_path / "plain"
    plain.mkdir()
    page.locator(".o-dispatch").first.click()
    page.locator("#o-path").fill(str(plain))
    page.get_by_role("button", name="下一步：檢查資料夾").click()
    page.wait_for_selector("#office-drawer .o-err", timeout=10000)
    assert "git" in page.locator("#office-drawer .o-err").first.inner_text()
    assert page.get_by_role("button", name="下一步：陣容").count() == 0

    repo = str(tmp_path / "wizrepo")
    git_repo(repo)
    page.get_by_role("button", name="換一個資料夾").click()
    page.locator("#o-path").fill(repo)
    page.get_by_role("button", name="下一步：檢查資料夾").click()
    page.wait_for_selector("#o-goal", timeout=10000)
    assert page.locator('input[name="o-src"][value="progress"]').is_checked()
    assert "mul" in page.locator("#o-goal").input_value()
    page.get_by_role("button", name="下一步：陣容").click()
    assert page.locator("#o-impl option").first.inner_text().startswith("自動（Decision-1")
    page.get_by_role("button", name="下一步：確認").click()
    page.wait_for_timeout(300)
    page.screenshot(path="/tmp/office-wizard.png")
    page.locator("#office-drawer").get_by_role("button", name="開工").click()
    page.wait_for_selector("#office-drawer .drawer", state="detached", timeout=15000)
    page.wait_for_function("(n) => [...document.querySelectorAll('.o-plate')].some(e => e.textContent.includes(n))",
                           arg=os.path.basename(repo), timeout=15000)
    assert page.locator("#office-drawer .drawer").count() == 0
    assert errors == []


INJECT = """async (patch) => {
  const r = await fetch('/api/team/state'); const s = await r.json();
  for (const room of s.rooms) Object.assign(room, patch || {}, { activity: 'tick ' + Math.random() });
  window.Office.onState(s);
}"""


def test_header_nav_and_old_ui_gone(srv, browser):  # noqa: F811
    assert J(srv, "/api/team/demo", {"autonomy": "careful"})[1]["ok"] is True
    page, errors = open_console(browser, srv)
    page.goto("http://127.0.0.1:%d/#overview" % srv.port)
    page.reload()  # 只換 hash 不會重新載入
    page.wait_for_selector(".office canvas")
    assert page.evaluate("location.hash") == ""
    navs = page.locator(".nav button[data-view]").all_inner_texts()
    assert [n.split()[0] for n in navs] == ["辦公室", "日誌", "設定"]
    for gone in ("IDE 視窗", "像素畫面", "基本按鈕精靈", "API 連線", "自主程度"):
        assert gone not in page.locator("body").inner_text()
    page.wait_for_function("() => document.querySelector('#run-what').textContent.includes('等你 1')", timeout=30000)
    assert "運作中" in page.locator("#run-what").inner_text() and "排隊" in page.locator("#run-what").inner_text()
    page.screenshot(path="/tmp/office2-header.png")
    page.keyboard.press("F8")
    page.wait_for_function("() => document.querySelector('#run-sub').textContent.includes('暫停')", timeout=15000)
    assert all(r["status"] == "paused" for r in J(srv, "/api/team/state")[1]["rooms"])
    assert page.locator("#run-btn").is_disabled()
    # 設定頁只剩辦公室用得到的
    page.locator('.nav button[data-view="settings"]').click()
    page.wait_for_selector("#f-team_context_rollover")
    assert page.locator("#f-ai_max_risk").count() == 0
    assert errors == []


def test_rail_click_details_and_errors_survive_snapshots(srv, browser):  # noqa: F811
    assert J(srv, "/api/team/demo", {"autonomy": "careful"})[1]["ok"] is True
    page, errors = open_console(browser, srv)
    page.wait_for_selector(".o-rail .o-item.k-approve_spec", timeout=30000)

    # 派工標籤只看 impl_by／review_by
    page.evaluate(INJECT, {"impl_by": "Decision-1", "review_by": "rule", "impl_reason": "", "review_reason": ""})
    assert page.locator(".o-desk .o-pick").all_inner_texts()[:2] == ["D1 派工", "規則"]
    assert page.locator(".o-desk .o-pick").first.get_attribute("title") == "由 Decision-1 派工"
    page.evaluate(INJECT, {"impl_by": "user", "review_by": ""})
    assert page.locator(".o-desk .o-pick").all_inner_texts()[:1] == ["你指定"]
    page.evaluate(INJECT, {"impl_by": "", "review_by": ""})
    assert page.locator(".o-desk .o-pick").count() == 0

    # 錯誤訊息：快照更新好幾次後仍在
    page.route("**/api/team/answer", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"ok": false, "text": "測試：回答失敗"}'))
    page.locator(".o-rail .o-item.k-approve_spec").get_by_role("button", name="開工", exact=True).click()
    page.wait_for_selector(".o-rail .o-err", timeout=5000)
    for i in range(3):
        page.evaluate(INJECT, {"context_pct": 10 + i})
    assert page.locator(".o-rail .o-err").inner_text() == "測試：回答失敗"
    page.unroute("**/api/team/answer")

    # 全文 details：快照來了仍然開著
    page.locator(".o-hit[data-room]").first.click()
    page.wait_for_selector("#office-drawer details", timeout=10000)
    det = page.locator("#office-drawer details").first
    det.locator("summary").click()
    assert det.evaluate("d => d.open")
    page.evaluate(INJECT, {"context_pct": 42})
    page.wait_for_timeout(300)
    assert page.locator("#office-drawer details[open]").count() >= 1
    page.keyboard.press("Escape")

    # 按下右側按鈕、快照到達、放開 → 點擊仍然成立並送出 POST
    btn = page.locator(".o-rail .o-item.k-approve_spec").get_by_role("button", name="開工", exact=True)
    box = btn.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    with page.expect_request(lambda r: r.url.endswith("/api/team/answer") and r.method == "POST", timeout=10000):
        page.mouse.down()
        page.evaluate(INJECT, {"context_pct": 55})
        page.mouse.up()
    page.wait_for_function("() => !document.querySelector('.o-rail .o-item.k-approve_spec')", timeout=15000)
    assert errors == []


def test_state_retry_and_seat_time_guard(srv, browser):  # noqa: F811
    assert J(srv, "/api/team/demo", {"autonomy": "careful"})[1]["ok"] is True
    page = browser.new_page(viewport={"width": 1366, "height": 768})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    calls = []

    def state(route):
        calls.append(1)
        if len(calls) == 1:
            route.fulfill(status=500, content_type="application/json", body='{"ok": false, "text": "暫時讀不到"}')
        else:
            route.continue_()
    page.route("**/api/stream*", lambda route: route.abort())  # 沒有串流：只能靠重試
    page.route("**/api/team/state", state)
    page.goto("http://127.0.0.1:%d/?t=%s" % (srv.port, TOKEN))
    page.wait_for_function("() => document.querySelector('.o-sum') && document.querySelector('.o-sum').textContent.includes('秒後重試')",
                           timeout=10000)
    page.wait_for_selector(".o-plate", timeout=10000)
    assert len(calls) >= 2
    # 不合理的試探時間不倒數
    page.evaluate("""async () => {
      const s = await (await fetch('/api/team/state')).json();
      s.fleet.kiro.quota = { state: 'exhausted', until: '2099-01-01T00:00:00', since: '' };
      s.fleet.agy.quota = { state: 'exhausted', until: '2001-01-01T00:00:00', since: '' };
      window.Office.onState(s);
    }""")
    page.get_by_role("button", name="座位", exact=True).click()
    page.wait_for_selector("#office-drawer .o-seatrow", timeout=5000)
    texts = page.locator("#office-drawer .o-sst").all_inner_texts()
    assert texts[0] == "已滿，等待下次試探" and texts[1] == "已滿，等待下次試探"
    assert errors == []


def test_login_item(srv, browser):  # noqa: F811
    from team.runner import OPT_LOGIN
    _, res = J(srv, "/api/team/demo", {"autonomy": "careful"})
    assert res["ok"] is True
    rid = res["room"]["id"]
    office = srv.team
    item, _ = office.store.join_login(rid, "kiro", "demo", "Kiro 需要登入",
                                      "Kiro 回報需要登入：\nSign in to Kiro to send a prompt.", OPT_LOGIN)
    office._changed()
    page, errors = open_console(browser, srv)
    card = page.locator('.o-rail .o-item.k-login')
    card.wait_for(timeout=30000)
    assert card.locator(".tag").inner_text() == "需要你登入"
    btns = card.locator("button.btn")
    assert btns.all_inner_texts() == ["開啟登入視窗", "我已登入，重新檢查", "改用其他座位"]
    assert "hot" in btns.first.get_attribute("class")

    opened = "已開啟登入視窗，登入完成後會自動繼續（每 5 秒檢查一次）"
    page.route("**/api/team/answer", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"ok": True, "text": opened})))
    with page.expect_request(lambda r: r.url.endswith("/api/team/answer") and r.method == "POST", timeout=10000) as req:
        card.get_by_role("button", name="開啟登入視窗").click()
    assert json.loads(req.value.post_data) == {"item_id": item.id, "answer": "open", "text": ""}
    page.wait_for_function("(t) => document.querySelector('.o-rail .o-item.k-login').textContent.includes(t)",
                           arg=opened, timeout=5000)
    office.store.update_item(item.id, lambda x: setattr(x, "state", "opened"))
    office._changed()
    page.wait_for_function("() => document.querySelector('.o-rail .o-item.k-login button.btn').textContent === '我已登入，重新檢查'",
                           timeout=10000)
    assert card.locator("button.btn").all_inner_texts() == ["我已登入，重新檢查", "改用其他座位", "再開一次登入視窗"]
    assert card.locator("button.btn").first.is_enabled()
    assert page.locator(".o-rail .o-item.k-login").inner_text().count(opened) == 1
    page.unroute("**/api/team/answer")
    office.store.resolve(item.id, "ok", "已登入")
    office._changed()
    page.wait_for_function("() => !document.querySelector('.o-rail .o-item.k-login')", timeout=10000)
    # 座位抽屜：一行公司網路憑證狀態
    page.get_by_role("button", name="座位", exact=True).click()
    page.wait_for_selector("#office-drawer .o-certs", timeout=5000)
    assert page.locator("#office-drawer .o-certs").inner_text().startswith("公司網路憑證：")
    assert errors == []


def test_usage_drawer_matches_api_and_model_events(srv, browser):  # noqa: F811
    """用量抽屜的數字與 /api/team/usage 一致；房間時間軸有 D1／GLM 模型事件；桌上標籤寫實際模型或「CLI 預設（未回報）」。"""
    from team.models import ModelReply
    from team.usage import ModelCall
    _, res = J(srv, "/api/team/demo", {})
    rid = res["room"]["id"]
    room = wait(lambda: next((r for r in J(srv, "/api/team/state")[1]["rooms"]
                              if r["id"] == rid and r["status"] == "done"), None), 60)
    assert room is not None
    # 再補兩筆「真模型」紀錄（假 glm 回報 usage；Decision-1 不回報 token），看抽屜怎麼呈現
    office = srv.team
    router = office._router(False)
    router._glm = lambda p, e, t: ModelReply("重點：檢查 mul 的負數", {"prompt_tokens": 1200, "completion_tokens": 80,
                                                                  "total_tokens": 1280}, "glm-5.3")
    router._d1 = lambda s, q: {"q": {"noul": 0.82}}
    router.write("x", ctx={"room_id": rid, "role": "reviewer", "purpose": "Reviewer 審查重點"})
    router.probability("s", "q", ctx={"room_id": rid, "role": "qa", "purpose": "檢查驗收條件品質", "pass_min": 0.6})
    office._changed()
    api = J(srv, "/api/team/usage")[1]

    page, errors = open_console(browser, srv)
    page.wait_for_selector(".o-plate", timeout=10000)
    # 桌上標籤：Kiro（示範假代理，ACP 不回報模型）→ CLI 預設（未回報）
    assert "CLI 預設（未回報）" in page.locator(".o-desk .o-seat").first.inner_text()
    # 鍵盤：Tab 到「用量」按 Enter
    btn = page.get_by_role("button", name="用量：模型呼叫與 token")
    btn.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#office-drawer .o-utable", timeout=5000)
    for m in api["today"]["models"]:
        row = page.locator('#office-drawer tr[data-model="%s|%s"]' % (m["model"], m["effort"]))
        assert row.locator('[data-k="calls"]').inner_text() == "{:,}".format(m["calls"])
        assert row.locator('[data-k="fail"]').inner_text() == "{:,}".format(m["fail"])
        assert row.locator('[data-k="latency"]').inner_text() == "{:,} ms".format(m["avg_latency_ms"])
    glm = page.locator('#office-drawer tr[data-model="glm-5.3|low"] [data-k="tokens"]').inner_text()
    assert glm.startswith("1,280（輸入 1,200／輸出 80）")
    assert page.locator('#office-drawer tr[data-model="Decision-1|-"] [data-k="tokens"]').inner_text().startswith("不計 token")
    assert page.locator('#office-drawer tr[data-model="scripted|-"] [data-k="tokens"]').inner_text() == "示範不計 token"
    assert "代 Decision-1" in page.locator('#office-drawer tr[data-model="scripted|-"] th').inner_text()
    for c in api["today"]["cli"]:
        row = page.locator('#office-drawer tr[data-seat="%s%s"]' % (c["seat"], ":demo" if c["demo"] else ""))
        want_in = "—（未回報）" if c["in"] is None else "{:,}".format(c["in"])
        assert row.locator('[data-k="in"]').inner_text() == want_in
        assert row.locator('[data-k="turns"]').inner_text().startswith("{:,}".format(c["turns"]))
    r0 = api["today"]["rooms"][0]
    assert page.locator('#office-drawer tr[data-room="%s"] [data-k="calls"]' % r0["room_id"]).inner_text() == \
        "{:,}".format(r0["model_calls"])
    # 切到「本次啟動」
    page.get_by_role("button", name="本次啟動", exact=False).click()
    assert page.get_by_role("button", name="本次啟動", exact=False).get_attribute("aria-pressed") == "true"
    page.wait_for_timeout(400)
    page.screenshot(path="/tmp/office4-usage.png")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement && document.activeElement.textContent") == "用量"

    # 進行中的模型呼叫：像素人泡泡
    page.evaluate(INJECT, {"inflight": [{"room_id": rid, "role": "reviewer", "model": "glm-5.3", "effort": "low",
                                         "purpose": "Reviewer 審查重點",
                                         "since": page.evaluate("new Date(Date.now() - 3000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 19)")}]})
    assert page.locator(".o-bub .o-thk").first.inner_text().startswith("glm-5.3 審查中…")

    # 房間時間軸：D1／GLM 小標
    page.locator('.o-hit[data-room="%s"]' % rid).click()
    page.wait_for_selector("#office-drawer .o-tl li.k-model .o-mtag.glm", timeout=10000)
    glm_row = page.locator("#office-drawer .o-tl li.k-model").filter(has=page.locator(".o-mtag.glm")).first
    assert "Reviewer：Reviewer 審查重點" in glm_row.inner_text() and "共 1280" in glm_row.inner_text()
    d1_row = page.locator("#office-drawer .o-tl li.k-model").filter(has=page.locator(".o-mtag.d1")).first
    assert "0.82 ≥ 0.60 通過" in d1_row.inner_text() and "不計 token" in d1_row.inner_text()
    page.wait_for_selector("#office-drawer .o-tl li.k-cli_usage", timeout=10000)  # 房間事件清單讀到後才有
    glm_row.scroll_into_view_if_needed()
    page.wait_for_timeout(400)
    page.screenshot(path="/tmp/office4-room.png", full_page=False)
    assert errors == []


# 用真實的示範房，改幾個欄位模擬各階段（欄位形狀與快照相同）
SCENE = """async ([rid, patch, extra]) => {
  const s = await (await fetch('/api/team/state')).json();
  const now = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  for (const room of s.rooms) {
    if (room.id !== rid) continue;
    Object.assign(room, patch || {});
    room.events = (extra.events || []).map((e) => Object.assign({ ts: now }, e));
    if (extra.items === false) s.inbox = s.inbox.filter((i) => i.room_id !== rid);
  }
  for (const k of Object.keys(s.roles)) s.roles[k] = '';
  if (patch && patch.persona) s.roles[patch.persona] = rid;
  window.Office.onState(s);
}"""


STILL = "() => !window.Office.debug().moving"  # 沒有人在走路


def bubble(page, actor):
    return page.locator('.o-cell.room .o-who[data-actor="%s"] .o-bub' % actor).inner_text()


def test_pixel_rooms_phases_bubbles_and_cards(srv, browser):  # noqa: F811
    _, res = J(srv, "/api/team/demo", {"autonomy": "careful"})
    rid = res["room"]["id"]
    for w, hgt in ((1366, 768), (1920, 1080)):
        page, errors = open_console(browser, srv, w, hgt)
        page.wait_for_selector(".o-rail .o-item.k-approve_spec", timeout=30000)
        suffix = "" if w == 1366 else "-1080"
        # 每個空格都是「＋ 派工」
        assert page.locator(".o-dispatch").count() == 3
        page.wait_for_function(STILL)
        page.screenshot(path="/tmp/office3-empty%s.png" % suffix)
        steps = [
            ("spec", {"status": "running", "phase": "spec", "persona": "po", "activity": "PO 起草驗收條件"},
             [{"kind": "phase", "text": "PO 起草驗收條件", "phase": "spec", "persona": "po"}]),
            ("build", {"status": "running", "phase": "build", "persona": "scrum", "activity": "實作中", "review_seat": "agy"},
             [{"kind": "phase", "text": "實作中", "phase": "build", "persona": "scrum"},
              {"kind": "tool", "text": "Write calc.py", "who": "Kiro"}]),
            ("test", {"status": "running", "phase": "test", "persona": "qa", "activity": "QA 在 worktree 跑測試", "review_seat": "agy"},
             [{"kind": "phase", "text": "QA 在 worktree 跑測試", "phase": "test", "persona": "qa"}]),
            ("review", {"status": "running", "phase": "review", "persona": "reviewer", "activity": "Reviewer 審查改動", "review_seat": "agy"},
             [{"kind": "phase", "text": "Reviewer 審查改動", "phase": "review", "persona": "reviewer"},
              {"kind": "say", "text": "【QA】測試關卡通過", "role": "qa"}]),
        ]
        for name, patch, evs in steps:
            page.evaluate(SCENE, [rid, patch, {"events": evs, "items": False}])
            page.wait_for_function(STILL)
            if name == "build":
                assert bubble(page, "impl") == "寫 calc.py"
            if name == "test":
                assert bubble(page, "role:qa") == "跑測試中"
                assert page.evaluate("(id) => window.Office.debug(id).scene.test", rid) == "run"
            if name == "review":
                assert page.evaluate("(id) => window.Office.debug(id).scene.test", rid) == "green"
            page.screenshot(path="/tmp/office3-%s%s.png" % (name, suffix))
        # 說明卡：房內代理與走廊角色
        page.locator('.o-cell.room .o-who[data-actor="impl"]').hover()
        card = page.locator("#o-card")
        card.wait_for(state="visible", timeout=3000)
        assert card.inner_text().startswith("Kiro（實作） — 負責寫程式的 CLI 代理")
        page.screenshot(path="/tmp/office3-hover%s.png" % suffix)
        page.locator('.o-hallwho .o-who[data-actor="qa"]').hover()
        page.wait_for_function("() => document.querySelector('#o-card').textContent.startsWith('QA — 品管')", timeout=3000)
        # 鍵盤：聚焦像素人也顯示同一張卡
        page.mouse.move(2, 2)
        page.locator('.o-cell.room .o-who[data-actor="role:reviewer"]').focus()
        assert page.locator("#o-card").inner_text().startswith("Reviewer — 唯讀審查")
        # 特寫：Enter 開、Esc 回
        page.locator(".o-hit[data-room]").first.focus()
        page.keyboard.press("Enter")
        page.wait_for_selector(".o-xview", timeout=5000)
        page.wait_for_function(STILL)
        page.screenshot(path="/tmp/office3-closeup%s.png" % suffix)
        page.keyboard.press("Escape")
        assert page.locator(".o-xview").count() == 0
        assert page.evaluate("document.activeElement.dataset.room") == rid
        # 雙擊房間也能開特寫，而且不會先開抽屜
        page.locator(".o-hit[data-room]").first.dblclick()
        page.wait_for_selector(".o-xview", timeout=5000)
        assert page.locator("#office-drawer .drawer").count() == 0
        page.keyboard.press("Escape")
        # 空格可點開派工精靈
        page.locator(".o-dispatch").last.click()
        page.wait_for_selector("#o-path", timeout=5000)
        page.keyboard.press("Escape")
        assert errors == []
        page.close()
    # 真的跑完：開工 → 等合併 → 合併 → 完成
    page, errors = open_console(browser, srv)
    page.locator(".o-rail .o-item.k-approve_spec").get_by_role("button", name="開工", exact=True).click()
    page.wait_for_selector(".o-rail .o-item.k-merge", timeout=60000)
    page.locator(".o-rail .o-item.k-merge").get_by_role("button", name="合併", exact=True).click()
    page.wait_for_function("() => [...document.querySelectorAll('.o-act')].some(e => e.textContent.includes('已合併'))", timeout=30000)
    page.wait_for_function("() => window.Office.debug().confetti && !window.Office.debug().moving", timeout=5000)
    page.screenshot(path="/tmp/office3-done.png")
    page.wait_for_function("() => !window.Office.debug().confetti && !window.Office.debug().moving", timeout=10000)
    assert "已合併" in bubble(page, "impl")
    page.screenshot(path="/tmp/office3-done-coffee.png")
    # 房間不在快照裡：畫面狀態一起釋放
    page.evaluate("async () => { const s = await (await fetch('/api/team/state')).json(); s.rooms = []; s.inbox = []; window.Office.onState(s); }")
    assert page.evaluate("window.Office.debug().rooms") == 0
    assert errors == []


def test_bubbles_only_say_what_the_data_says(srv, browser):  # noqa: F811
    """工具動詞只看標題第一個字；「已登入」只來自辦公室自己的通知；測試機綠燈只看 QA 關卡結果事件。"""
    _, res = J(srv, "/api/team/demo", {"autonomy": "careful"})
    rid = res["room"]["id"]
    page, errors = open_console(browser, srv)
    page.wait_for_selector(".o-rail .o-item.k-approve_spec", timeout=30000)
    build = {"status": "running", "phase": "build", "persona": "scrum"}

    def say_after(events):
        page.evaluate(SCENE, [rid, build, {"events": events, "items": False}])
        page.wait_for_function(STILL)
        return bubble(page, "impl")

    tool = lambda t: [{"kind": "tool", "text": t, "who": "Kiro"}]  # noqa: E731
    assert say_after(tool("Write test_calc.py")) == "寫 test_calc.py"
    assert say_after(tool("Patch calc.py")) == "calc.py"
    assert say_after(tool("Read README.md")) == "讀 README.md"
    assert say_after(tool("pytest -q tests/test_calc.py")) == "跑測試 test_calc.py"
    assert say_after(tool("npm test")) == "跑測試"
    assert "處理" not in say_after(tool("Something notes.txt"))
    # 代理自己說「已登入」不算；辦公室的通知才算
    assert say_after([{"kind": "notice", "who": "Kiro", "text": "Kiro 已登入，繼續工作"}]) == "寫程式中"
    assert say_after([{"kind": "notice", "text": "Kiro 已登入，繼續工作"}]) == "已登入，繼續工作"
    # 測試機：「未通過」不是通過；模型補充說明不算結果
    scr = "(id) => window.Office.debug(id).scene.test"
    say_after([{"kind": "say", "role": "qa", "text": "【QA】這次未通過"}])
    assert page.evaluate(scr, rid) == "off"
    say_after([{"kind": "say", "role": "qa", "note": "model", "text": "【QA】測試關卡通過的說明"}])
    assert page.evaluate(scr, rid) == "off"
    say_after([{"kind": "say", "role": "qa", "text": "【QA】測試關卡沒過（第 1 次）：x"}])
    assert page.evaluate(scr, rid) == "red"
    say_after([{"kind": "say", "role": "qa", "text": "【QA】測試關卡通過"}])
    assert page.evaluate(scr, rid) == "green"

    # 說明卡跟著泡泡換內容；Esc 先只關卡片
    page.locator('.o-cell.room .o-who[data-actor="impl"]').hover()
    page.locator("#o-card").wait_for(state="visible", timeout=3000)
    page.evaluate(SCENE, [rid, build, {"events": tool("Write calc.py"), "items": False}])
    page.wait_for_function("() => document.querySelector('#o-card').textContent.includes('現在：寫 calc.py')", timeout=3000)
    # 滑鼠移到卡片上，卡片留著
    box = page.locator("#o-card").bounding_box()
    page.mouse.move(box["x"] + 10, box["y"] + 10)
    page.wait_for_timeout(500)  # 超過離開熱區後的 300 毫秒延遲
    assert page.locator("#o-card").is_visible()
    page.keyboard.press("Escape")
    assert page.locator("#o-card").is_hidden()

    # 雙擊像素人＝開特寫（不開抽屜）；單擊像素人＝開房間抽屜
    page.locator('.o-cell.room .o-who[data-actor="impl"]').dblclick()
    page.wait_for_selector(".o-xview", timeout=5000)
    assert page.locator("#office-drawer .drawer").count() == 0
    page.keyboard.press("Escape")
    if page.locator(".o-xview").count():
        page.keyboard.press("Escape")
    assert page.locator(".o-xview").count() == 0
    page.locator('.o-cell.room .o-who[data-actor="impl"]').click()
    page.wait_for_selector("#office-drawer .drawer", timeout=5000)
    page.keyboard.press("Escape")
    # 觸控：點一下立刻開抽屜
    page.evaluate("""() => {
      const b = document.querySelector('.o-hit[data-room]');
      b.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'touch', bubbles: true }));
      b.dispatchEvent(new PointerEvent('click', { pointerType: 'touch', detail: 1, bubbles: true }));
    }""")
    assert page.locator("#office-drawer .drawer").count() == 1
    page.keyboard.press("Escape")
    assert errors == []


def test_reduced_motion_has_no_animation_loop(srv, browser):  # noqa: F811
    _, res = J(srv, "/api/team/demo", {"autonomy": "careful"})
    rid = res["room"]["id"]
    ctx = browser.new_context(viewport={"width": 1366, "height": 768}, reduced_motion="reduce")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:%d/?t=%s" % (srv.port, TOKEN))
    page.wait_for_selector(".o-rail .o-item.k-approve_spec", timeout=30000)
    page.evaluate(SCENE, [rid, {"status": "running", "phase": "build", "persona": "scrum"},
                          {"events": [{"kind": "tool", "text": "Write calc.py", "who": "Kiro"}], "items": False}])
    # 等 20 個顯示畫格：降低動態時一格都不該畫
    page.evaluate("() => new Promise((ok) => { let n = 0; const f = () => (++n >= 20 ? ok() : requestAnimationFrame(f)); requestAnimationFrame(f); })")
    dbg = page.evaluate("window.Office.debug()")
    assert dbg["frames"] == 0 and dbg["loop"] == "off"
    # 泡泡照樣顯示（靜態）
    assert "calc.py" in page.locator('.o-cell.room .o-who[data-actor="impl"] .o-bub').inner_text()
    # 有動態時迴圈會跑
    page2 = browser.new_page(viewport={"width": 1366, "height": 768})
    page2.goto("http://127.0.0.1:%d/?t=%s" % (srv.port, TOKEN))
    page2.wait_for_selector(".o-plate", timeout=10000)
    page2.evaluate(SCENE, [rid, {"status": "running", "phase": "build", "persona": "scrum"}, {"events": [], "items": False}])
    page2.wait_for_function("() => window.Office.debug().frames > 0", timeout=5000)
    # 特寫開著時總覽暫停：只有特寫在畫
    page2.locator(".o-hit[data-room]").first.focus()
    page2.keyboard.press("Enter")
    page2.wait_for_selector(".o-xview", timeout=5000)
    assert page2.evaluate("window.Office.debug().loop") == "on"
    ctx.close()
    assert errors == []


def test_pick_models_in_wizard_and_room_drawer(msrv, browser, tmp_path):  # noqa: F811
    from test_server_team import KIRO_IDS
    app = msrv
    app.gate.set()  # 讓啟動時的 kiro 偵測完成
    assert wait(lambda: J(app, "/api/team/models")[1]["seats"]["kiro"]["known"], 10)
    page, errors = open_console(browser, app)
    repo = str(tmp_path / "pickrepo")
    git_repo(repo)
    page.locator(".o-dispatch").first.click()
    page.locator("#o-path").fill(repo)
    page.get_by_role("button", name="下一步：檢查資料夾").click()
    page.wait_for_selector("#o-goal", timeout=10000)
    page.get_by_role("button", name="下一步：陣容").click()
    impl = page.locator("#o-impl")
    auto_label = J(app, "/api/team/models")[1].get("auto_label") or "自動（Decision-1 決定）"
    assert impl.locator("option").first.inner_text() == auto_label
    values = impl.locator("option").evaluate_all("os => os.map(o => o.value)")
    assert "kiro|" + KIRO_IDS[1] in values and "claude|" in values
    assert not any(v.startswith("agy|") for v in values)  # 沒安裝的座位不列
    impl.select_option("kiro|" + KIRO_IDS[1])
    page.locator("#o-rev").select_option("claude|")
    page.screenshot(path="/tmp/office5-wizard-models.png")
    page.get_by_role("button", name="下一步：確認").click()
    assert "Kiro・%s" % KIRO_IDS[1] in page.locator("#office-drawer").inner_text()
    with page.expect_request(lambda r: r.url.endswith("/api/team/rooms") and r.method == "POST", timeout=10000) as req:
        page.locator("#office-drawer").get_by_role("button", name="開工").click()
    body = json.loads(req.value.post_data)
    assert body["impl_cli"] == "kiro" and body["impl_model"] == KIRO_IDS[1]
    assert body["review_cli"] == "claude" and body["review_model"] == ""
    page.wait_for_selector("#office-drawer .drawer", state="detached", timeout=15000)
    rid = [r for r in J(app, "/api/team/state")[1]["rooms"] if not r["demo"]][0]["id"]
    assert J(app, "/api/team/state")[1]["rooms"][0]["impl_pref"] == {"seat": "kiro", "model": KIRO_IDS[1]}

    # 房間抽屜：目前設定、換模型（下一回合生效）、錯誤一直留著
    page.locator('.o-hit[data-room="%s"]' % rid).click()
    pick = page.locator("#office-drawer .o-mpick")
    pick.wait_for(timeout=10000)
    assert "實作：Kiro・%s" % KIRO_IDS[1] in pick.inner_text()
    page.locator("#o-m-impl").select_option("kiro|" + KIRO_IDS[2])
    page.locator('#office-drawer [data-role="impl"]').get_by_role("button", name="套用").click()
    page.wait_for_function("() => document.querySelector('#office-drawer [data-role=\"impl\"] .o-out').textContent.includes('下一回合生效')",
                           timeout=10000)
    assert wait(lambda: J(app, "/api/team/state")[1]["rooms"][0]["impl_pref"]["model"] == KIRO_IDS[2], 5)
    page.wait_for_function("(m) => document.querySelector('#office-drawer [data-role=\"impl\"] .o-mnow').textContent.includes(m)",
                           arg=KIRO_IDS[2], timeout=10000)
    page.locator("#o-m-impl").select_option("claude|")  # 跟審查同一個座位 → 伺服器拒絕
    page.locator('#office-drawer [data-role="impl"]').get_by_role("button", name="套用").click()
    err = page.locator('#office-drawer [data-role="impl"] .o-err')
    err.wait_for(timeout=10000)
    assert "不同的座位" in err.inner_text()
    page.evaluate(INJECT, {"context_pct": 12})
    page.evaluate(INJECT, {"context_pct": 13})
    assert "不同的座位" in page.locator('#office-drawer [data-role="impl"] .o-err').inner_text()
    page.wait_for_timeout(300)  # 抽屜滑入動畫
    page.screenshot(path="/tmp/office5-room-models.png")
    page.keyboard.press("Escape")

    # 示範房：不能換模型
    demo = J(app, "/api/team/demo", {"autonomy": "careful"})[1]["room"]["id"]
    page.wait_for_selector('.o-hit[data-room="%s"]' % demo, timeout=10000)
    page.locator('.o-hit[data-room="%s"]' % demo).click()
    page.wait_for_selector("#office-drawer .o-mpick", timeout=10000)
    assert "示範房使用假代理，不能換模型" in page.locator("#office-drawer .o-mpick").inner_text()
    assert page.locator("#o-m-impl").count() == 0
    assert errors == []


def test_timeline_rows_expand_to_details(srv, browser):  # noqa: F811
    """工具（指令＋輸出，合併成一列）、模型呼叫（題目＋答案）、督促（原文＋送出）都能展開；快照更新後仍展開。"""
    _, res = J(srv, "/api/team/demo", {})
    rid = res["room"]["id"]
    assert wait(lambda: next((r for r in J(srv, "/api/team/state")[1]["rooms"] if r["id"] == rid and r["status"] == "done"), None), 60)
    store = srv.team.store
    # 形狀同 runner 寫的事件（後端單元測試驗證 runner 真的這樣寫）
    key = "Kiro|7|sess-a|tc-1"
    store.event(rid, "tool", "Run Command", who="Kiro", tool_call_id="tc-1", tool_key=key,
                detail={"title": "Run Command", "kind": "execute", "input": '{\n "cwd": "."\n}'})
    long_path = "/very/long/" + "nested/" * 20 + "tests/test_calc.py"
    store.event(rid, "tool_update", "Run Command：更新", who="Kiro", tool_call_id="tc-1", tool_key=key,
                detail={"command": "python -m pytest -q tests/test_calc.py", "paths": [long_path]})
    store.event(rid, "tool_result", "Run Command：完成", who="Kiro", tool_call_id="tc-1", tool_key=key, status="completed",
                detail={"status": "completed", "output": "..\n2 passed in 0.03s"})
    # 別的對話裡同樣的 tc-1：不會併進來
    store.event(rid, "tool", "Read File", who="Kiro", tool_call_id="tc-1", tool_key="Kiro|8|sess-b|tc-1",
                detail={"title": "Read File", "kind": "read"})
    store.event(rid, "say", "【Scrum Master】請先完成 C1 的 mul，再 commit。", role="scrum", nudge=True,
                detail={"generated": "請先完成 C1 的 mul，再 commit。", "sent": "【Scrum Master】請先完成 C1 的 mul，再 commit。\n\n目標（全文）：\n在 calc.py 加入 mul"})
    srv.team._changed()
    # 快照與房間清單都不帶 detail（展開時才依事件 id 讀）
    snap = J(srv, "/api/team/state")[1]
    assert all("detail" not in e for r in snap["rooms"] for e in r["events"])
    lst = J(srv, "/api/team/room?id=" + rid)[1]["events"]
    assert len(lst) <= 100 and all("detail" not in e for e in lst if e.get("id"))
    page, errors = open_console(browser, srv)
    page.locator('.o-hit[data-room="%s"]' % rid).click()
    tool = page.locator("#office-drawer .o-tl li.k-tool").filter(has_text="Run Command").first
    tool.wait_for(timeout=10000)
    assert "完成" in tool.locator(".o-tstate").inner_text()
    assert page.locator("#office-drawer .o-tl li.k-tool_result").count() == 0  # 結果併進工具那一列
    assert page.locator("#office-drawer .o-tl li.k-tool_update").count() == 0
    assert page.locator("#office-drawer .o-tl li.k-tool").filter(has_text="Read File").count() == 1  # 不同對話不合併
    tool.locator(".o-more > summary").click()
    box = tool.locator(".o-detail-box")
    box.wait_for(timeout=5000)  # 展開時才讀細節
    assert "python -m pytest -q tests/test_calc.py" in box.inner_text() and "2 passed in 0.03s" in box.inner_text()
    path = box.locator(".o-dpaths li").first
    assert path.get_attribute("title") == long_path and "…" in path.inner_text() and path.inner_text().endswith("tests/test_calc.py")
    # 快照更新後仍展開
    page.evaluate(INJECT, {"context_pct": 21})
    page.evaluate(INJECT, {"context_pct": 22})
    page.wait_for_function("() => [...document.querySelectorAll('#office-drawer .o-tl li.k-tool .o-more')].some(d => d.open)", timeout=5000)
    tool.scroll_into_view_if_needed()
    page.wait_for_function("() => !document.querySelector('#office-drawer .drawer').getAnimations().length")
    page.screenshot(path="/tmp/office6-tool.png")
    # 模型呼叫：題目與答案
    model = page.locator("#office-drawer .o-tl li.k-model").filter(has_text="檢查驗收條件品質").first
    model.locator(".o-more > summary").click()
    mbox = model.locator(".o-detail-box")
    mbox.wait_for(timeout=5000)
    assert "題目" in mbox.inner_text() and "答案" in mbox.inner_text() and "0.90" in mbox.inner_text()
    assert "結論" in mbox.inner_text()
    model.scroll_into_view_if_needed()
    page.screenshot(path="/tmp/office6-model.png")
    # 督促：模型原文＋實際送出
    nudge = page.locator("#office-drawer .o-tl li.k-say").filter(has_text="督促").first
    nudge.locator(".o-more > summary").click()
    nbox = nudge.locator(".o-detail-box")
    nbox.wait_for(timeout=5000)
    assert "模型產生的原文" in nbox.inner_text() and "目標（全文）" in nbox.inner_text()
    nudge.scroll_into_view_if_needed()
    page.screenshot(path="/tmp/office6-nudge.png")
    # 細節讀取失敗：顯示「點此重試」，之後的重畫不會自動重讀
    calls = []

    def fail(route):
        calls.append(1)
        route.fulfill(status=200, content_type="application/json", body='{"ok": false, "text": "暫時讀不到"}')
    page.route("**/api/team/room/events*", fail)
    other = page.locator("#office-drawer .o-tl li.k-prompt").first
    other.locator(".o-more > summary").click()
    retry = other.get_by_role("button", name="細節讀取失敗，點此重試")
    retry.wait_for(timeout=5000)
    page.evaluate(INJECT, {"context_pct": 31})
    page.evaluate(INJECT, {"context_pct": 32})
    page.wait_for_function("() => document.querySelector('#office-drawer .o-dretry')", timeout=5000)
    assert len(calls) == 1
    page.unroute("**/api/team/room/events*")
    page.locator("#office-drawer .o-dretry").first.click()
    other.locator(".o-detail-box").wait_for(timeout=5000)
    assert "送給代理的完整訊息" in other.locator(".o-detail-box").inner_text()
    page.keyboard.press("Escape")
    # 特寫的即時時間軸也能展開
    page.locator('.o-hit[data-room="%s"]' % rid).focus()
    page.keyboard.press("Enter")
    xt = page.locator(".o-xview .o-tl li.k-tool").filter(has_text="Run Command").first
    xt.wait_for(timeout=10000)
    assert xt.locator(".o-more").evaluate("d => d.open")  # 展開狀態跨畫面保留
    xt.locator(".o-detail-box").wait_for(timeout=5000)
    assert "2 passed" in xt.locator(".o-detail-box").inner_text()
    assert errors == []


def test_delegate_toggle_badge_and_decision_rows(srv, browser):  # noqa: F811
    """Task 20：房間抽屜頂部「代管：開／關」；開啟後驗收條件由代管決定（不進收件匣）、門牌「代管」徽章、
    實作的像素人頭上「AI 代管」、時間軸的決策列可展開看理由；關閉後恢復。"""
    _, res = J(srv, "/api/team/demo", {"autonomy": "balanced"})
    rid = res["room"]["id"]
    assert wait(lambda: next((i for i in J(srv, "/api/team/state")[1]["inbox"]
                              if i["room_id"] == rid and i["kind"] == "approve_spec"), None), 60)
    page, errors = open_console(browser, srv)
    page.locator('.o-hit[data-room="%s"]' % rid).click()
    btn = page.locator('#office-drawer [data-fk="ctl:delegate"]')
    btn.wait_for(timeout=5000)
    assert btn.inner_text() == "代管：關" and btn.get_attribute("aria-pressed") == "false"
    assert "合併與登入仍由你決定；危險指令與專案外路徑會直接拒絕" in page.locator("#office-drawer .o-delhint").inner_text()
    assert page.locator(".o-plate .o-deleg").count() == 0
    btn.click()
    page.wait_for_function("() => document.querySelector('#office-drawer [data-fk=\"ctl:delegate\"]').textContent === '代管：開'",
                           timeout=10000)
    assert page.locator('#office-drawer [data-fk="ctl:delegate"]').get_attribute("aria-pressed") == "true"
    page.wait_for_selector(".o-plate .o-deleg", timeout=5000)
    assert page.locator(".o-plate .o-deleg").first.inner_text() == "代管・示範"
    page.wait_for_function("() => [...document.querySelectorAll('.o-who[data-actor=\"impl\"] .o-wdel')].some(e => !e.classList.contains('hidden'))",
                           timeout=10000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.screenshot(path="/tmp/office7-delegate-grid.png")
    page.locator('.o-hit[data-room="%s"]' % rid).click()
    page.locator('#office-drawer [data-fk="ctl:delegate"]').wait_for(timeout=5000)
    # 驗收條件由代管決定，房間一路跑到等你合併（合併仍交給你）
    assert wait(lambda: next((i for i in J(srv, "/api/team/state")[1]["inbox"]
                              if i["room_id"] == rid and i["kind"] == "merge"), None), 60)
    st = J(srv, "/api/team/state")[1]
    assert {i["kind"] for i in st["inbox"] if i["room_id"] == rid} == {"merge"}
    row = page.locator("#office-drawer .o-tl li.k-decision").filter(has_text="驗收條件通過").first
    row.wait_for(timeout=10000)
    assert "代管・示範" in row.locator(".o-mtag").inner_text()
    row.locator(".o-more > summary").click()
    box = row.locator(".o-detail-box")
    assert "開工" in box.inner_text() and "各模型的答案" in box.inner_text()
    row.scroll_into_view_if_needed()
    page.wait_for_function("() => !document.querySelector('#office-drawer .drawer').getAnimations().length")
    page.screenshot(path="/tmp/office7-delegate.png")
    # 關閉代管：恢復原狀
    page.locator('#office-drawer [data-fk="ctl:delegate"]').click()
    page.wait_for_function("() => document.querySelector('#office-drawer [data-fk=\"ctl:delegate\"]').textContent === '代管：關'",
                           timeout=10000)
    page.wait_for_function("() => !document.querySelector('.o-plate .o-deleg')", timeout=5000)
    assert J(srv, "/api/team/room?id=" + rid)[1]["room"]["delegate"] is False
    assert errors == []
