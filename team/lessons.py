"""專案知識庫（lessons.md）：記錄同專案踩過的環境、測試與審查教訓，並注入後續房間（設計 §5.6、Task 11）。"""
import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from team import events, personas
from team.models import call_ctx
from team.schemas import LessonEntry

log = logging.getLogger(__name__)

LESSONS_N = 20
LESSONS_CHARS = 2000
MAX_LESSON_LEN = 80

_HEADER_RE = re.compile(r"^##\s+([^｜\n]+)｜([^｜\n]+)｜([^\n]+)\s*$")


def repo_id(repo: str) -> str:
    """主 repo 絕對路徑正規化後的 SHA1 前 12 碼（跨大小寫與斜線一致）。"""
    cleaned = os.path.normcase(os.path.normpath(os.path.abspath(repo or "")))
    return hashlib.sha1(cleaned.encode("utf-8", errors="replace")).hexdigest()[:12]


def _header_id(header_line: str) -> str:
    return hashlib.sha1(header_line.strip().encode("utf-8", errors="replace")).hexdigest()[:8]


def condense(
    router: Any,
    material: str,
    ctx: Optional[Dict[str, Any]] = None,
    secrets: Sequence[str] = (),
) -> str:
    """請 glm 低思考量將框內的診斷或卡關材料濃縮成一句教訓（≤ 80 字）；模型失敗回空字串。"""
    if router is None or not (material or "").strip():
        return ""
    fenced_mat = personas.fenced("事件材料", (material or "")[:3000])
    prompt = (
        "請把以下事件材料濃縮成「一句簡短的專案教訓」（繁體中文，80 字以內，單行、不要換行、不要引號、不要編號），"
        "讓未來在同一個專案工作的代理不要再踩同一個坑。\n\n"
        + fenced_mat
    )
    try:
        if hasattr(router, "write_json"):
            entry = call_ctx(router.write_json, prompt, LessonEntry, effort="low", ctx=ctx or {})
            raw = getattr(entry, "lesson", "")
        else:
            raw = call_ctx(router.write, prompt, effort="low", ctx=ctx or {})
    except Exception as e:
        log.warning("教訓濃縮失敗（fail-closed）：%s", type(e).__name__)
        return ""
    line = " ".join(str(raw or "").split()).strip()
    if not line:
        return ""
    if len(line) > MAX_LESSON_LEN:
        line = line[: MAX_LESSON_LEN - 1] + "…"
    return events.mask_text(line, secrets)


