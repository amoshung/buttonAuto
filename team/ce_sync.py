"""Compound Engineering (CE) 計畫同步器與房間交接（Handoff）管理。

讓房間的目標、階段里程碑與驗收條件直接跟隨 `docs/plans/*.md`（`/ce-plan` 產物）動態演進，
並在關閉／封存房間或開新房間接續開發時，完整保留歷次開發紀錄並寫入 LLM Wiki。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from team.usage import now_iso

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_UNIT_HEADING_RE = re.compile(
    r"^#{2,4}\s+(?:(U\d+[a-z]?|SP\d+|C\d+|階段\s*[A-Z0-9]+|Unit\s+\d+|Task\s+\d+|Step\s+\d+)[\s:：\-—]+)?(.+)$",
    re.I,
)
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.+)$")
_BULLET_CRIT_RE = re.compile(r"^\s*[-*]\s+\*\*([^*]+)\*\*[\s:：—\-]*(.+)$")
_PYTEST_CMD_RE = re.compile(r"`((?:python\s+-m\s+)?pytest[^`\n]+|npm\s+test[^`\n]*)`")


def _parse_frontmatter(text: str) -> Dict[str, str]:
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}
    out: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def _extract_objective(text: str) -> str:
    # 優先找 Goal Capsule 的 Objective
    m = re.search(r"-\s*\*\*Objective:\*\*\s*(.+)", text or "")
    if m:
        return m.group(1).strip()
    # 次選 Summary 段落
    m2 = re.search(r"###\s+Summary\s*\n+([^\n#]+)", text or "")
    if m2:
        return m2.group(1).strip()
    # 找第一個 H1 標題與後續第一段文字
    m3 = re.search(r"^#\s+([^\n]+)", text or "", re.M)
    if m3:
        return m3.group(1).strip()
    return ""


def _infer_default_test_cmd(repo: str) -> str:
    if not repo or not os.path.isdir(repo):
        return ""
    if os.path.isfile(os.path.join(repo, "Cargo.toml")):
        return "cargo test"
    if os.path.isfile(os.path.join(repo, "package.json")) and not os.path.isdir(os.path.join(repo, "tests")) and not os.path.isdir(os.path.join(repo, "backend")):
        return "npm test"
    if os.path.isdir(os.path.join(repo, "backend", "tests")):
        return "pytest backend/tests -q"
    if os.path.isdir(os.path.join(repo, "tests")) or os.path.isfile(os.path.join(repo, "pytest.ini")):
        return "pytest -q"
    return ""


def parse_ce_plan(path: str, repo: str = "") -> Dict[str, Any]:
    """解析單份 `docs/plans/*.md`，抽出專案最終目的（Objective）、實作單元（Units）與驗證條件。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    rel_path = os.path.relpath(path, repo).replace("\\", "/") if repo and path.startswith(os.path.abspath(repo)) else os.path.basename(path)
    return parse_ce_plan_text(
        text,
        filename=os.path.basename(path),
        rel_path=rel_path,
        mtime=mtime,
        path=path,
        repo=repo,
    )


