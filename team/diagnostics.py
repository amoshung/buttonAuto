"""停下說明 Markdown 與房間診斷包 ZIP（設計 §5.1.4、Task 10）。"""
import io
import json
import os
import platform
import subprocess
import sys
import zipfile
from typing import Any, Dict, List, Sequence

from team import events
from team.rooms import Room, RoomStore

STUCK_TAIL_LINES = 60

_SUGGESTIONS_BY_CAT = {
    "env": (
        "- **標記為環境既有失敗並繼續**：若這些失敗是因為缺少 git 外資料、外部服務或本機環境既有問題。\n"
        "- **提供資訊**：補充環境設定或測試資料位置。\n"
        "- **修改驗收條件**：將依賴外部環境的條件改為 `env_dependent: true` 或調整檢查指令。"
    ),
    "check": (
        "- **修改驗收條件**：檢查指令（check）本身有誤或與專案現況不符，請直接修正條件後繼續。\n"
        "- **提供資訊**：告訴實作者正確的驗證方式。"
    ),
    "unclear": (
        "- **修改驗收條件**：釐清模糊或互相矛盾的條件描述。\n"
        "- **提供資訊**：補充具體的產品行為決定。"
    ),
    "review": (
        "- **修改驗收條件**：若審查要求超出原本範圍，可明確界定條件。\n"
        "- **提供資訊**：指示實作者或審查者如何取捨。"
    ),
    "code": (
        "- **提供資訊**：提示關鍵檔案、演算法或修法方向，讓代理回到 L1 繼續嘗試。\n"
        "- **修改驗收條件**：把過大的條件拆小或調整範圍。"
    ),
    "system": (
        "- **提供資訊**：確認 CLI 狀態、權限或系統設定後讓房間繼續。\n"
        "- **封存房間**：若目前已不需要此任務。"
    ),
}


def stuck_markdown(
    room: Room,
    reason: str,
    last_output: str,
    diagnosis: Dict[str, Any],
    secrets: Sequence[str] = (),
) -> str:
    """產生可直接複製去問別人的 Markdown 停下說明（全文遮蔽金鑰，錯誤輸出最多保留最後 60 行）。"""
    rounds = list(room.rounds or [])
    last_round = rounds[-1] if rounds else {}
    passed = set(last_round.get("criteria_passed") or [])
    failing_cids = [
        str(c.get("id") or "?")
        for c in (room.criteria or [])
        if str(c.get("id") or "?") not in passed
        and (c.get("check") or "").strip().lower() not in ("", "manual")
        and not bool(c.get("env_dependent"))
    ]
    new_fails = list(last_round.get("new_failures") or [])
    fp = str(last_round.get("fingerprint") or "")
    fp_count = sum(1 for r in rounds if fp and r.get("fingerprint") == fp) if fp else 0

    ladder_data = room.ladder if isinstance(room.ladder, dict) else {}
    hist = list(ladder_data.get("history") or [])
    tried = list(ladder_data.get("tried") or [])

    diag = diagnosis if isinstance(diagnosis, dict) else {}
    cat = str(diag.get("category") or "system")
    ev_lines = [str(x) for x in (diag.get("evidence") or []) if str(x).strip()]
    action = str(diag.get("action") or "").strip()
    unsure = [str(x) for x in (diag.get("unsure") or []) if str(x).strip()]

    tail_lines = (last_output or "").splitlines()[-STUCK_TAIL_LINES:]
    tail_block = "\n".join(tail_lines) if tail_lines else "（無錯誤輸出紀錄）"

    hist_lines = [
        "- %s：%s → %s（%s）" % (h.get("at", ""), h.get("from", "?"), h.get("to", "?"), h.get("reason", ""))
        for h in hist
    ] or ["- （無階梯升級紀錄）"]
    if tried:
        hist_lines.append("- 已嘗試過的修法：" + "；".join(tried))

    diag_lines = [
        "- 分類：`%s`" % cat,
        "- 建議動作：%s" % (action or "請人工確認"),
    ]
    if ev_lines:
        diag_lines.append("- 證據行：\n" + "\n".join("  - `%s`" % ln for ln in ev_lines))
    if unsure:
        diag_lines.append("- 待確認事項：" + "；".join(unsure))

    sugg = _SUGGESTIONS_BY_CAT.get(cat, _SUGGESTIONS_BY_CAT["code"])
    if "預算" in (reason or ""):
        sugg = "- **再給一份預算**：目前設定的預算已用完，按此加倍預算並繼續。\n" + sugg

    parts = [
        "# 「%s」停下來等你（%s）" % (room.name or room.slug, reason or "方法用盡"),
        "",
        "## 卡在哪",
        "- 未通過條件：%s" % ("、".join(failing_cids) if failing_cids else "（無或全部已通過）"),
        "- 新增失敗測試：%s" % ("、".join(new_fails) if new_fails else "（無）"),
        "- 錯誤指紋：`%s`（重複 %d 次）" % (fp or "無", fp_count),
        "",
        "## 試過什麼",
        "\n".join(hist_lines),
        "",
        "## 診斷",
        "\n".join(diag_lines),
        "",
        "## 建議你做的決定",
        sugg,
        "",
        "## 房間",
        "- 房間名稱：%s（`%s`）" % (room.name or room.slug, room.id),
        "- 專案路徑：`%s`" % room.repo,
        "- 分支：`%s`（目標分支：`%s`）" % (room.branch or "未建立", room.target_branch or "主線"),
        "- 工作文件目錄：`%s`" % (room.notes_dir or "未設定"),
        "",
        "## 最近一次完整錯誤",
        "```text",
        tail_block,
        "```",
    ]
    return events.mask_text("\n".join(parts), secrets)


