"""專案知識庫（LLM Wiki 架構 + 中鋼主權 AI qwen3-embedding 2560d / bge-reranker-v2-m3 + glm-5.3 編譯合成）。

取代舊版 LightRAG 黑盒圖譜，改採可讀、可版控、零幻覺的 **LLM Wiki（雙向連結 Markdown 百科 + 結構化索引）**：
- 每個專案在 `<sessions_root>/kb/wiki/<repo_id>/` 維護：
  - `index.md`：全庫總目錄（依命名空間分類、一句話摘要、`[[namespace/slug]]` 雙向連結、來源房間 `[room:<id>]`）
  - `log.md`：雙時序追加日誌（事件發生與收錄時間、分類、房間與指紋）
  - `pages/<namespace>/<slug>.md`：結構化 Markdown 百科條目（含 YAML frontmatter、精編摘要、歷次實證紀錄與 `[[wikilinks]]`）
  - `vectors.json`：中鋼主權 AI `qwen3-embedding` (2560d) 條目向量索引（離線時自動以確定性 BM25 / 字詞重疊檢索退回）
- 自動同步專案內 Compound Engineering 的 `docs/solutions/**/*.md` 與 `docs/plans/**/*.md` 進入 LLM Wiki。
- 單一寫入執行緒序列更新 Wiki 條目與索引，避免多房間並發寫入競態。
- 每筆寫入皆帶 `[project:<repo_id>] [room:<room_id>]` 標籤並以 `(repo_id, fingerprint)` 去重。
- 寫入與查詢前皆經 `events.mask_text(..., secrets)` 遮罩金鑰。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import re
import shutil
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

from team import events
from team.lessons import LessonBook, repo_id
from team.usage import now_iso

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-/]+|[\u4e00-\u9fff]{2,}")

NAMESPACE_LABELS: Dict[str, str] = {
    "architecture": "架構與模組脈絡",
    "plans": "CE 開發計畫與里程碑",
    "solutions": "CE 已驗證解法 (docs/solutions)",
    "test": "測試關卡與驗證防呆",
    "review": "程式碼審查規範與教訓",
    "env": "環境與相依設定",
    "rooms": "房間交接與開發紀錄 (Handoff)",
    "general": "通用工程知識",
}


def _post_json(url: str, key: str, payload: Dict[str, Any], timeout: int = _DEFAULT_TIMEOUT) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _slugify_title(text: str) -> str:
    cleaned = re.sub(r"\[(?:project|room|category|fingerprint):[^\]]+\]", "", text or "").strip()
    first_line = (cleaned.splitlines()[0] if cleaned else "entry").strip()
    ascii_part = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    digest = hashlib.sha1(first_line.encode("utf-8", errors="replace")).hexdigest()[:8]
    if ascii_part and len(ascii_part) >= 4:
        return f"{ascii_part[:40]}-{digest}"
    return f"wiki-{digest}"


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    tokens: List[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        tok = m.group(0)
        tokens.append(tok)
        if re.match(r"^[\u4e00-\u9fff]{3,}$", tok):
            for i in range(len(tok) - 1):
                tokens.append(tok[i : i + 2])
    return tokens


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return dot / (na * nb)


class LLMWikiEngine:
    """純檔案式＋主權向量輔助之 LLM Wiki 引擎（管理單一或多個專案的 Markdown Wiki）。"""

    def __init__(self, kb_dir: str, kb: "KnowledgeBase"):
        self.kb_dir = kb_dir
        self.kb = kb
        self.wiki_root = os.path.join(kb_dir, "wiki")
        os.makedirs(self.wiki_root, exist_ok=True)
        self._lock = threading.RLock()

    def _proj_wiki_dir(self, rid: str) -> str:
        d = os.path.join(self.wiki_root, rid or "default")
        os.makedirs(os.path.join(d, "pages"), exist_ok=True)
        return d

    def _meta_path(self, rid: str) -> str:
        return os.path.join(self._proj_wiki_dir(rid), "catalog.json")

    def _vectors_path(self, rid: str) -> str:
        return os.path.join(self._proj_wiki_dir(rid), "vectors.json")

    @staticmethod
    def _load_json_dict(path: str) -> Dict[str, Any]:
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load_catalog(self, rid: str) -> Dict[str, Dict[str, Any]]:
        return self._load_json_dict(self._meta_path(rid))

    def _save_catalog(self, rid: str, catalog: Dict[str, Dict[str, Any]]) -> None:
        path = self._meta_path(rid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _load_vectors(self, rid: str) -> Dict[str, List[float]]:
        return self._load_json_dict(self._vectors_path(rid))

    def _save_vectors(self, rid: str, vectors: Dict[str, List[float]]) -> None:
        path = self._vectors_path(rid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(vectors, f)
        os.replace(tmp, path)

    @staticmethod
    def _parse_tagged_doc(doc: str) -> Tuple[str, str, str, str, str]:
        """從 `[fingerprint:...] [project:...] [room:...] [category:...]\n內容` 解析欄位。"""
        fp_m = re.search(r"\[fingerprint:([^\]]+)\]", doc)
        pr_m = re.search(r"\[project:([^\]]+)\]", doc)
        rm_m = re.search(r"\[room:([^\]]+)\]", doc)
        ct_m = re.search(r"\[category:([^\]]+)\]", doc)
        fp = fp_m.group(1).strip() if fp_m else ""
        rid = pr_m.group(1).strip() if pr_m else "default"
        room_id = rm_m.group(1).strip() if rm_m else "room"
        cat = ct_m.group(1).strip() if ct_m else "general"
        body = re.sub(r"^(?:\[(?:fingerprint|project|room|category):[^\]]+\]\s*)+", "", doc or "").strip()
        return rid, room_id, cat, fp, body

    def insert(self, doc: str) -> Optional[str]:
        """寫入或增量更新一篇 LLM Wiki 頁面，並重建 `index.md` 與 `log.md`。"""
        if not (doc or "").strip():
            return None
        rid, room_id, category, fp, body = self._parse_tagged_doc(doc)
        if not body:
            return None
        ns = category.lower().strip() if category else "general"
        if ns not in NAMESPACE_LABELS:
            ns = "general"
        now_str = now_iso()
        slug = _slugify_title(body)
        page_id = f"{ns}/{slug}"

        with self._lock:
            wdir = self._proj_wiki_dir(rid)
            catalog = self._load_catalog(rid)
            existing = catalog.get(page_id)
            first_line = body.splitlines()[0].strip()[:80]
            title = re.sub(r"^#+\s*", "", first_line) or slug

            # 擷取內文中的 [[wikilinks]]
            links = sorted(set(_WIKILINK_RE.findall(body)))

            # 若有設定 glm-5.3 router 且內容較長，合成一段精簡百科摘要
            summary = title
            if self.kb.router is not None and len(body) > 220:
                try:
                    prompt = (
                        "請將以下工程紀錄濃縮為 LLM Wiki 條目的一句話核心結論（繁體中文、60 字以內、單行無引號）：\n\n"
                        + body[:1500]
                    )
                    syn = self.kb.router.write(prompt, {"purpose": "LLM Wiki 條目摘要"})
                    if isinstance(syn, str) and syn.strip():
                        summary = " ".join(syn.strip().split())[:80]
                except Exception:
                    pass

            page_rel = os.path.join("pages", ns, f"{slug}.md")
            page_full = os.path.join(wdir, page_rel)
            os.makedirs(os.path.dirname(page_full), exist_ok=True)

            rooms = list(existing.get("rooms", [])) if existing else []
            if room_id and room_id not in rooms:
                rooms.append(room_id)
            fps = list(existing.get("fingerprints", [])) if existing else []
            if fp and fp not in fps:
                fps.append(fp)
            created_at = existing.get("created_at", now_str) if existing else now_str

            entries: List[Dict[str, str]] = list(existing.get("entries", [])) if existing else []
            entries.append({"at": now_str, "room_id": room_id, "text": body})

            md_lines = [
                "---",
                f"id: {page_id}",
                f"title: {json.dumps(title, ensure_ascii=False)}",
                f"namespace: {ns}",
                f"repo_id: {rid}",
                f"rooms: {json.dumps(rooms, ensure_ascii=False)}",
                f"created_at: {created_at}",
                f"updated_at: {now_str}",
                f"links: {json.dumps(links, ensure_ascii=False)}",
                "---",
                "",
                f"# {title}",
                "",
                f"> **核心摘要**：{summary}",
                f"> **命名空間**：`{ns}`（{NAMESPACE_LABELS.get(ns, ns)}）｜**來源房間**：{', '.join(f'`{r}`' for r in rooms)}",
                "",
                "## 知識內容與實證紀錄",
                "",
            ]
            for idx, ent in enumerate(entries, 1):
                md_lines.append(f"### 紀錄 {idx}（{ent['at']} · 房間 `{ent['room_id']}`）")
                md_lines.append("")
                md_lines.append(ent["text"])
                md_lines.append("")

            if links:
                md_lines.append("## 關聯條目 (Wikilinks)")
                for lk in links:
                    md_lines.append(f"- [[{lk}]]")
                md_lines.append("")

            with open(page_full, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(md_lines))

            catalog[page_id] = {
                "id": page_id,
                "title": title,
                "summary": summary,
                "namespace": ns,
                "path": page_rel.replace("\\", "/"),
                "rooms": rooms,
                "fingerprints": fps,
                "links": links,
                "created_at": created_at,
                "updated_at": now_str,
                "entries": entries[-10:],
                "search_text": f"{title}\n{summary}\n" + "\n".join(e["text"] for e in entries[-5:]),
            }
            self._save_catalog(rid, catalog)
            self._append_log(wdir, now_str, page_id, room_id, ns, summary)
            self._rebuild_index_md(wdir, rid, catalog)

            # 若中鋼主權 AI 可用，背景計算並保存該頁向量
            if self.kb._sovereign_ready():
                try:
                    vecs = self.kb.embed([catalog[page_id]["search_text"][:4000]])
                    if vecs and vecs[0]:
                        vmap = self._load_vectors(rid)
                        vmap[page_id] = vecs[0]
                        self._save_vectors(rid, vmap)
                except Exception:
                    pass
            return page_id

    def _append_log(self, wdir: str, now_iso: str, page_id: str, room_id: str, ns: str, summary: str) -> None:
        log_path = os.path.join(wdir, "log.md")
        header_needed = not os.path.isfile(log_path)
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            if header_needed:
                f.write("# LLM Wiki 演進日誌 (Bi-Temporal Log)\n\n")
            f.write(f"- `{now_iso}`｜`[{ns}]` [[{page_id}]]（房間 `{room_id}`）：{summary}\n")

    def _rebuild_index_md(self, wdir: str, rid: str, catalog: Dict[str, Dict[str, Any]]) -> None:
        lines = [
            f"# 專案 LLM Wiki 知識總覽 (`{rid}`)",
            "",
            "> 本檔案由系統自動維護，供開發與審查代理以 **Wiki-First** 方式快速定位專案架構、CE 計畫、已驗證解法與防呆規則。",
            "",
        ]
        by_ns: Dict[str, List[Dict[str, Any]]] = {}
        for item in catalog.values():
            ns = item.get("namespace") or "general"
            by_ns.setdefault(ns, []).append(item)

        for ns, label in NAMESPACE_LABELS.items():
            items = sorted(by_ns.get(ns) or [], key=lambda x: x.get("updated_at", ""), reverse=True)
            if not items:
                continue
            lines.append(f"## {label} (`{ns}`)")
            lines.append("")
            for it in items:
                pid = it["id"]
                summary = it.get("summary") or it.get("title") or pid
                updated = (it.get("updated_at") or "")[:16].replace("T", " ")
                rooms_str = ",".join(it.get("rooms") or [])
                lines.append(f"- [[{pid}]]：{summary} _({updated} · {rooms_str})_")
            lines.append("")

        idx_path = os.path.join(wdir, "index.md")
        with open(idx_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))

    def query(self, scoped_query: str, *, top_k: int = 6, **_: Any) -> str:
        """Wiki-First 檢索：結合關鍵字/BM25、雙向連結展開、向量相似度與中鋼 bge-reranker-v2-m3 精排。"""
        pr_m = re.search(r"\[project:([^\]]+)\]", scoped_query or "")
        rid = pr_m.group(1).strip() if pr_m else "default"
        q_body = re.sub(r"^(?:\[(?:project|room):[^\]]+\]\s*)+", "", scoped_query or "").strip()

        with self._lock:
            catalog = self._load_catalog(rid)
            if not catalog:
                return ""
            q_tokens = set(_tokenize(q_body))
            vmap = self._load_vectors(rid)
            q_vec: Optional[List[float]] = None
            if vmap and self.kb._sovereign_ready() and q_body:
                try:
                    vecs = self.kb.embed([q_body[:2000]])
                    if vecs and vecs[0]:
                        q_vec = vecs[0]
                except Exception:
                    q_vec = None

            scored: List[Tuple[float, Dict[str, Any]]] = []
            for pid, item in catalog.items():
                doc_text = item.get("search_text") or f"{item.get('title', '')}\n{item.get('summary', '')}"
                d_tokens = set(_tokenize(doc_text))
                overlap = len(q_tokens & d_tokens) / max(1.0, math.sqrt(len(q_tokens) or 1))
                vec_score = _cosine(q_vec, vmap.get(pid, [])) if q_vec and pid in vmap else 0.0
                # 最新更新與架構/解法命名空間微幅加權
                ns_boost = 0.15 if item.get("namespace") in ("solutions", "architecture", "plans", "rooms") else 0.0
                total = overlap * 0.65 + vec_score * 0.55 + ns_boost
                scored.append((total, item))

            scored.sort(key=lambda x: x[0], reverse=True)
            candidates = [it for _, it in scored[: max(top_k * 2, 8)]]

            # 若中鋼主權 AI reranker 可用且候選 >= 2，使用 bge-reranker-v2-m3 精排
            if len(candidates) >= 2 and self.kb._sovereign_ready() and q_body:
                try:
                    docs = [c.get("search_text") or c.get("summary") or "" for c in candidates]
                    rr = self.kb.rerank(q_body, docs, top_n=min(top_k, len(candidates)))
                    if rr:
                        reordered: List[Dict[str, Any]] = []
                        for r in sorted(rr, key=lambda x: float(x.get("relevance_score", 0.0)), reverse=True):
                            idx = int(r.get("index", -1))
                            if 0 <= idx < len(candidates) and candidates[idx] not in reordered:
                                reordered.append(candidates[idx])
                        if reordered:
                            candidates = reordered
                except Exception:
                    pass

            picked = candidates[:top_k]
            # 1-hop 雙向連結展開：若命中的頁面有 [[wikilinks]] 指向同庫其他頁面，補入最多 2 篇關聯頁
            seen_ids = {p["id"] for p in picked}
            for p in list(picked):
                for lk in p.get("links") or []:
                    if lk in catalog and lk not in seen_ids and len(picked) < top_k + 2:
                        picked.append(catalog[lk])
                        seen_ids.add(lk)

            out_lines = [f"### LLM Wiki 檢索結果（共命中 {len(picked)} 篇條目）"]
            for p in picked:
                latest_text = ""
                ents = p.get("entries") or []
                if ents:
                    latest_text = ents[-1].get("text", "")[:400]
                out_lines.append(
                    f"- **[[{p['id']}]]**（{p.get('namespace')}）：{p.get('summary')}\n  {latest_text}"
                )
            return "\n".join(out_lines)


class KnowledgeBase:
    """單寫入佇列的專案知識庫（LLM Wiki 雙向連結百科 + LessonBook 保底）。"""

    def __init__(
        self,
        sessions_root: str,
        config: Any = None,
        router: Any = None,
        secrets: Sequence[str] = (),
        post_fn: Optional[Callable[[str, str, Dict[str, Any], int], Dict[str, Any]]] = None,
        rag_factory: Optional[Callable[..., Any]] = None,
    ):
        self.sessions_root = sessions_root
        self.kb_dir = os.path.join(sessions_root, "kb")
        os.makedirs(self.kb_dir, exist_ok=True)
        self.config = config
        self.router = router
        self.secrets = tuple(s for s in (secrets or ()) if s)
        self._post = post_fn or _post_json
        self._rag_factory = rag_factory
        self._lessons = LessonBook(sessions_root)
        self._dedup_path = os.path.join(self.kb_dir, "dedup.json")
        self._dedup_lock = threading.Lock()
        self._seen: set[Tuple[str, str]] = self._load_dedup()
        self._wiki: Any = None
        self._wiki_lock = threading.Lock()
        self._queue: queue.Queue[Optional[Tuple[str, Optional[threading.Event]]]] = queue.Queue()
        self._closed = False
        self._worker = threading.Thread(target=self._run_worker, name="kb-wiki-writer", daemon=True)
        self._worker.start()

    # ---- 去重持久化 ----
    def _load_dedup(self) -> set[Tuple[str, str]]:
        try:
            with open(self._dedup_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                return {(str(item[0]), str(item[1])) for item in raw if isinstance(item, list) and len(item) == 2}
        except (OSError, ValueError):
            pass
        return set()

    def _save_dedup(self) -> None:
        try:
            tmp = self._dedup_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted([list(x) for x in self._seen]), f, ensure_ascii=False)
            os.replace(tmp, self._dedup_path)
        except OSError as e:
            log.warning("kb dedup.json 寫入失敗：%s", type(e).__name__)

    def _has_seen(self, rid: str, fingerprint: str) -> bool:
        if not fingerprint:
            return False
        with self._dedup_lock:
            return (rid, fingerprint) in self._seen

    def _mark_if_new(self, rid: str, fingerprint: str) -> bool:
        if not fingerprint:
            return True
        key = (rid, fingerprint)
        with self._dedup_lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self._save_dedup()
            return True

    # ---- 主權 AI 呼叫 ----
    def _sovereign_ready(self) -> bool:
        return bool(
            self.config
            and getattr(self.config, "sovereign_base_url", "")
            and getattr(self.config, "sovereign_key", "")
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not self._sovereign_ready():
            raise RuntimeError("尚未設定中鋼主權 AI（sovereign_base_url / sovereign_key）")
        base = self.config.sovereign_base_url.rstrip("/")
        model = getattr(self.config, "embedding_model", "qwen3-embedding")
        clean = [events.mask_text(t, self.secrets) for t in texts]
        data = self._post(
            f"{base}/v1/embeddings",
            self.config.sovereign_key,
            {"model": model, "input": clean},
            _DEFAULT_TIMEOUT,
        )
        rows = sorted(data.get("data") or [], key=lambda x: x.get("index", 0))
        return [list(r["embedding"]) for r in rows]

    def rerank(self, query: str, documents: List[str], top_n: int = 8) -> List[Dict[str, Any]]:
        if not self._sovereign_ready():
            raise RuntimeError("尚未設定中鋼主權 AI（sovereign_base_url / sovereign_key）")
        base = self.config.sovereign_base_url.rstrip("/")
        model = getattr(self.config, "rerank_model", "bge-reranker-v2-m3")
        payload = {
            "model": model,
            "query": events.mask_text(query, self.secrets),
            "documents": [events.mask_text(d, self.secrets) for d in documents],
            "top_n": top_n,
        }
        data = self._post(f"{base}/v1/rerank", self.config.sovereign_key, payload, _DEFAULT_TIMEOUT)
        return list(data.get("results") or [])

    # ---- LLM Wiki 引擎初始化 ----
    def _ensure_wiki(self) -> Any:
        with self._wiki_lock:
            if self._wiki is not None:
                return self._wiki
            if self._rag_factory is not None:
                self._wiki = self._rag_factory(self.kb_dir, self)
                return self._wiki
            self._wiki = LLMWikiEngine(self.kb_dir, self)
            return self._wiki

    # ---- 單寫入執行緒 ----
    def _run_worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            doc, done_ev = item
            try:
                wiki = self._ensure_wiki()
                if wiki is not None and hasattr(wiki, "insert"):
                    wiki.insert(doc)
            except Exception as e:
                log.warning("LLM Wiki 寫入失敗（已略過）：%s", type(e).__name__)
            finally:
                if done_ev is not None:
                    done_ev.set()
                self._queue.task_done()

    def record(
        self,
        repo: str,
        room_id: str,
        category: str,
        text: str,
        *,
        fingerprint: str = "",
        wait: bool = False,
    ) -> bool:
        """將已遮罩與標記專案/房間的知識排入單一寫入佇列編譯為 LLM Wiki 條目；重複 fingerprint 回 False。"""
        clean = events.mask_text((text or "").strip(), self.secrets)
        if not clean:
            return False
        rid = repo_id(repo)
        if not self._mark_if_new(rid, fingerprint):
            return False
        self._lessons.add(repo, category, clean, room_id=room_id, secrets=self.secrets)
        tagged = f"[project:{rid}] [room:{room_id}] [category:{category}]\n{clean}"
        if fingerprint:
            tagged = f"[fingerprint:{fingerprint}] " + tagged
        ev = threading.Event() if wait else None
        self._queue.put((tagged, ev))
        if ev is not None:
            ev.wait(timeout=10.0)
        return True

    def sync_repo_compound_docs(
        self,
        repo: str,
        reference_root: str = "",
        *,
        target_subdir: str = "",
        target_subdirs: Sequence[str] = (),
    ) -> int:
        """自動掃描目標專案資料夾下的 `docs/solutions/` 與 `docs/plans/` 並收錄至 LLM Wiki（不掃描母資料夾內無關的兄弟專案）。"""
        if not repo:
            return 0
        rid = repo_id(repo)
        subs: List[str] = []
        for s in (target_subdirs or ()):
            sc = str(s or "").strip().strip("/\\")
            if sc and sc not in subs:
                subs.append(sc)
        if not subs and target_subdir and "," not in target_subdir:
            sc = target_subdir.strip().strip("/\\")
            if sc:
                subs.append(sc)
        if subs:
            roots = [os.path.join(repo, s.replace("/", os.sep)) for s in subs if os.path.isdir(os.path.join(repo, s.replace("/", os.sep)))]
        else:
            roots = [repo]
        added = 0
        for rpath in roots:
            for sub, ns in (("docs/solutions", "solutions"), ("docs/plans", "plans")):
                d = os.path.join(rpath, sub.replace("/", os.sep))
                if not os.path.isdir(d):
                    continue
                for dirpath, _dirs, files in os.walk(d):
                    for fn in sorted(files):
                        if not fn.endswith(".md"):
                            continue
                        full = os.path.join(dirpath, fn)
                        try:
                            st = os.stat(full)
                            fp = f"ce-doc:{fn}:{int(st.st_mtime)}"
                            if self._has_seen(rid, fp):
                                continue
                            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                                content = f.read(3500).strip()
                            if content and self.record(
                                repo, "ce-sync", ns, f"# {fn}\n\n{content}", fingerprint=fp, wait=True
                            ):
                                added += 1
                        except OSError:
                            pass
        return added

    def flush(self, timeout: float = 5.0) -> None:
        """等待佇列中所有寫入完成。"""
        done = threading.Event()
        self._queue.put(("", done))
        done.wait(timeout=timeout)

    def query(self, repo: str, question: str, *, room_id: str = "", mode: str = "hybrid", top_k: int = 8) -> str:
        """以 Wiki-First 方式查詢專案 LLM Wiki 知識庫；若無條目則退回 LessonBook.format_for_prompt。"""
        rid = repo_id(repo)
        q_clean = events.mask_text((question or "").strip(), self.secrets)
        if not q_clean:
            return self._lessons.format_for_prompt(repo)[0]
        scope = f"[project:{rid}]" + (f" [room:{room_id}]" if room_id else "")
        scoped_query = f"{scope}\n{q_clean}"
        try:
            wiki = self._ensure_wiki()
            if wiki is not None and hasattr(wiki, "query"):
                ans = wiki.query(scoped_query, mode=mode, top_k=top_k)
                if isinstance(ans, str) and ans.strip():
                    return events.mask_text(ans.strip(), self.secrets)
        except Exception as e:
            log.warning("LLM Wiki 查詢失敗，退回 lessons.md：%s", type(e).__name__)
        return self._lessons.format_for_prompt(repo)[0]

    def wiki_view(
        self,
        repo: str,
        reference_root: str = "",
        *,
        sync: bool = True,
        target_subdir: str = "",
        target_subdirs: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """回傳指定專案的 LLM Wiki 目錄、統計與 `index.md` 供前端檢視。"""
        if not repo:
            return {"repo_id": "", "index_md": "", "pages": [], "namespaces": NAMESPACE_LABELS}
        if sync:
            self.sync_repo_compound_docs(
                repo,
                reference_root=reference_root,
                target_subdir=target_subdir,
                target_subdirs=target_subdirs,
            )
        rid = repo_id(repo)
        wiki = self._ensure_wiki()
        if not isinstance(wiki, LLMWikiEngine):
            return {"repo_id": rid, "index_md": "", "pages": [], "namespaces": NAMESPACE_LABELS}
        wdir = wiki._proj_wiki_dir(rid)
        catalog = wiki._load_catalog(rid)
        idx_path = os.path.join(wdir, "index.md")
        idx_md = ""
        if os.path.isfile(idx_path):
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    idx_md = f.read()
            except OSError:
                pass
        pages = sorted(
            [
                {
                    "id": v["id"],
                    "title": v.get("title", v["id"]),
                    "summary": v.get("summary", ""),
                    "namespace": v.get("namespace", "general"),
                    "rooms": v.get("rooms", []),
                    "links": v.get("links", []),
                    "updated_at": v.get("updated_at", ""),
                }
                for v in catalog.values()
            ],
            key=lambda x: x.get("updated_at", ""),
            reverse=True,
        )
        return {
            "repo_id": rid,
            "wiki_dir": wdir,
            "index_md": events.mask_text(idx_md, self.secrets),
            "pages": pages,
            "namespaces": NAMESPACE_LABELS,
        }

    def clear_repo_wiki(self, repo: str) -> Dict[str, Any]:
        """清空指定專案的 LLM Wiki 目錄、去重指紋與教訓紀錄，讓知識庫從頭重新累積。"""
        if not repo:
            return {"repo_id": "", "pages": [], "index_md": "", "namespaces": NAMESPACE_LABELS}
        self.flush(timeout=5.0)
        rid = repo_id(repo)
        wiki = self._ensure_wiki()
        with self._wiki_lock:
            if isinstance(wiki, LLMWikiEngine):
                wdir = wiki._proj_wiki_dir(rid)
                if os.path.isdir(wdir):
                    shutil.rmtree(wdir, ignore_errors=True)
        with self._dedup_lock:
            self._seen = {(r, fp) for (r, fp) in self._seen if r != rid}
            self._save_dedup()
        try:
            lessons_path = self._lessons.path(repo)
            if lessons_path and os.path.isfile(lessons_path):
                os.remove(lessons_path)
        except Exception:
            pass
        return self.wiki_view(repo, sync=False)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put(None)
            self._worker.join(timeout=2.0)