def parse_ce_plan_text(
    text: str,
    *,
    filename: str = "plan.md",
    rel_path: str = "docs/plans/plan.md",
    mtime: float = 0.0,
    path: str = "",
    repo: str = "",
) -> Dict[str, Any]:
    """從 Markdown 純文字解析 CE 計畫（支援本機與 SSH 遠端計畫）。"""
    fm = _parse_frontmatter(text)
    title = fm.get("title") or os.path.splitext(filename)[0]
    objective = _extract_objective(text) or title

    # 萃取測試指令
    cmd_m = _PYTEST_CMD_RE.search(text)
    test_cmd = cmd_m.group(1).strip() if cmd_m else _infer_default_test_cmd(repo)

    # 萃取 Implementation Units / 階段里程碑 / Success Criteria
    units: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    # 若計畫檔內含 AI 外科細化同步區塊，優先讀取以確保「跟隨計畫同步里程碑」不遺失細化單元
    auto_m = re.search(
        r"<!--\s*AUTO_REFINED_UNITS_START\s*-->(.*?)<!--\s*AUTO_REFINED_UNITS_END\s*-->",
        text,
        re.S,
    )
    if auto_m:
        for line in auto_m.group(1).splitlines():
            cb = _CHECKBOX_RE.match(line)
            if not cb:
                continue
            done = cb.group(1).lower() == "x"
            body = cb.group(2).strip()
            chk = test_cmd
            chk_m = re.search(r"\s*\|\s*check:\s*`([^`]*)`\s*$", body)
            if chk_m:
                chk = chk_m.group(1).strip()
                body = body[: chk_m.start()].strip()
            id_m = re.match(r"^\*\*\[([A-Za-z0-9_.\-]+)\]\*\*\s*(.+)$", body)
            if id_m:
                uid = id_m.group(1).strip()
                body = id_m.group(2).strip()
            else:
                uid = f"U{len(units) + 1}"
            if uid not in seen_ids and len(units) < 120:
                seen_ids.add(uid)
                units.append({"id": uid, "text": body[:200], "check": chk, "done": done})
        if units:
            return {
                "filename": filename,
                "rel_path": rel_path,
                "title": title,
                "date": fm.get("date") or "",
                "objective": objective,
                "test_cmd": test_cmd,
                "units": units,
                "mtime": mtime,
                "path": path,
            }

    lines = text.splitlines()
    in_units_or_criteria = False
    for line in lines:
        s = line.strip()
        if re.match(r"^#{2,3}\s+(Implementation Units|Implementation Plan|Success Criteria|Verification Contract|執行步驟|階段規劃|Acceptance Criteria)", s, re.I):
            in_units_or_criteria = True
            continue
        if re.match(r"^#{2}\s+", s) and in_units_or_criteria and not re.search(r"(Unit|Criteria|Verification|階段|Step)", s, re.I):
            in_units_or_criteria = False

        cb = _CHECKBOX_RE.match(line)
        if cb:
            done = cb.group(1).lower() == "x"
            body = cb.group(2).strip()
            uid = f"U{len(units) + 1}"
            if uid not in seen_ids and len(units) < 15:
                seen_ids.add(uid)
                units.append({
                    "id": uid,
                    "text": body[:180],
                    "check": test_cmd,
                    "done": done,
                })
            continue

        hm = _UNIT_HEADING_RE.match(s)
        if hm and (hm.group(1) or in_units_or_criteria):
            raw_id = (hm.group(1) or f"U{len(units) + 1}").strip().replace(" ", "")
            heading_txt = hm.group(2).strip()
            if len(heading_txt) >= 3 and raw_id not in seen_ids and len(units) < 15:
                seen_ids.add(raw_id)
                units.append({
                    "id": raw_id,
                    "text": heading_txt[:180],
                    "check": test_cmd,
                    "done": bool(re.search(r"(已完成|DONE|✓|✅)", heading_txt, re.I)),
                })
            continue

        if in_units_or_criteria:
            bm = _BULLET_CRIT_RE.match(line)
            if bm and len(units) < 15:
                uid = f"C{len(units) + 1}"
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    units.append({
                        "id": uid,
                        "text": f"{bm.group(1).strip()}：{bm.group(2).strip()}"[:180],
                        "check": test_cmd,
                        "done": False,
                    })

    return {
        "path": path or rel_path,
        "rel_path": rel_path,
        "filename": filename,
        "title": title,
        "date": fm.get("date", ""),
        "topic": fm.get("topic", ""),
        "objective": objective,
        "test_cmd": test_cmd,
        "units": units,
        "mtime": mtime,
    }


def discover_ce_plans(repo: str, reference_root: str = "") -> List[Dict[str, Any]]:
    """掃描目標專案（及母資料夾 reference_root）的 `docs/plans/*.md`，新到舊排序。"""
    roots: List[str] = []
    if repo and os.path.isdir(repo):
        roots.append(os.path.abspath(repo))
    if reference_root and os.path.isdir(reference_root):
        abs_ref = os.path.abspath(reference_root)
        if os.path.normcase(abs_ref) not in {os.path.normcase(r) for r in roots}:
            roots.append(abs_ref)

    plans: List[Dict[str, Any]] = []
    seen_files: set[str] = set()
    for r in roots:
        pdir = os.path.join(r, "docs", "plans")
        if not os.path.isdir(pdir):
            continue
        for fn in os.listdir(pdir):
            if not fn.endswith(".md"):
                continue
            if fn.upper().startswith(("PROGRESS", "IMPROVEMENT_LEDGER", "GO_LIVE")):
                continue
            full = os.path.join(pdir, fn)
            norm = os.path.normcase(full)
            if norm in seen_files or not os.path.isfile(full):
                continue
            seen_files.add(norm)
            parsed = parse_ce_plan(full, repo=r)
            if parsed:
                plans.append(parsed)

    plans.sort(key=lambda p: (p.get("date") or "", p.get("mtime") or 0.0, p.get("filename") or ""), reverse=True)
    return plans


