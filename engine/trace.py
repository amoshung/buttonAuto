"""房間執行流程結構化追蹤紀錄（trace.jsonl），供除錯、效能分析與提示詞優化使用。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, List


class RoomTracer:
    """執行緒安全的單房 JSONL 追蹤器（寫入 sessions/team/<room_id>/trace.jsonl）。"""

    def __init__(self, room_dir: str, room_id: str) -> None:
        self.room_id = room_id
        self.path = os.path.join(room_dir, "trace.jsonl")
        self._lock = threading.Lock()
        os.makedirs(room_dir, exist_ok=True)

    def record(self, event_kind: str, /, **fields: Any) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "room_id": self.room_id,
            "kind": event_kind,
        }
        for k, v in fields.items():
            entry[k] = v
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        return entry

    def read(self, limit: int = 200) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                return []
        out: List[Dict[str, Any]] = []
        for raw in lines[-max(1, int(limit)) :]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    out.append(obj)
            except ValueError:
                continue
        return out
