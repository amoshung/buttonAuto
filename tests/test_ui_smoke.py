"""介面冒煙測試（D13）：主視窗與工作階段詳情能建立、更新、處理事件，不連 AI、不碰任何 IDE。

同一個行程裡重複建立 tk.Tk() 在某些 Python 發行版會失敗，所以整個檔案共用一個主視窗。
"""
import tkinter as tk

import pytest

import automation
import overlay_ui
import win_utils as wu
from projects import ProjectWindow
from sessions import SessionStore


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    mp = pytest.MonkeyPatch()
    mp.setattr(automation, "SessionStore", lambda: SessionStore(root=str(tmp / "sessions")))
    mp.setattr(automation.AutomationWorker, "start", lambda self: None)  # 不啟動背景執行緒
    mp.setattr(wu, "notify", lambda *a, **k: True)
    try:
        root = tk.Tk()
    except tk.TclError as e:
        mp.undo()
        pytest.skip(f"沒有可用的顯示環境：{e}")
    root.withdraw()
    a = overlay_ui.FloatingOverlayApp(root, config_path=str(tmp / "config.json"),
                                      secrets_path=str(tmp / "secrets.json"))
    root.update()
    yield a
    a._on_close()
    mp.undo()


def test_main_window_builds_and_starts_on_screen(app):
    app.root.update_idletasks()
    x, y = app.root.winfo_x(), app.root.winfo_y()
    l, t, r, b = wu.work_area_at(x + 10, y + 10)
    assert l <= x and y >= t and x + app.root.winfo_width() <= r + 1 and y + 40 <= b
    assert overlay_ui.CODE_VERSION in app.root.title()


def test_session_dialog_and_events(app, tmp_path):
    pw = ProjectWindow(1, "kiro", "Kiro", "Kiro - proj", "proj", "", str(tmp_path), False, False)
    sess = app.worker.store.get(pw.key, pw.app_label, pw.project, pw.path, pw.remote)
    sess.update(lambda st: setattr(st, "goal", "加入 CSV 匯出"))
    sess.history("observe", agent_state="waiting_input", summary="做完第一段")
    dlg = overlay_ui.SessionDialog(app, pw)
    app.root.update()
    assert "做完第一段" in dlg.events_lbl.cget("text")
    assert dlg.goal_text.get("1.0", "end").strip() == "加入 CSV 匯出"
    dlg.reload(full=False)
    summary = tmp_path / "daily.md"
    summary.write_text("x", encoding="utf-8")
    app.events.put({"type": "summary", "path": str(summary)})
    app.events.put({"type": "humans", "items": []})
    app.events.put({"type": "session", "key": pw.key, "text": "測試狀態"})
    app._drain_events()
    assert "daily.md" in app.summary_lbl.cget("text")
    dlg.win.destroy()