def sync_room_ce_plan(repo: str, reference_root: str = "", plan_rel: str = "") -> Dict[str, Any]:
    """回傳專案目前的 CE 計畫清單與當前應對齊的計畫（含整體目標與階段單元）。"""
    plans = discover_ce_plans(repo, reference_root=reference_root)
    active: Optional[Dict[str, Any]] = None
    if plan_rel:
        for p in plans:
            if p.get("rel_path") == plan_rel or p.get("filename") == os.path.basename(plan_rel):
                active = p
                break
    if active is None and plans:
        active = plans[0]

    if active is None:
        return {
            "has_plan": False,
            "active_plan": None,
            "plans": [],
            "objective": "",
            "criteria": [],
            "test_cmd": _infer_default_test_cmd(repo),
        }

    criteria = [
        {
            "id": str(u.get("id") or f"U{i + 1}"),
            "text": str(u.get("text") or ""),
            "check": str(u.get("check") or active.get("test_cmd") or ""),
            "done": bool(u.get("done", False)),
        }
        for i, u in enumerate(active.get("units") or [])
    ]
    return {
        "has_plan": True,
        "active_plan": active,
        "plans": [
            {
                "filename": p["filename"],
                "rel_path": p["rel_path"],
                "title": p["title"],
                "date": p["date"],
                "objective": p["objective"][:160],
                "unit_count": len(p.get("units") or []),
            }
            for p in plans[:15]
        ],
        "objective": active.get("objective") or "",
        "criteria": criteria,
        "test_cmd": active.get("test_cmd") or _infer_default_test_cmd(repo),
    }


def sync_remote_ce_plan(
    ssh_host: str,
    repo: str,
    *,
    reference_root: str = "",
    target_subdir: str = "",
    target_subdirs: Sequence[str] = (),
    plan_rel: str = "",
) -> Dict[str, Any]:
    """透過 SSH 遠端掃描 `docs/plans/*.md`（含子資料夾與母資料夾）並回傳 CE 計畫清單。"""
    if not ssh_host or not repo:
        return {"has_plan": False, "active_plan": None, "plans": [], "objective": "", "criteria": [], "test_cmd": ""}
    try:
        from team.ssh import remote_fs

        res = remote_fs(
            ssh_host,
            "list_plans",
            repo,
            reference_root=reference_root,
            target_subdir=target_subdir,
            target_subdirs=list(target_subdirs or ()),
            timeout=12.0,
        )
    except Exception:
        return {"has_plan": False, "active_plan": None, "plans": [], "objective": "", "criteria": [], "test_cmd": ""}

    raw_plans = res.get("plans") if isinstance(res, dict) else None
    if not isinstance(raw_plans, list):
        return {"has_plan": False, "active_plan": None, "plans": [], "objective": "", "criteria": [], "test_cmd": ""}

    plans: List[Dict[str, Any]] = []
    for item in raw_plans:
        if not isinstance(item, dict):
            continue
        parsed = parse_ce_plan_text(
            str(item.get("content") or ""),
            filename=str(item.get("filename") or "plan.md"),
            rel_path=str(item.get("rel_path") or "docs/plans/plan.md"),
            mtime=float(item.get("mtime") or 0.0),
        )
        if parsed:
            plans.append(parsed)
    plans.sort(key=lambda p: (p.get("date") or "", p.get("mtime") or 0.0, p.get("filename") or ""), reverse=True)

    active: Optional[Dict[str, Any]] = None
    if plan_rel:
        for p in plans:
            if p.get("rel_path") == plan_rel or p.get("filename") == os.path.basename(plan_rel):
                active = p
                break
    if active is None and plans:
        active = plans[0]
    if active is None:
        return {"has_plan": False, "active_plan": None, "plans": [], "objective": "", "criteria": [], "test_cmd": ""}

    criteria = [
        {
            "id": str(u.get("id") or f"U{i + 1}"),
            "text": str(u.get("text") or ""),
            "check": str(u.get("check") or active.get("test_cmd") or ""),
            "done": bool(u.get("done", False)),
        }
        for i, u in enumerate(active.get("units") or [])
    ]
    return {
        "has_plan": True,
        "active_plan": active,
        "plans": [
            {
                "filename": p["filename"],
                "rel_path": p["rel_path"],
                "title": p["title"],
                "date": p["date"],
                "objective": p["objective"][:160],
                "unit_count": len(p.get("units") or []),
            }
            for p in plans[:15]
        ],
        "objective": active.get("objective") or "",
        "criteria": criteria,
        "test_cmd": active.get("test_cmd") or "",
    }


