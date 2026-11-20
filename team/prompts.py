"""角色提示詞三層儲存（room > global > default）、驗證、版本歷史（20 版）與升為全域（設計 §5.4.1）。"""
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from team import events
from team.prompt_defaults import (
    AUTO_ROLES,
    CONTRACTS,
    DEFAULTS,
    PLACEHOLDERS,
    ROLE_LABELS,
    ROLES,
    SAFETY,
)

PROMPT_MAX_CHARS = 8000
PROMPT_HISTORY = 20
SECTION_KEYS = ("identity", "principles", "checklist")

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")
_FORBIDDEN_WORDS = (
    "```json",
    '"status"',
    "回覆契約",
    "忽略以上",
    "ignore previous",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def validate(sections: Dict[str, Any]) -> List[str]:
    """驗證可編輯段落（identity、principles、checklist）；回傳錯誤訊息清單（空＝通過）。"""
    if not isinstance(sections, dict):
        return ["sections 必須是物件"]
    errs: List[str] = []
    unknown_keys = sorted(set(sections.keys()) - set(SECTION_KEYS))
    if unknown_keys:
        errs.append("不認得的段落欄位：%s" % "、".join(unknown_keys))
    combined_parts: List[str] = []
    for k in SECTION_KEYS:
        if k not in sections:
            continue
        v = sections[k]
        if not isinstance(v, str):
            errs.append("段落 %s 必須是字串" % k)
            continue
        combined_parts.append(v)
    combined = "\n".join(combined_parts)
    if len(combined) > PROMPT_MAX_CHARS:
        errs.append("提示詞總長度（%d 字）超過上限 %d 字" % (len(combined), PROMPT_MAX_CHARS))
    for m in _PLACEHOLDER_RE.finditer(combined):
        name = m.group(1)
        if name not in PLACEHOLDERS:
            errs.append("不認得的佔位符：%s" % name)
    if events.mask_text(combined) != combined:
        errs.append("內容含疑似金鑰，拒絕儲存")
    lower = combined.lower()
    for bad in _FORBIDDEN_WORDS:
        if bad.lower() in lower:
            errs.append("不可改寫回覆契約或包含覆寫指示（%s）" % bad)
            break
    return errs


def _merge_with_default(role: str, partial: Dict[str, str]) -> Dict[str, str]:
    base = dict(DEFAULTS.get(role) or {"identity": "", "principles": "", "checklist": ""})
    for k in SECTION_KEYS:
        if isinstance(partial.get(k), str) and partial[k].strip():
            base[k] = partial[k]
    return base


class PromptStore:
    def __init__(self, root: str):
        self.root = root
        self._lock = threading.RLock()
        os.makedirs(root, exist_ok=True)

    def _file_path(self, scope: str, room_id: str = "") -> str:
        if scope == "global":
            return os.path.join(self.root, "prompts.json")
        if scope == "room":
            if not room_id:
                raise ValueError("scope=room 需要提供 room_id")
            return os.path.join(self.root, room_id, "prompts.json")
        raise ValueError("不認得的 scope：%s" % scope)

    def _read(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path: str, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in ROLES:
            raise ValueError("不認得的角色：%s" % role)

    def resolve(self, role: str, room_id: str = "") -> Tuple[Dict[str, str], str]:
        """依優先序 room > global > default 回傳 (sections, version)。"""
        self._check_role(role)
        with self._lock:
            if room_id:
                r_data = self._read(self._file_path("room", room_id)).get(role)
                if isinstance(r_data, dict) and isinstance(r_data.get("current"), dict) and r_data.get("version"):
                    return _merge_with_default(role, r_data["current"]), str(r_data["version"])
            g_data = self._read(self._file_path("global")).get(role)
            if isinstance(g_data, dict) and isinstance(g_data.get("current"), dict) and g_data.get("version"):
                return _merge_with_default(role, g_data["current"]), str(g_data["version"])
            return dict(DEFAULTS[role]), "default"

    def save(
        self,
        role: str,
        sections: Dict[str, str],
        scope: str,
        room_id: str = "",
        kind: str = "room",
        note: str = "",
    ) -> str:
        """驗證並儲存提示詞；回傳新版本字串（global:vN / room:vN / auto:vN）。"""
        self._check_role(role)
        if scope not in ("global", "room"):
            raise ValueError("scope 必須是 global 或 room")
        if scope == "global":
            prefix = "global"
            kind = "global"
        else:
            if kind not in ("room", "auto"):
                raise ValueError("kind 必須是 room 或 auto")
            prefix = kind
        errs = validate(sections)
        if errs:
            raise ValueError("；".join(errs))
        with self._lock:
            # 以目前有效版本為基底合併未傳入的段落
            base_sec, _ = self.resolve(role, room_id if scope == "room" else "")
            merged = dict(base_sec)
            for k in SECTION_KEYS:
                if k in sections and isinstance(sections[k], str):
                    merged[k] = sections[k]
            path = self._file_path(scope, room_id)
            all_data = self._read(path)
            entry = all_data.get(role) if isinstance(all_data.get(role), dict) else {}
            seq = int(entry.get("seq") or 0) + 1
            ver = "%s:v%d" % (prefix, seq)
            hist: List[Dict[str, Any]] = list(entry.get("history") or [])
            hist.append(
                {
                    "version": ver,
                    "at": _now(),
                    "kind": kind,
                    "note": note or "",
                    "sections": dict(merged),
                }
            )
            hist = hist[-PROMPT_HISTORY:]
            all_data[role] = {
                "seq": seq,
                "version": ver,
                "current": dict(merged),
                "history": hist,
            }
            self._write(path, all_data)
            return ver

    def reset(self, role: str, scope: str, room_id: str = "") -> None:
        """清除該層的覆寫（回到下一層或 default），但保留版本歷史。"""
        self._check_role(role)
        with self._lock:
            path = self._file_path(scope, room_id)
            all_data = self._read(path)
            entry = all_data.get(role)
            if isinstance(entry, dict):
                entry.pop("current", None)
                entry.pop("version", None)
                all_data[role] = entry
                self._write(path, all_data)

    def history(self, role: str, scope: str, room_id: str = "") -> List[Dict[str, Any]]:
        self._check_role(role)
        with self._lock:
            path = self._file_path(scope, room_id)
            entry = self._read(path).get(role)
            if not isinstance(entry, dict) or not isinstance(entry.get("history"), list):
                return []
            return list(entry["history"])

    def restore(self, role: str, scope: str, version: str, room_id: str = "") -> str:
        """把目前設定回復為歷史中的指定版本。"""
        self._check_role(role)
        with self._lock:
            path = self._file_path(scope, room_id)
            all_data = self._read(path)
            entry = all_data.get(role)
            if not isinstance(entry, dict):
                raise ValueError("找不到版本歷史：%s" % version)
            hist = list(entry.get("history") or [])
            target = next((h for h in reversed(hist) if h.get("version") == version), None)
            if target is None or not isinstance(target.get("sections"), dict):
                raise ValueError("找不到版本：%s" % version)
            entry["current"] = dict(target["sections"])
            entry["version"] = str(target["version"])
            all_data[role] = entry
            self._write(path, all_data)
            return str(target["version"])

    def mark(self, role: str, room_id: str, version: str, note: str) -> None:
        """替房間的某個歷史版本標註評估結果（例如 Decision-1 判定較好）。"""
        self._check_role(role)
        with self._lock:
            path = self._file_path("room", room_id)
            all_data = self._read(path)
            entry = all_data.get(role)
            if not isinstance(entry, dict):
                return
            for h in entry.get("history") or []:
                if h.get("version") == version:
                    h["note"] = note
            all_data[role] = entry
            self._write(path, all_data)

    def promote(self, role: str, room_id: str) -> str:
        """把房間目前的版本升為全域新版本。"""
        self._check_role(role)
        sec, ver = self.resolve(role, room_id)
        if ver == "default":
            raise ValueError("這間房目前使用的是預設提示詞，無須升為全域")
        return self.save(role, sec, "global", note="從房間 %s（%s）升為全域" % (room_id, ver))

    def view(self, room_id: str = "") -> Dict[str, Any]:
        """給 GET /api/team/prompts 的完整檢視結構。"""
        with self._lock:
            g_all = self._read(self._file_path("global"))
            r_all = self._read(self._file_path("room", room_id)) if room_id else {}
            roles_out: Dict[str, Any] = {}
            for role in ROLES:
                active, ver = self.resolve(role, room_id)
                g_entry = g_all.get(role) if isinstance(g_all.get(role), dict) else {}
                r_entry = r_all.get(role) if isinstance(r_all.get(role), dict) else {}
                r_hist = list(r_entry.get("history") or [])
                cur_ver = str(r_entry.get("version") or "")
                recommend_promote = any(
                    h.get("version") == cur_ver and "較好" in str(h.get("note") or "")
                    for h in r_hist
                )
                roles_out[role] = {
                    "label": ROLE_LABELS.get(role, role),
                    "auto_tunable": role in AUTO_ROLES,
                    "contract": CONTRACTS[role],
                    "safety": SAFETY,
                    "default": dict(DEFAULTS[role]),
                    "global": dict(g_entry["current"]) if isinstance(g_entry.get("current"), dict) else None,
                    "room": dict(r_entry["current"]) if isinstance(r_entry.get("current"), dict) else None,
                    "active": active,
                    "version": ver,
                    "placeholders": sorted(PLACEHOLDERS),
                    "history_global": list(g_entry.get("history") or []),
                    "history_room": r_hist,
                    "recommend_promote": recommend_promote,
                }
            return {"roles": roles_out, "placeholders": sorted(PLACEHOLDERS)}