class LessonBook:
    def __init__(self, root: str):
        self.root = root
        self._lock = threading.RLock()
        self._seq = 0
        os.makedirs(root, exist_ok=True)

    def _proj_dir(self, repo: str) -> str:
        d = os.path.join(self.root, "projects", repo_id(repo))
        os.makedirs(d, exist_ok=True)
        return d

    def _path(self, repo: str) -> str:
        return os.path.join(self._proj_dir(repo), "lessons.md")

    def _counts_path(self, repo: str) -> str:
        return os.path.join(self._proj_dir(repo), "review_counts.json")

    def _parse(self, repo: str) -> List[Dict[str, str]]:
        """依檔案順序（舊到新）解析所有教訓。"""
        path = self._path(repo)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return []
        entries: List[Dict[str, str]] = []
        cur_header: Optional[Tuple[str, str, str, str]] = None
        cur_lines: List[str] = []

        def _flush() -> None:
            if cur_header is not None:
                raw_h, dt, cat, rid = cur_header
                txt = " ".join(" ".join(cur_lines).split()).strip()
                entries.append(
                    {
                        "id": _header_id(raw_h),
                        "header": raw_h,
                        "date": dt,
                        "category": cat,
                        "room_id": rid,
                        "text": txt,
                    }
                )

        for line in content.splitlines():
            m = _HEADER_RE.match(line.strip())
            if m:
                _flush()
                cur_header = (line.strip(), m.group(1).strip(), m.group(2).strip(), m.group(3).strip())
                cur_lines = []
            elif cur_header is not None and line.strip():
                cur_lines.append(line.strip())
        _flush()
        return entries

    def _save_entries(self, repo: str, entries: List[Dict[str, str]]) -> None:
        path = self._path(repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        blocks = []
        for e in entries:
            h = e.get("header") or ("## %s｜%s｜%s" % (e["date"], e["category"], e["room_id"]))
            blocks.append("%s\n%s\n" % (h, e["text"]))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(blocks))
        os.replace(tmp, path)

    def add(
        self,
        repo: str,
        category: str,
        text: str,
        room_id: str,
        secrets: Sequence[str] = (),
    ) -> str:
        """新增一筆教訓（遮蔽金鑰後寫入），回傳該筆 id（標題行雜湊前 8 碼）。"""
        clean = events.mask_text(" ".join((text or "").split()).strip(), secrets)
        if not clean or not repo:
            return ""
        if len(clean) > 200:
            clean = clean[:199] + "…"
        cat = (category or "general").replace("｜", "/").strip()
        rid = (room_id or "room").replace("｜", "/").strip()
        with self._lock:
            entries = self._parse(repo)
            existing_ids = {e["id"] for e in entries}
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = "## %s｜%s｜%s" % (now_str, cat, rid)
            while _header_id(header) in existing_ids:
                self._seq += 1
                header = "## %s.%d｜%s｜%s" % (now_str, self._seq, cat, rid)
            lid = _header_id(header)
            dt_part = header[3:].split("｜", 1)[0]
            entries.append(
                {
                    "id": lid,
                    "header": header,
                    "date": dt_part,
                    "category": cat,
                    "room_id": rid,
                    "text": clean,
                }
            )
            self._save_entries(repo, entries)
            return lid

    def list(self, repo: str) -> List[Dict[str, str]]:
        """回傳該專案的所有教訓（新到舊）。"""
        with self._lock:
            return [
                {k: v for k, v in e.items() if k != "header"}
                for e in reversed(self._parse(repo))
            ]

    def recent(self, repo: str, n: int = LESSONS_N, max_chars: int = LESSONS_CHARS) -> List[Dict[str, str]]:
        """回傳最近 n 筆教訓（新到舊），且總字數不超過 max_chars。"""
        with self._lock:
            items = self.list(repo)
        out: List[Dict[str, str]] = []
        used = 0
        for it in items:
            if len(out) >= n:
                break
            line_len = len(it.get("text") or "") + len(it.get("category") or "") + 6
            if out and used + line_len > max_chars:
                break
            if not out and line_len > max_chars:
                clipped = dict(it)
                clipped["text"] = (it.get("text") or "")[: max(1, max_chars - 10)]
                out.append(clipped)
                break
            out.append(it)
            used += line_len
        return out

    def format_for_prompt(
        self, repo: str, n: int = LESSONS_N, max_chars: int = LESSONS_CHARS
    ) -> Tuple[str, List[str]]:
        """回傳給 Composer 帶入【本次資料】的字串與使用的教訓 id 清單。"""
        items = self.recent(repo, n=n, max_chars=max_chars)
        if not items:
            return "", []
        lines = ["- [%s] %s" % (it["category"], it["text"]) for it in items]
        return "\n".join(lines)[:max_chars], [it["id"] for it in items]

    def edit(self, repo: str, lid: str, text: str, secrets: Sequence[str] = ()) -> bool:
        clean = events.mask_text(" ".join((text or "").split()).strip(), secrets)
        if not clean:
            return False
        with self._lock:
            entries = self._parse(repo)
            found = False
            for e in entries:
                if e["id"] == lid:
                    e["text"] = clean
                    found = True
                    break
            if found:
                self._save_entries(repo, entries)
            return found

    def delete(self, repo: str, lid: str) -> bool:
        with self._lock:
            entries = self._parse(repo)
            kept = [e for e in entries if e["id"] != lid]
            if len(kept) == len(entries):
                return False
            self._save_entries(repo, kept)
            return True

    def bump_review_blocking(self, repo: str, blocking_text: str) -> int:
        """統計同專案出現的 Reviewer blocking 類型（去掉檔案:行號後正規化前 30 字），回傳累計次數。"""
        if not repo or not (blocking_text or "").strip():
            return 0
        cleaned = re.sub(r"^[^\s:]+:\d+\s*", "", (blocking_text or "").strip())
        key = " ".join(cleaned.split()).lower()[:30]
        if not key:
            return 0
        with self._lock:
            cpath = self._counts_path(repo)
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    counts = json.load(f)
                    if not isinstance(counts, dict):
                        counts = {}
            except (OSError, ValueError):
                counts = {}
            n = int(counts.get(key) or 0) + 1
            counts[key] = n
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(counts, f, ensure_ascii=False, indent=2)
            return n