def build_handoff_markdown(
    room: Any,
    events_list: Sequence[Any],
    commits: Sequence[Tuple[str, str]] = (),
    reason: str = "封存交接",
) -> str:
    """產出房間的 Compound Engineering 交接紀錄（Handoff Markdown），供寫入 LLM Wiki 與新房間無縫接續。"""
    ts = now_iso()
    lines = [
        f"# 房間開發交接紀錄（{getattr(room, 'name', '') or getattr(room, 'id', '')}）",
        "",
        f"- **房間 ID**：`{getattr(room, 'id', '')}`",
        f"- **交接時間**：`{ts}`（原因：{reason}）",
        f"- **工作資料夾 (Repo)**：`{getattr(room, 'repo', '')}`",
    ]
    if getattr(room, "reference_root", ""):
        lines.append(
            f"- **母資料夾唯讀範圍**：`{room.reference_root}`（目標可寫子資料夾：`{getattr(room, 'target_subdir', '')}`）"
        )
    if getattr(room, "branch", ""):
        lines.append(f"- **開發分支**：`{room.branch}`")
    lines.append(f"- **最後階段與狀態**：`phase={getattr(room, 'phase', '')}` / `status={getattr(room, 'status', '')}`")
    lines.append("")

    goal = (getattr(room, "goal", "") or "").strip()
    lines += [
        "## 1. 開發目標 (Goal)",
        "",
        goal or "（尚未填寫自訂目標，依專案 `docs/plans/` 推進）",
        "",
    ]

    criteria = getattr(room, "criteria", None) or []
    if criteria:
        lines += ["## 2. CE 計畫里程碑與驗收狀態", ""]
        for c in criteria:
            mark = "✅" if c.get("done") else "⬜"
            lines.append(f"- {mark} **{c.get('id', '?')}**：{c.get('text', '')}（驗證：`{c.get('check', 'manual')}`）")
        lines.append("")

    if commits:
        lines += ["## 3. 已完成 Commits", ""]
        for sha, subj in commits[:25]:
            lines.append(f"- `{sha}` {subj}")
        lines.append("")

    recent_ev = [e for e in (events_list or []) if getattr(e, "kind", "") in ("say", "phase", "merge", "decision", "user")]
    if recent_ev:
        lines += ["## 4. 關鍵開發事件摘要", ""]
        for e in recent_ev[-15:]:
            ts = (getattr(e, "ts", "") or "")[:19].replace("T", " ")
            txt = (getattr(e, "text", "") or "").strip()
            if txt:
                lines.append(f"- `{ts}` [{getattr(e, 'kind', '')}] {txt[:200]}")
        lines.append("")

    lines += [
        "## 5. 新房間接續開發指引",
        "",
        "- 開啟新房間時，系統已自動掛載本交接紀錄至 LLM Wiki (`[[rooms/handoff-" + str(getattr(room, "id", "")) + "]]`)。",
        "- 請實作代理先閱讀 `docs/plans/` 最新計畫與本交接紀錄，從尚未完成的實作單元繼續推進。",
        "",
    ]
    return "\n".join(lines)


def _ev_field(e: Any, key: str, default: str = "") -> str:
    if isinstance(e, dict):
        return str(e.get(key) or default)
    return str(getattr(e, key, default) or default)


