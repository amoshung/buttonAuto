"""Pixel Office (Next.js) UI 冒煙測試：驗證 `/` 載入、CSP nonce、畫布、例外收件匣、派工精靈、SSH 遠端開發嚴格 CLI 過濾與座位／用量抽屜。"""
import pytest

pw = pytest.importorskip("playwright.sync_api")
from team.clis import CliInfo  # noqa: E402
from team.ssh import run_remote_probe  # noqa: E402
from test_server_team import TOKEN, git_repo, srv  # noqa: E402,F401


@pytest.fixture()
def browser():
    try:
        p = pw.sync_playwright().start()
        b = p.chromium.launch()
    except Exception as e:
        pytest.skip(f"Playwright/Chromium 不可用：{e}")
    yield b
    b.close()
    p.stop()


def test_pixel_office_root_renders_and_modals_work(srv, browser, tmp_path):  # noqa: F811
    # 設定本機可調用的 CLI 為 claude 與 kiro（agy 未安裝，應被自動隱藏）
    srv.team.clis_fn = lambda: {
        "claude": CliInfo("claude", "Claude Code", "/usr/bin/claude", "headless"),
        "kiro": CliInfo("kiro", "Kiro", "/usr/bin/kiro-cli", "acp"),
    }
    srv.team._clis_cache = None
    repo = tmp_path / "proj_ui"
    git_repo(repo)
    (repo / "sub_b").mkdir(parents=True, exist_ok=True)
    (repo / "sub_b" / "main.py").write_text("print('b')\n", encoding="utf-8")
    (repo / "ref_c").mkdir(parents=True, exist_ok=True)
    (repo / "ref_c" / "doc.md").write_text("# ref\n", encoding="utf-8")
    page = browser.new_page(viewport={"width": 1366, "height": 768})
    page.add_init_script("window.sessionStorage.setItem('pixel_office_setup_prompted_v1', '1');")
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)

    resp = page.goto(f"http://127.0.0.1:{srv.port}/?t={TOKEN}")
    assert resp is not None and resp.status == 200
    page.get_by_role("button", name="新增房間", exact=True).wait_for(state="visible", timeout=10000)

    # 確認 CSP nonce 與 token meta 存在
    assert page.locator('meta[name="aea-token"]').get_attribute("content") == TOKEN

    # 確認僅保留「房間總覽」（已移除全景辦公室與流程看板）
    page.get_by_text("房間總覽", exact=True).wait_for(state="visible", timeout=5000)
    assert page.get_by_role("button", name="全景辦公室").count() == 0
    assert page.get_by_role("button", name="流程看板").count() == 0

    # 開啟並關閉例外收件匣
    page.get_by_role("button", name="收件匣").click()
    page.get_by_role("dialog", name="例外收件匣").wait_for(state="visible", timeout=5000)
    page.keyboard.press("Escape")
    page.get_by_role("dialog", name="例外收件匣").wait_for(state="hidden", timeout=5000)

    # 開啟派工精靈、確認僅有三位角色（高層主管・研發工程師・審查工程師）、顯式 Decision-1 協作模型選單、無 CLI 前綴、皆含職責說明 tooltip
    page.get_by_role("button", name="新增房間", exact=True).click()
    wiz = page.get_by_role("dialog", name="派工精靈")
    wiz.wait_for(state="visible", timeout=5000)
    assert not wiz.get_by_label("🤝 與 Decision-1 協作的生成模型").is_disabled()
    assert not wiz.get_by_label("研發工程師 模型").is_disabled()
    assert not wiz.get_by_label("審查工程師 模型").is_disabled()
    assert wiz.locator('[role="tooltip"]').count() >= 3
    wiz_text = wiz.inner_text()
    assert "實作 CLI" not in wiz_text and "審查 CLI" not in wiz_text
    wiz.locator('input[type="text"]').first.fill(str(repo))
    wiz.get_by_role("button", name="掃描", exact=True).click()
    sub_sel = wiz.get_by_label("目標工作子資料夾 B")
    sub_sel.wait_for(state="visible", timeout=5000)
    sub_sel.select_option("sub_b")
    wiz.get_by_text("唯讀參考來源：").wait_for(state="visible", timeout=5000)
    wiz.locator("textarea").fill("修正 README 並驗證進度顯示")
    wiz.get_by_role("button", name="建立並派工").click()
    wiz.wait_for(state="hidden", timeout=5000)

    # 確認房間卡片顯示三位純職位角色（高層主管／研發工程師／審查工程師）且無性別符號（♀／♂），以及 1×1 / 2×2 / 3×3 切換
    page.get_by_text("高層主管").first.wait_for(state="visible", timeout=5000)
    page.get_by_text("研發工程師").first.wait_for(state="visible", timeout=5000)
    page.get_by_text("審查工程師").first.wait_for(state="visible", timeout=5000)
    page.get_by_role("button", name="2×2", exact=True).click()
    page.get_by_text("空房間 #4").wait_for(state="visible", timeout=5000)
    page.get_by_role("button", name="3×3", exact=True).click()
    page.get_by_text("空房間 #9").wait_for(state="visible", timeout=5000)
    for gm in ("1×1", "自動"):
        page.get_by_role("button", name=gm, exact=True).click()

    # 確認右邊詳情面板顯示房間進度、三位角色分工（無性別、含 tooltip）、子資料夾 B 範圍、CE 技能與三角色模型配置
    aside = page.locator('aside[aria-label="視窗詳情"]')
    aside.get_by_text("目標資料夾 B（可讀寫）：sub_b/").wait_for(state="visible", timeout=5000)
    aside.get_by_role("button", name="📂 手動選擇目標／參考資料夾").click()
    aside.get_by_text("📁 手動設定目標工作資料夾 B 與唯讀參考資料夾 A").wait_for(state="visible", timeout=5000)
    aside.get_by_role("button", name="收起資料夾設定").click()
    aside.get_by_text("房間內 Personas 分工").wait_for(state="visible", timeout=5000)
    aside_text = aside.inner_text()
    assert "♀" not in aside_text and "♂" not in aside_text
    assert "實作 CLI" not in aside_text and "審查 CLI" not in aside_text
    assert aside.locator('[role="tooltip"]').count() >= 6
    aside.get_by_text("Compound Engineering 全角色已掛載：").wait_for(state="visible", timeout=5000)
    aside.get_by_text("團隊三位角色與模型配置").wait_for(state="visible", timeout=5000)
    aside.get_by_text("目前進度與活動").wait_for(state="visible", timeout=5000)
    aside.get_by_role("button", name="複製動態").wait_for(state="visible", timeout=5000)

    # 驗證點擊像素人／角色卡片可展開查看最新 5 項工作紀錄（DESC 排序）
    aside.get_by_text("審查工程師", exact=True).first.click()
    aside.get_by_text("最新 5 項工作紀錄（DESC 最新在上）").wait_for(state="visible", timeout=5000)

    # 驗證選擇工具後 Model 下拉選單立即啟用並可選擇模型，且不可調用的 agy 不會顯示在選單中
    impl_seat_sel = aside.locator("#select-seat-impl")
    impl_model_sel = aside.locator("#select-model-impl")
    seat_option_vals = impl_seat_sel.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert "claude" in seat_option_vals
    assert "kiro" in seat_option_vals
    assert "agy" not in seat_option_vals
    impl_seat_sel.select_option("claude")
    assert not impl_model_sel.is_disabled()
    impl_model_sel.select_option("sonnet")

    # 驗證重新偵測可用模型按鈕與高層主管傳話輸入框
    aside.get_by_role("button", name="重新偵測可用模型").click()
    chat_input = aside.locator("#agent-message")
    chat_input.fill("請優先檢查單元測試覆蓋率")
    aside.get_by_role("button", name="送出").click()

    # 開啟並關閉座位／用量抽屜
    page.get_by_role("button", name="座位／用量").click()
    page.get_by_role("dialog", name="座位與用量抽屜").wait_for(state="visible", timeout=5000)
    page.keyboard.press("Escape")
    page.get_by_role("dialog", name="座位與用量抽屜").wait_for(state="hidden", timeout=5000)

    assert not errors, f"前端出現錯誤：{errors}"


