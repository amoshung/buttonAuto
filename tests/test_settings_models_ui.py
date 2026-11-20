"""設定頁「模型」區（Task 18，Playwright）：偵測中、選座位模型、標等級、判斷用途、存檔、重新偵測。

沒有 Playwright 或 Chromium 時整個檔案略過。伺服器用 test_server_team.py 的 msrv fixture（kiro 偵測是注入的假函式）。
"""
import json

import pytest

pw = pytest.importorskip("playwright.sync_api")

from test_server_team import KIRO_IDS, TOKEN, J, msrv, wait  # noqa: E402,F401  （msrv 是 fixture）


@pytest.fixture()
def browser():
    try:
        p = pw.sync_playwright().start()
    except Exception as e:  # pragma: no cover
        pytest.skip("Playwright 無法啟動：%s" % e)
    try:
        b = p.chromium.launch()
    except Exception as e:  # pragma: no cover
        p.stop()
        pytest.skip("Chromium 無法啟動：%s" % e)
    yield b
    b.close()
    p.stop()


def saved_models(tmp_path):
    return json.loads((tmp_path / "team_models.json").read_text(encoding="utf-8"))["models"]


def test_settings_models_save_and_redetect(msrv, browser, tmp_path):  # noqa: F811
    app = msrv
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
    page.goto("http://127.0.0.1:%d/?t=%s" % (app.port, TOKEN))
    page.locator('.nav button[data-view="settings"]').click()
    # 啟動時的偵測還在進行（假函式卡在 gate）：顯示偵測中、按鈕停用、只能選自動
    page.wait_for_selector('.m-seat[data-seat="kiro"]')
    kiro = page.locator('.m-seat[data-seat="kiro"]')
    assert "偵測中" in kiro.inner_text()
    assert kiro.locator("button[data-detect]").is_disabled()
    assert kiro.locator("#m-seat-kiro option").count() == 1
    app.gate.set()
    page.wait_for_function("() => document.querySelectorAll('#m-seat-kiro option').length === %d" % (len(KIRO_IDS) + 1),
                           timeout=10000)
    assert "CLI 目前用 auto" in kiro.inner_text()
    # 未安裝的 agy 不能偵測；glm 固定
    assert "未安裝" in page.locator('.m-seat[data-seat="agy"]').inner_text()
    assert "固定用 glm-5.3" in page.locator('.m-seat[data-seat="glm"]').inner_text()
    # 選模型、標等級、選用途、加 Claude 完整 id
    page.select_option("#m-seat-kiro", "claude-sonnet-5.5")
    # Kiro／Antigravity：自動派工不會選 → 沒有等級表，只有說明；「自動」選項寫明只在 Claude／GLM 中選
    assert kiro.locator("details.m-tiers").count() == 0
    assert "只在你指定時使用（自動派工不會選）" in kiro.inner_text()
    assert kiro.locator("#m-seat-kiro option").first.inner_text() == "自動（Decision-1 在 Claude／GLM 中選）"
    claude_seat = page.locator('.m-seat[data-seat="claude"]')
    row = claude_seat.locator("tr", has_text="sonnet")
    row.locator("input.m-cost").fill("4")
    page.select_option("#m-purpose-notes", "off")
    page.select_option("#m-purpose-permission", "glm")
    claude = page.locator('.m-seat[data-seat="claude"]')
    claude.locator(".m-add input").fill("claude-opus-4-1-20250805")
    claude.locator(".m-add button").click()
    page.select_option("#m-seat-claude", "claude-opus-4-1-20250805")
    page.locator("#models-save").click()
    page.wait_for_selector("#models-result .ok")
    v = J(app, "/api/team/models")[1]
    assert v["seats"]["kiro"]["pref"] == "claude-sonnet-5.5" and v["seats"]["claude"]["pref"] == "claude-opus-4-1-20250805"
    assert v["purposes"]["notes"] == "off" and v["purposes"]["permission"] == "glm"
    sonnet = [m for m in v["seats"]["claude"]["models"] if m["id"] == "sonnet"][0]
    assert sonnet["tier"] == "standard" and sonnet["cost"] == 4
    opus = [m for m in v["seats"]["kiro"]["models"] if m["id"] == "claude-opus-5.5"][0]
    assert opus["inferred"] is True  # 沒動過的列不送：維持「推定」
    assert saved_models(tmp_path)["kiro"] == []
    assert v["auto_label"] == "自動（Decision-1 在 Claude／GLM 中選）" and v["seats"]["kiro"]["auto_eligible"] is False
    saved = json.loads((tmp_path / "team_models.json").read_text(encoding="utf-8"))
    assert saved["seats"] == {"kiro": "claude-sonnet-5.5", "claude": "claude-opus-4-1-20250805"}
    # 重新偵測：偵測中 → 完成；你的選擇還在
    app.gate.clear()
    kiro.locator("button[data-detect]").click()
    page.wait_for_function("() => document.querySelector('.m-seat[data-seat=\"kiro\"]').textContent.includes('偵測中')",
                           timeout=5000)
    assert wait(lambda: app.detect_calls.count("kiro") == 2, 5)
    page.screenshot(path="/tmp/settings-models-detecting.png", full_page=True)
    app.gate.set()
    page.wait_for_function("() => !document.querySelector('.m-seat[data-seat=\"kiro\"]').textContent.includes('偵測中')",
                           timeout=10000)
    assert page.locator("#m-seat-kiro").input_value() == "claude-sonnet-5.5"
    page.screenshot(path="/tmp/settings-models.png", full_page=True)
    assert errors == []