def find_active_criterion(
    criteria: Sequence[Dict[str, Any]],
    activity: str = "",
    goal: str = "",
    events_list: Sequence[Any] = (),
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """找出目前正在執行的 CE 計畫單元／驗收條件索引與內容。"""
    crit_list = list(criteria or [])
    if not crit_list:
        return -1, None
    for i, c in enumerate(crit_list):
        if str(c.get("status") or "").lower() in ("running", "in_progress"):
            return i, c
    haystacks = [activity or ""]
    for ev in list(events_list or [])[:8]:
        haystacks.append(_ev_field(ev, "text"))
    if goal:
        haystacks.append(goal)
    for hay in haystacks:
        if not hay:
            continue
        for i, c in enumerate(crit_list):
            if c.get("done") or str(c.get("status") or "").lower() == "verified":
                continue
            cid = str(c.get("id") or "").strip()
            if cid and re.search(rf"\b{re.escape(cid)}\b", hay, re.I):
                return i, c
    for i, c in enumerate(crit_list):
        if not c.get("done") and str(c.get("status") or "").lower() != "verified":
            return i, c
    return -1, None


def build_room_progress_markdown(
    room: Any,
    events_list: Sequence[Any] = (),
    commits: Sequence[Any] = (),
    open_items: Sequence[str] = (),
    notes_dir: str = "",
    extra_note: str = "",
    events: Sequence[Any] = (),
    role_comms: Sequence[str] = (),
    supervisor_decisions: Sequence[str] = (),
) -> str:
    """產出每個房間專屬的即時 `progress.md`（斷點續傳檔案）。

    包含全部 7 個標準 `PROGRESS_SECTIONS`（目標、驗收條件狀態、已完成（commit）、正在做、下一步、未決問題、注意事項）、
    跨角色協作溝通紀錄，以及最新工作動態（DESC），確保房間在任何時刻中斷、暫停、重啟或切換座位時都能 100% 無縫接續。
    """
    eff_events = list(events_list or events or ())
    ts = now_iso()
    rid = getattr(room, "id", "")
    slug = getattr(room, "slug", "") or rid
    rname = getattr(room, "name", "") or slug
    repo = getattr(room, "repo", "")
    ref_root = getattr(room, "reference_root", "")
    t_subs = list(getattr(room, "target_subdirs", None) or [])
    t_sub_str = getattr(room, "target_subdir", "") or (", ".join(t_subs) if t_subs else "")
    phase = getattr(room, "phase", "goal")
    status = getattr(room, "status", "queued")
    activity = getattr(room, "activity", "") or ""
    goal = (getattr(room, "goal", "") or "").strip()
    criteria = list(getattr(room, "criteria", None) or [])
    test_cmd = getattr(room, "test_cmd", "") or ""
    allowed_paths = list(getattr(room, "allowed_paths", None) or [])
    branch = getattr(room, "branch", "") or ""
    worktree = getattr(room, "worktree", "") or ""

    active_idx, active_crit = find_active_criterion(criteria, activity=activity, goal=goal, events_list=eff_events)
    done_count = sum(1 for c in criteria if c.get("done") or str(c.get("status") or "").lower() == "verified")

    target_desc = (
        "、".join(f"`{s}/`" for s in t_subs)
        if len(t_subs) > 1
        else (f"`{t_sub_str}/`" if t_sub_str else f"`{repo}`")
    )

    header_lines = [
        f"# 進度（{slug}）",
        "",
        f"- **房間名稱**：`{rname}`（ID：`{rid}`）",
        f"- **最後存檔時間**：`{ts}`（自動斷點續傳保護中）",
        f"- **目前階段／狀態**：`phase={phase}` · `status={status}`",
        f"- **目標工作資料夾 B（可讀寫）**：{target_desc}（主工作區：`{repo}`）",
    ]
    if ref_root:
        header_lines.append(f"- **唯讀參考母資料夾 A**：`{ref_root}`（除目標 B 外皆為唯讀）")
    if branch or worktree:
        header_lines.append(f"- **開發分支／工作樹**：`{branch or 'main'}`（`{worktree or repo}`）")
    if active_crit:
        header_lines.append(
            f"- **⚡ 目前執行中單元**：`[{active_crit.get('id', '?')}]` {active_crit.get('text', '')}（已完成 {done_count}/{len(criteria)}）"
        )
    header_lines.append("")

    # 1. 目標
    sec_goal = goal or "（依專案 `docs/plans/` 最新 Compound Engineering 計畫推進）"

    # 2. 驗收條件狀態
    crit_lines: List[str] = []
    for i, c in enumerate(criteria):
        is_done = bool(c.get("done") or str(c.get("status") or "").lower() == "verified")
        box = "[x]" if is_done else "[ ]"
        cur_tag = "⚡ **【目前執行中】** " if (i == active_idx and not is_done) else ""
        chk = c.get("check") or "manual"
        crit_lines.append(f"- {box} {cur_tag}**{c.get('id', '?')}** {c.get('text', '')}（檢查：`{chk}`）")
    sec_criteria = "\n".join(crit_lines) if crit_lines else "（尚未建立階段單元，開工時將由 `/ce-plan` 自動同步）"

    # 3. 已完成（commit）
    commit_lines: List[str] = []
    for item in commits or ():
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            commit_lines.append(f"- `{item[0]}` {item[1]}")
        elif isinstance(item, str) and item.strip():
            commit_lines.append(f"- {item.strip()}")
    sec_commits = "\n".join(commit_lines) if commit_lines else "（尚無新 commit）"

    # 4. 正在做
    doing_parts = [f"階段：`{phase}`；狀態：`{status}`；即時動作：{activity or '待命中'}"]
    if active_crit:
        doing_parts.append(
            f"正在進行 CE 計畫單元（第 {active_idx + 1}/{len(criteria)} 項）：`[{active_crit.get('id', '?')}]` {active_crit.get('text', '')}"
        )
    if extra_note:
        doing_parts.append(f"補充註記：{extra_note.strip()}")
    sec_doing = "\n".join(f"- {p}" for p in doing_parts)

    # 5. 下一步
    nd_label = notes_dir or f"docs/ai-team/{slug}"
    if active_crit:
        sec_next = (
            f"接續完成單元 `[{active_crit.get('id', '?')}]`（{active_crit.get('text', '')}），"
            f"對照 `{nd_label}/spec.md` 與 `plan.md` 實作並跑測試指令：`{test_cmd or 'pytest -q'}`。"
        )
    else:
        sec_next = f"先讀 `{nd_label}/spec.md` 與 `plan.md`，依計畫完成尚未通過的條件；測試指令：`{test_cmd or '（未定）'}`"

    # 6. 未決問題
    sec_open = "\n".join(f"- {t}" for t in (open_items or ())) or "（無）"

    # 7. 注意事項
    scope_txt = "、".join(f"`{p}`" for p in allowed_paths) if allowed_paths else target_desc
    sec_notes = (
        f"本檔由房間自動維護作為斷點續傳依據；任何時候中斷重啟皆直接讀取本檔接續開發。"
        f"允許修改範圍：{scope_txt}"
    )

    sections = [
        ("目標", sec_goal),
        ("驗收條件狀態", sec_criteria),
        ("已完成（commit）", sec_commits),
        ("正在做", sec_doing),
        ("下一步", sec_next),
        ("未決問題", sec_open),
        ("注意事項", sec_notes),
    ]

    out = "\n".join(header_lines) + "\n"
    for title, content in sections:
        out += f"## {title}\n{content}\n\n"

    dec_lines: List[str] = [f"- {str(d).strip()}" for d in (supervisor_decisions or ()) if str(d).strip()]
    if dec_lines:
        out += "## 主管決策紀錄（最新在上）\n" + "\n".join(dec_lines[:10]) + "\n\n"

    # 附加跨角色協作溝通紀錄（包含代管拒絕原因、QA/Reviewer 意見、高階主管指引）
    comm_lines: List[str] = [f"- {c.strip()}" for c in (role_comms or ()) if str(c).strip()]
    if not comm_lines:
        for ev in reversed(eff_events):
            ev_kind = _ev_field(ev, "kind")
            ev_txt = _ev_field(ev, "text").strip()
            if ev_kind in ("say", "decision") or ("代管拒絕" in ev_txt or "自動拒絕" in ev_txt or "→" in ev_txt):
                ev_ts = _ev_field(ev, "ts")[:19].replace("T", " ")
                comm_lines.append(f"- `{ev_ts}` {ev_txt[:260]}")
                if len(comm_lines) >= 8:
                    break
    if comm_lines:
        out += "## 跨角色協作溝通紀錄（最新在上）\n" + "\n".join(comm_lines[:8]) + "\n\n"

    # 附加最新 10 筆工作動態（DESC 最新在上）方便人類與 AI 快速掌握中斷前最後動作
    ev_lines: List[str] = []
    for ev in list(reversed(eff_events))[:10]:
        ev_ts = _ev_field(ev, "ts")[:19].replace("T", " ")
        ev_txt = _ev_field(ev, "text").strip()
        if ev_txt:
            ev_lines.append(f"- `{ev_ts}` {ev_txt[:220]}")
    if ev_lines:
        out += "## 最新工作動態（DESC 最新在上）\n" + "\n".join(ev_lines) + "\n"

    return out


def write_room_progress_files(
    store_root: str,
    room: Any,
    events_list: Sequence[Any] = (),
    commits: Sequence[Tuple[str, str]] = (),
    open_items: Sequence[str] = (),
    notes_dir: str = "",
    extra_note: str = "",
) -> Dict[str, Any]:
    """將房間最新 `progress.md` 同步寫入 `.ai-team/<room_id>/progress.md` 與工作區 `docs/ai-team/<slug>/progress.md`。"""
    rid = getattr(room, "id", "")
    content = build_room_progress_markdown(
        room,
        events_list=events_list,
        commits=commits,
        open_items=open_items,
        notes_dir=notes_dir,
        extra_note=extra_note,
    )
    store_path = ""
    if store_root and rid:
        room_dir = os.path.join(store_root, rid)
        os.makedirs(room_dir, exist_ok=True)
        store_path = os.path.join(room_dir, "progress.md")
        try:
            with open(store_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except OSError:
            pass

    notes_path = ""
    nd = notes_dir or getattr(room, "notes_dir", "")
    if nd:
        try:
            os.makedirs(nd, exist_ok=True)
            notes_path = os.path.join(nd, "progress.md")
            with open(notes_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except OSError:
            notes_path = ""

    return {
        "ok": True,
        "room_id": rid,
        "store_path": store_path,
        "notes_path": notes_path,
        "updated_at": now_iso(),
        "markdown": content,
        "content": content,
        "target_subdirs": list(getattr(room, "target_subdirs", None) or []),
    }


def _read_repo_plan_text(repo: str, rel: str, *, ssh_host: str = "") -> str:
    """讀取本機或 SSH 遠端計畫檔內容（失敗回傳空字串）。"""
    rel_clean = (rel or "").strip().replace("\\", "/")
    if not repo or not rel_clean:
        return ""
    try:
        if ssh_host:
            from team.ssh import remote_exec
            q_rel = rel_clean.replace("'", "'\\''")
            r = remote_exec(ssh_host, repo, f"cat '{q_rel}'", shell=True, timeout=6.0)
            return str(r.get("stdout") or "")
        fp = os.path.join(repo, rel_clean.replace("/", os.sep))
        if os.path.isfile(fp):
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        pass
    return ""


def parse_numbered_subplan_text(text: str, *, source_name: str = "") -> List[Dict[str, Any]]:
    """當子計畫採用條列設計（如 `## Rust 設計` 底下的 `1. ...`）而非 `- [ ]` 時，自動擷取各階段與對應驗證指令。"""
    if not text.strip():
        return []
    verify_cmds = re.findall(r"(cargo\s+test[^\n`]+|pytest[^\n`]+|python3?\s+-m\s+unittest[^\n`]+)", text)
    items: List[Dict[str, Any]] = []
    in_design = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            in_design = any(k in s for k in ("設計", "步驟", "階段", "實作", "修補", "待辦", "任務", "Design", "Tasks", "Steps"))
            continue
        if in_design:
            m = re.match(r"^\d+\.\s+(.+)$", s)
            if m:
                title = m.group(1).strip()
                if not title or len(title) < 6:
                    continue
                chk = ""
                for vc in verify_cmds:
                    if any(tok in title for tok in vc.split() if len(tok) > 4):
                        chk = vc.strip()
                        break
                if not chk and verify_cmds:
                    chk = verify_cmds[0].strip()
                prefix = f"[{source_name}] " if source_name else ""
                items.append({"id": "", "text": f"{prefix}{title}", "check": chk, "done": False})
    return items


def expand_referenced_subplan_criteria(
    criteria: Sequence[Dict[str, Any]],
    repo: str,
    *,
    reference_root: str = "",
    target_subdir: str = "",
    target_subdirs: Sequence[str] = (),
    ssh_host: str = "",
    selected_plan_rel: str = "",
    max_depth: int = 3,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """依序展開里程碑中提及或透過「衍生自」反向關聯的 `docs/plans/*.md` 未完成子計畫項目。"""
    crit_list = [dict(c) for c in (criteria or ())]
    if not crit_list or not repo:
        return crit_list, []

    root_rel = (selected_plan_rel or "").strip().replace("\\", "/")
    root_base = os.path.basename(root_rel) if root_rel else ""

    all_plans: List[str] = []
    try:
        if ssh_host:
            from team.ssh import remote_exec
            r = remote_exec(ssh_host, repo, "ls -1 docs/plans/*.md 2>/dev/null | sort", shell=True, timeout=6.0)
            all_plans = [
                ln.strip().replace("\\", "/")
                for ln in str(r.get("stdout") or "").splitlines()
                if ln.strip().endswith(".md")
            ]
        else:
            pdir = os.path.join(repo, "docs", "plans")
            if os.path.isdir(pdir):
                all_plans = sorted(f"docs/plans/{fn}" for fn in os.listdir(pdir) if fn.endswith(".md"))
    except Exception:
        all_plans = []

    visited = {root_rel} if root_rel else set()
    used_ids = {str(c.get("id") or "").strip() for c in crit_list if str(c.get("id") or "").strip()}
    seen_titles = {str(c.get("text") or "").strip()[:120] for c in crit_list if str(c.get("text") or "").strip()}
    added_sources: List[str] = []

    _SPAWN_CUE_RE = re.compile(r"(產出|衍生|第二輪|下一輪|後續計畫|produced|follow-?up|replan|d1r2|\br2\b)", re.I)
    _SUBPLAN_DONE_RE = re.compile(r"(?im)^\s*(?:status|狀態)\s*[:：]\s*(done|completed|superseded|archived|已完成|已作廢|已結案)\b")
    _META_PREFIX_RE = re.compile(
        r"^(?:Goal|Requirements|Dependencies|Patterns\s+to\s+follow|Verification|目標|需求|依賴|驗證)\s*[:：]",
        re.I,
    )

    def _is_subplan_finished(rel: str) -> bool:
        head = "\n".join(_read_repo_plan_text(repo, rel, ssh_host=ssh_host).splitlines()[:35])
        return bool(_SUBPLAN_DONE_RE.search(head))

    def _extract_refs(text: str) -> List[str]:
        norm = (text or "").replace("\\", "/")
        refs: List[str] = []
        for m in re.findall(r"docs/plans/[A-Za-z0-9_.\-/]+\.md", norm):
            if m not in refs:
                refs.append(m)
        for pref in re.findall(r"docs/plans/[A-Za-z0-9_.\-/]+-", norm):
            for cand in all_plans:
                if cand.startswith(pref) and cand not in refs:
                    refs.append(cand)
        return refs

    def _load_raw(rel: str) -> List[Dict[str, Any]]:
        try:
            if ssh_host:
                info = sync_remote_ce_plan(
                    ssh_host,
                    repo,
                    reference_root=reference_root,
                    target_subdir=target_subdir,
                    target_subdirs=list(target_subdirs or ()),
                    plan_rel=rel,
                )
            else:
                info = sync_room_ce_plan(repo, reference_root=reference_root, plan_rel=rel)
            raw_c = list(info.get("criteria") or [])
            if not raw_c:
                txt = _read_repo_plan_text(repo, rel, ssh_host=ssh_host)
                raw_c = parse_numbered_subplan_text(txt, source_name=os.path.basename(rel))
            return raw_c
        except Exception:
            return []

    def _expand_file(rel: str, depth: int) -> List[Dict[str, Any]]:
        if depth > max_depth or rel in visited:
            return []
        visited.add(rel)
        if _is_subplan_finished(rel):
            return []
        raw_c = _load_raw(rel)
        out: List[Dict[str, Any]] = []
        added_here = 0
        for c in raw_c:
            if added_here >= 30 or len(used_ids) >= 120:
                break
            if c.get("done"):
                continue
            tk_full = str(c.get("text") or "").strip()
            if not tk_full or _META_PREFIX_RE.match(tk_full):
                continue
            tk = tk_full[:120]
            if tk in seen_titles:
                continue
            cid = f"EX_{len(used_ids) + 1}"
            while cid in used_ids:
                cid += "_x"
            used_ids.add(cid)
            seen_titles.add(tk)
            item = {
                "id": cid,
                "text": str(c.get("text") or "").strip(),
                "check": str(c.get("check") or "").strip(),
                "done": False,
                "status": "pending",
            }
            for nrel in _extract_refs(item["text"]):
                out.extend(_expand_file(nrel, depth + 1))
            out.append(item)
            added_here += 1
        if added_here > 0:
            bn = os.path.basename(rel)
            if bn not in added_sources:
                added_sources.append(bn)
        return out

    expanded: List[Dict[str, Any]] = []
    for c in crit_list:
        body = f"{c.get('text') or ''}\n{c.get('description') or ''}"
        is_done = bool(c.get("done") or str(c.get("status") or "").lower() in ("done", "waived", "verified"))
        # 已完成單元若只是回顧歷史計畫（如 U1/U4），不重展開舊計畫；僅在明確產出後續／下一輪計畫時才展開
        should_expand = (not is_done) or bool(_SPAWN_CUE_RE.search(body))
        refs = _extract_refs(body) if should_expand else []
        sub_items: List[Dict[str, Any]] = []
        for rpath in refs:
            sub_items.extend(_expand_file(rpath, 1))
        if is_done:
            expanded.append(c)
            expanded.extend(sub_items)
        else:
            expanded.extend(sub_items)
            expanded.append(c)

    # 反向檢查「衍生自（Parent Plan）」標註的子計畫（即使父計畫文字未明寫檔名，也依序納入）
    if root_rel:
        backlinked: List[Dict[str, Any]] = []
        for cand in all_plans:
            if cand in visited:
                continue
            head = "\n".join(_read_repo_plan_text(repo, cand, ssh_host=ssh_host).splitlines()[:30])
            if ("衍生自" in head or "Parent Plan" in head) and (root_rel in head or (root_base and root_base in head)):
                backlinked.extend(_expand_file(cand, 1))
        if backlinked:
            last_txt = str(expanded[-1].get("text") or "") if expanded else ""
            if expanded and any(k in last_txt for k in ("終止判定", "結案", "Verification")):
                expanded = expanded[:-1] + backlinked + [expanded[-1]]
            else:
                expanded.extend(backlinked)

    return expanded, added_sources


def persist_refined_units_to_plan(
    repo: str,
    plan_rel: str,
    units: Sequence[Dict[str, Any]],
    *,
    ssh_host: str = "",
) -> bool:
    """將 AI 外科細化或動態 Replan 後的單元清單寫回計畫檔的自動同步區塊，避免重載里程碑時失真。"""
    rel = (plan_rel or "").strip().replace("\\", "/")
    if not repo or not rel or not units:
        return False
    lines = [
        "<!-- AUTO_REFINED_UNITS_START -->",
        "## AI 外科細化執行里程碑（自動同步）",
        "",
    ]
    for u in list(units)[:120]:
        uid = str(u.get("id") or "").strip() or "?"
        title = str(u.get("title") or u.get("text") or "").strip().replace("\n", " ")[:200]
        chk = str(u.get("check_cmd") or u.get("check") or "").strip().replace("`", "")
        done = bool(u.get("done") or str(u.get("status") or "").lower() in ("done", "waived", "verified"))
        box = "x" if done else " "
        chk_part = f" | check: `{chk}`" if chk else ""
        lines.append(f"- [{box}] **[{uid}]** {title}{chk_part}")
    lines.append("<!-- AUTO_REFINED_UNITS_END -->")
    block = "\n".join(lines) + "\n"

    try:
        orig = _read_repo_plan_text(repo, rel, ssh_host=ssh_host)
        if not orig.strip():
            return False
        pat = re.compile(
            r"<!--\s*AUTO_REFINED_UNITS_START\s*-->.*?<!--\s*AUTO_REFINED_UNITS_END\s*-->\n?",
            re.S,
        )
        if pat.search(orig):
            updated = pat.sub(block, orig, count=1)
        else:
            updated = orig.rstrip() + "\n\n" + block
        if updated == orig:
            return True
        if ssh_host:
            import base64
            from team.ssh import remote_exec
            b64 = base64.b64encode(updated.encode("utf-8")).decode("ascii")
            q_rel = rel.replace("'", "'\\''")
            remote_exec(
                ssh_host,
                repo,
                f"python3 -c \"import base64; open('{q_rel}', 'wb').write(base64.b64decode('{b64}'))\"",
                shell=True,
                timeout=8.0,
            )
            return True
        fp = os.path.join(repo, rel.replace("/", os.sep))
        if os.path.isfile(fp):
            with open(fp, "w", encoding="utf-8", newline="\n") as f:
                f.write(updated)
            return True
    except Exception:
        return False
    return False