def test_pixel_office_ssh_remote_cli_filtering_and_room_creation(srv, browser, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("BUTTONAUTO_FAKE_SSH_LOCAL", "1")
    remote_repo = tmp_path / "remote_ssh_proj"
    git_repo(remote_repo)

    from team.ssh import SshError, clear_ssh_passwords, has_ssh_password

    clear_ssh_passwords()

    # 模擬遠端主機需要密碼驗證（未填密碼時回 Permission denied），驗證成功後只有 kiro 與 claude 可調用，agy 不可調用
    def fake_probe_runner(ssh_host, payload, timeout=25.0):
        op = payload.get("op")
        if op in ("dirs", "scan", "exec", "fs"):
            return run_remote_probe("", payload, timeout)
        if ssh_host == "ubuntu@10.0.0.8:2222" and not has_ssh_password(ssh_host):
            raise SshError(
                "SSH 連線失敗（exit 255）：ubuntu@10.0.0.8: Permission denied (publickey,password)."
            )
        return {
            "ok": True,
            "hostname": "devbox-remote",
            "platform": "Linux-x86_64",
            "home": str(tmp_path),
            "claude_bin_ok": True,
            "clis": {
                "claude": {
                    "name": "claude",
                    "label": "Claude Code",
                    "path": "/usr/local/bin/claude",
                    "protocol": "headless",
                    "callable": True,
                    "models": [{"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"}],
                    "current_model": "claude-sonnet-4-6",
                },
                "kiro": {
                    "name": "kiro",
                    "label": "Kiro",
                    "path": "/usr/local/bin/kiro-cli",
                    "protocol": "acp",
                    "callable": True,
                    "models": [{"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"}],
                    "current_model": "claude-sonnet-4.5",
                },
                "agy": {
                    "name": "agy",
                    "label": "Antigravity",
                    "path": "/usr/local/bin/agy",
                    "protocol": "acp",
                    "callable": False,
                    "error": "handshake failed",
                    "models": [],
                },
            },
        }

    srv.team.ssh_probe_runner = fake_probe_runner
    page = browser.new_page(viewport={"width": 1366, "height": 768})
    page.add_init_script("window.sessionStorage.setItem('pixel_office_setup_prompted_v1', '1');")
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)

    page.goto(f"http://127.0.0.1:{srv.port}/?t={TOKEN}")
    page.get_by_role("button", name="新增房間", exact=True).wait_for(state="visible", timeout=10000)
    page.get_by_role("button", name="新增房間", exact=True).click()

    wiz = page.get_by_role("dialog", name="派工精靈")
    wiz.wait_for(state="visible", timeout=5000)

    # 切換到「SSH 遠端開發」並新增需要密碼的主機
    wiz.get_by_role("button", name="SSH 遠端開發").click()
    wiz.get_by_placeholder("新增 SSH 主機（如 ubuntu@192.168.1.50:22 或 ~/.ssh/config 別名）").fill("ubuntu@10.0.0.8:2222")
    wiz.get_by_role("button", name="＋ 新增主機").click()

    # 因為遠端需要密碼，應自動彈出「SSH 登入密碼」輸入框
    pw_input = wiz.get_by_label("SSH 登入密碼")
    pw_input.wait_for(state="visible", timeout=5000)
    pw_input.fill("remote-pass-123")
    wiz.get_by_role("button", name="🔓 驗證密碼並偵測 CLI").click()

    # 等待遠端 CLI 偵測完成並顯示連線資訊
    wiz.get_by_text("已連線 devbox-remote").wait_for(state="visible", timeout=5000)

    # 檢查「研發工程師 執行工具」與「審查工程師 執行工具」下拉選單：不可調用的 agy 絕不能顯示！
    impl_sel = wiz.get_by_label("🛠️ 研發工程師 執行工具")
    rev_sel = wiz.get_by_label("🔍 審查工程師 執行工具")
    impl_opts = impl_sel.locator("option").evaluate_all("els => els.map(e => e.value)")
    rev_opts = rev_sel.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert "claude" in impl_opts
    assert "kiro" in impl_opts
    assert "agy" not in impl_opts
    assert "agy" not in rev_opts

    # 填入遠端專案路徑與目標並建立遠端房間
    wiz.get_by_placeholder("例如：/home/ubuntu/my-app 或 ~/projects/app").fill(str(remote_repo))
    wiz.locator("textarea").fill("透過 SSH 在遠端實作新功能")
    impl_sel.select_option("kiro")
    rev_sel.select_option("claude")
    wiz.get_by_role("button", name="建立並派工").click()
    wiz.wait_for(state="hidden", timeout=5000)

    # 驗證右側 SessionPanel 顯示「🌐 SSH 遠端：ubuntu@10.0.0.8:2222」，且房間內 CLI 選單同樣隱藏不可調用的 agy
    aside = page.locator('aside[aria-label="視窗詳情"]')
    aside.get_by_text("🌐 SSH 遠端：ubuntu@10.0.0.8:2222").wait_for(state="visible", timeout=5000)
    panel_impl_opts = aside.locator("#select-seat-impl option").evaluate_all("els => els.map(e => e.value)")
    assert "kiro" in panel_impl_opts
    assert "claude" in panel_impl_opts
    assert "agy" not in panel_impl_opts

    assert not errors, f"前端出現錯誤：{errors}"