def _git_version() -> str:
    try:
        p = subprocess.run(
            ["git", "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        return (p.stdout or "").strip()
    except Exception:
        return "unknown"


def build_zip(store: RoomStore, room_id: str, secrets: Sequence[str] = ()) -> bytes:
    """打包房間診斷包 ZIP（所有內容皆經 mask_text / mask_detail 遮蔽金鑰）。"""
    room = store.get(room_id)
    if room is None:
        raise KeyError(room_id)
    room_dir = store._dir(room_id)
    evs = list(reversed(store.events(room_id, limit=500)))
    masked_evs = [events.mask_detail(e, secrets) for e in evs]
    model_calls = [e for e in masked_evs if e.get("kind") == "model"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        room_dict = events.mask_detail(json.loads(json.dumps(room.__dict__, ensure_ascii=False, default=str)), secrets)
        zf.writestr("room.json", json.dumps(room_dict, ensure_ascii=False, indent=2))
        zf.writestr(
            "events.jsonl",
            "\n".join(json.dumps(e, ensure_ascii=False) for e in masked_evs) + ("\n" if masked_evs else ""),
        )
        zf.writestr(
            "baseline.json",
            json.dumps(events.mask_detail(room.baseline or {}, secrets), ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "model_calls.json",
            json.dumps(model_calls, ensure_ascii=False, indent=2),
        )
        env_txt = "\n".join(
            [
                "os=%s (%s)" % (os.name, platform.platform()),
                "python=%s" % sys.version.replace("\n", " "),
                "git=%s" % _git_version(),
                "env_accepted=%s" % json.dumps(room.env_accepted or [], ensure_ascii=False),
            ]
        )
        zf.writestr("env.txt", events.mask_text(env_txt, secrets))

        # gate logs
        for log_root in (os.path.join(room_dir, "logs"), os.path.join(room.notes_dir, "logs") if room.notes_dir else ""):
            if not log_root or not os.path.isdir(log_root):
                continue
            for fn in sorted(os.listdir(log_root)):
                if fn.startswith("gate-") and fn.endswith(".txt"):
                    full = os.path.join(log_root, fn)
                    if os.path.isfile(full):
                        try:
                            with open(full, "r", encoding="utf-8", errors="replace") as f:
                                zf.writestr("logs/%s" % fn, events.mask_text(f.read(), secrets))
                        except OSError:
                            pass

        # notes
        notes_roots = [p for p in (room.notes_dir, os.path.join(room_dir, "notes")) if p and os.path.isdir(p)]
        seen_notes = set()
        for nr in notes_roots:
            for dirpath, _dirnames, filenames in os.walk(nr):
                rel_dir = os.path.relpath(dirpath, nr)
                if rel_dir.startswith("logs"):
                    continue
                for fn in filenames:
                    arc = ("notes/%s" % fn) if rel_dir == "." else ("notes/%s/%s" % (rel_dir.replace("\\", "/"), fn))
                    if arc in seen_notes:
                        continue
                    seen_notes.add(arc)
                    full = os.path.join(dirpath, fn)
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as f:
                            zf.writestr(arc, events.mask_text(f.read(), secrets))
                    except OSError:
                        pass
    return buf.getvalue()
