"""EngineV2Runner：將 LangGraph StateGraph + D1Packer 銜接至既有的 Office / RoomStore / Drivers。"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from langgraph.types import Command

from engine.d1_gates import D1Packer
from engine.schemas import PlanSpec, UnitResult, UnitSpec, VerifyReport
from engine.state import RoomGraphState, open_sqlite_checkpointer
from engine.trace import RoomTracer
from engine.workflow import build_room_workflow
from team import ce_skills, ce_sync, gitops, personas
from team.ce_skills import role_ce_directive
from team.clis import CliInfo
from team.drivers import GLM_SEATS, make_driver, seat_launch
from team.events import AgentEvent, PermissionRequest, command_text
from team.gitops import GitError, Worktree
from team.models import ModelConfig, ModelRouter, call_ctx
from team.rooms import Room, RoomStore
from team.runner import _is_done_reply, _requests_fresh_session
from team.ssh import remote_exec
from team.usage import merge_tokens

log = logging.getLogger(__name__)

_QUOTA_PAT = re.compile(
    r"(?i)(quota|rate[\s_-]?limit|too many requests|429|usage limit|credit balance|limit reached|exceeded.*limit)"
)


def _is_quota_error(msg: str) -> bool:
    return bool(_QUOTA_PAT.search(msg or ""))


def _match_deny(cmd: str, patterns: Sequence[str]) -> str:
    for pat in patterns or ():
        if not pat:
            continue
        try:
            if re.search(pat, cmd or "", re.I):
                return pat
        except re.error:
            if pat.lower() in (cmd or "").lower():
                return pat
    return ""

_NODE_TO_PHASE = {
    "plan": "spec",
    "select_unit": "build",
    "implement": "build",
    "verify": "test",
    "d1_gate": "test",
    "review": "review",
    "commit": "build",
    "human_gate": "build",
    "final_merge": "review",
    "done": "done",
}

_NODE_TO_PERSONA = {
    "plan": "po",
    "implement": "developer",
    "verify": "qa",
    "d1_gate": "supervisor",
    "review": "reviewer",
    "commit": "developer",
    "human_gate": "supervisor",
    "done": "",
}


class _PausedV2(Exception):
    pass


class _V2Runtime:
    """實作 NodeRuntime 協定，將各節點接到真實 Worktree / CLI Driver / Decision-1 / RoomStore。"""

    def __init__(self, runner: "EngineV2Runner") -> None:
        self.r = runner
        self.d1 = D1Packer(router=runner.router, on_trace=runner.tracer.record)

    def set_node(self, node_name: str, activity: str, *, unit_id: str = "") -> None:
        phase = _NODE_TO_PHASE.get(node_name, self.r.room.phase or "build")
        persona = _NODE_TO_PERSONA.get(node_name, "")
        updates: Dict[str, Any] = {
            "graph_node": node_name,
            "phase": phase,
            "persona": persona,
            "activity": activity[:200],
        }
        if unit_id or node_name in ("done", "final_merge"):
            updates["current_unit_id"] = unit_id
        self.r._set(**updates)
        self.r.tracer.record("node", node=node_name, unit_id=unit_id, activity=activity[:200])

    def emit(self, kind: str, text: str, **extra: Any) -> None:
        self.r._event(kind, text, **extra)

    def check_pause(self) -> None:
        if self.r._pause_ev.is_set():
            raise _PausedV2()

    # -------- Plan --------
    def load_existing_units(self) -> Tuple[List[UnitSpec], str]:
        room = self.r.store.get(self.r.room_id) or self.r.room
        raw = list(room.criteria or [])
        is_collapsed_c1 = (
            len(raw) == 1
            and str(raw[0].get("id") or "") == "C1"
            and not raw[0].get("done")
            and bool(getattr(room, "selected_plan_rel", ""))
        )
        if raw and not is_collapsed_c1:
            loaded = [UnitSpec.from_criterion_dict(c, i + 1) for i, c in enumerate(raw)]
            return self._expand_referenced_subplans(loaded), room.test_cmd or ""
        # 嘗試從專案 docs/plans/*.md 同步（支援本機與 SSH 遠端，優先使用使用者選定的 plan_rel）
        sh = (getattr(room, "ssh_host", "") or "").strip()
        if room.repo:
            try:
                if sh:
                    info = ce_sync.sync_remote_ce_plan(
                        sh,
                        room.repo,
                        reference_root=room.reference_root,
                        target_subdir=room.target_subdir,
                        target_subdirs=list(getattr(room, "target_subdirs", None) or ()),
                        plan_rel=getattr(room, "selected_plan_rel", "") or "",
                    )
                else:
                    info = ce_sync.sync_room_ce_plan(
                        room.repo,
                        reference_root=room.reference_root,
                        plan_rel=getattr(room, "selected_plan_rel", "") or "",
                    )
                if info.get("criteria"):
                    loaded = [UnitSpec.from_criterion_dict(c, i + 1) for i, c in enumerate(info["criteria"])]
                    return (
                        self._expand_referenced_subplans(loaded),
                        str(info.get("test_cmd") or room.test_cmd or ""),
                    )
            except Exception:
                pass
        return [], room.test_cmd or ""

    def scan_triage_candidates(self) -> List[Dict[str, str]]:
        """通用掃描專案工作區內的程式碼檔案（最多 80 個代表檔案）供 Decision-1 批次分診。"""
        wt = self.r._wt
        if wt is None or getattr(wt, "ssh_host", "") or not os.path.isdir(wt.path):
            return []
        root = wt.path
        target_sub = (self.r.room.target_subdir or "").strip()
        if target_sub and "," not in target_sub:
            cand_root = os.path.join(root, target_sub)
            if os.path.isdir(cand_root):
                root = cand_root

        skip_dirs = {".git", "node_modules", "__pycache__", ".next", "target", "dist", "build", ".venv", "venv"}
        code_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".cpp", ".c", ".h", ".hpp", ".cs"}
        out: List[Dict[str, str]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs and not d.startswith(".")]
            for fn in sorted(filenames):
                _, ext = os.path.splitext(fn)
                if ext.lower() not in code_exts:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, wt.path).replace("\\", "/")
                try:
                    with open(full, encoding="utf-8", errors="ignore") as f:
                        content = f.read(1600)
                except OSError:
                    continue
                out.append({"id": rel, "label": rel, "content": content})
                if len(out) >= 80:
                    return out


        return out



    def _repo_snapshot_for_plan(self) -> str:
        """快速收集目標資料夾的淺層目錄結構與偵測到的測試框架，避免 Planner 盲猜路徑與指令。"""
        wt = self.r._wt
        cwd = wt.path if wt is not None else self.r.room.repo
        sh = (getattr(self.r.room, "ssh_host", "") or "").strip()
        if not cwd:
            return ""
        entries: List[str] = []
        runners: List[str] = []
        ignore = {".git", "node_modules", "target", ".venv", "venv", "__pycache__", ".ai-team-worktrees", "dist", "build"}
        if sh:
            try:
                res = remote_exec(
                    sh,
                    cwd,
                    "ls -1a | head -n 40",
                    shell=True,
                    timeout=5.0,
                )
                txt = str(res.get("stdout") or "").strip()
                if txt:
                    entries = [ln.strip() for ln in txt.splitlines() if ln.strip() and ln.strip() not in (".", "..")][:35]
            except Exception:
                return ""
        elif os.path.isdir(cwd):
            try:
                for root_name in sorted(os.listdir(cwd)):
                    if root_name in ignore or root_name.startswith(".pytest"):
                        continue
                    full = os.path.join(cwd, root_name)
                    if os.path.isdir(full):
                        entries.append(root_name + "/")
                        try:
                            for sub in sorted(os.listdir(full))[:8]:
                                if sub not in ignore:
                                    entries.append(f"  {root_name}/{sub}")
                        except OSError:
                            pass
                    else:
                        entries.append(root_name)
                    if len(entries) >= 40:
                        break
            except OSError:
                return ""
        joined_names = "\n".join(entries)
        if "Cargo.toml" in joined_names:
            runners.append("Rust (cargo test)")
        if "pyproject.toml" in joined_names or "pytest.ini" in joined_names or "backend/" in joined_names:
            runners.append("Python (pytest)")
        if "package.json" in joined_names:
            runners.append("Node (npm test / build)")
        if "go.mod" in joined_names:
            runners.append("Go (go test ./...)")
        if not entries:
            return ""
        lines = []
        if runners:
            lines.append("偵測到的測試框架：" + ", ".join(runners))
        lines.append("專案根目錄結構（節錄）：\n" + "\n".join(entries[:35]))
        return "\n".join(lines)

    def generate_plan(self, goal: str, *, triage_hint: str = "", refine_hint: str = "") -> PlanSpec:
        prompt_parts = [
            "請根據以下任務目標，拆解出依序執行的原子實作單元清單，只回傳合法 JSON（不要其他文字）：",
            '格式：{"global_test_cmd": "專案測試指令（可空）", "units": [{"id": "C1", "title": "一句話目標", "description": "具體做法", "scope_files": ["path/a.py"], "check_cmd": "單一可執行驗證指令"}]}',
            "原則：每單元必須夠小（適合單次獨立對話完成）、順序合理、每個 check_cmd 盡量具體可自動執行。",
            "嚴禁事項：禁止把已完成／已作廢的歷史舊計畫當成待辦單元，也嚴禁把 Goal/Requirements/Dependencies/Verification 等說明欄位拆成獨立單元；只規劃真正尚未完成的工作。",
            "驗證分級規範：純文件/盤點單元請用輕量檔案存在與非空檢查（勿跑全倉測試）；程式碼單元請優先指定對應模組測試，全倉測試留到結案前再跑。",
            "時間效率規範：下方已附專案目錄結構與測試框架，請勿地毯式遍歷全倉；最多快速查閱 2～3 個關鍵檔案後，即刻輸出合法 JSON。",
            f"【任務目標】\n{goal}",
        ]
        repo_snap = self._repo_snapshot_for_plan()
        if repo_snap:
            prompt_parts.append(f"【目標專案環境（請只引用真實存在或要新建的路徑）】\n{repo_snap}")
        if triage_hint:
            prompt_parts.append(f"【Decision-1 批次分診優先清單】\n{triage_hint}")
        if refine_hint:
            prompt_parts.append(f"【計畫品質修正要求】\n{refine_hint}")
        lineage_ctx = self._plan_lineage_context()
        if lineage_ctx:
            prompt_parts.append(f"【計畫譜系與衍伸計畫納入規範】\n{lineage_ctx}")
        prompt_parts.append(
            role_ce_directive(
                "tech_lead",
                notes_dir=self.r.store._dir(self.r.room_id),
                reference_root=self.r.room.reference_root,
                target_subdir=self.r.room.target_subdir,
                target_subdirs=list(getattr(self.r.room, "target_subdirs", None) or ()),
            )
        )
        prompt = "\n\n".join(prompt_parts)

        units: List[UnitSpec] = []
        global_test = self.r.room.test_cmd or ""
        raw = ""
        try:
            if not self.r.room.demo and self.r.clis:
                if not hasattr(self, "_cached_planner"):
                    from team.model_catalog import select_strongest_planner
                    self._cached_planner = select_strongest_planner(
                        self.r.clis,
                        store_root=self.r.store.root,
                        model_cfg=self.r.model_cfg,
                    )
                p_seat, p_model, p_reason = self._cached_planner
                if p_seat not in GLM_SEATS and p_seat in self.r.clis:
                    self.emit("notice", f"計畫規劃採用 Decision-1 選定最強免費模型：{p_seat}/{p_model}（{p_reason}）")
                    try:
                        raw = self.r._run_fresh_cli_turn(
                            p_seat,
                            p_model,
                            prompt,
                            read_only=True,
                            role="planner",
                            timeout_override=min(self.r.turn_timeout, 360.0),
                        )
                    except _PausedV2:
                        raise
                    except Exception as cli_e:
                        if self.r._pause_ev.is_set():
                            raise _PausedV2()
                        self.emit("notice", f"規劃座位 {p_seat} 失敗（{type(cli_e).__name__}），改用 GLM-5.3 備援規劃")
                        raw = ""
            if self.r._pause_ev.is_set():
                raise _PausedV2()
            if not raw.strip():
                raw = call_ctx(
                    self.r.router.write,
                    prompt,
                    effort="medium",
                    ctx={"room": self.r.room_id, "role": "po", "purpose": "v2_generate_plan"},
                    response_format={"type": "json_object"},
                )
            data = personas.parse_json_block(raw)
            if isinstance(data, dict):
                if data.get("global_test_cmd"):
                    global_test = str(data["global_test_cmd"]).strip()
                raw_units = data.get("units") or data.get("criteria") or []
                if isinstance(raw_units, list):
                    for idx, item in enumerate(raw_units, start=1):
                        if isinstance(item, dict):
                            units.append(UnitSpec.from_criterion_dict(item, idx))
        except _PausedV2:
            raise
        except Exception as e:
            if self.r._pause_ev.is_set():
                raise _PausedV2()
            self.emit("notice", f"自動拆解計畫失敗（將建立單一主單元）：{type(e).__name__}")

        if not units and not refine_hint:
            units = [UnitSpec(id="C1", title=goal[:160] or "完成任務目標", check_cmd=global_test)]
        return PlanSpec(
            objective=goal,
            task_mode="triage" if self.r.room.task_mode == "triage" else "standard",
            global_test_cmd=global_test,
            units=units,
        )

    def save_units(self, units: List[UnitSpec], global_test_cmd: str = "") -> None:
        crit_dicts = [u.to_criterion_dict() for u in units]
        updates: Dict[str, Any] = {"criteria": crit_dicts}
        if global_test_cmd:
            updates["test_cmd"] = global_test_cmd
        self.r._set(**updates)
        if units:
            summary = "\n".join(f"- [{u.id}] {u.title}（狀態={u.status}）" for u in units[:25])
            self.r.evolve_wiki(
                "plans",
                f"# 執行計畫狀態更新\n\n- 目標：{self.r.room.goal or ''}\n\n{summary}",
                fingerprint=f"v2:plan:{self.r.room_id}:{len(units)}:{hash(summary)}",
            )

    def persist_units_to_plan(self, units: List[UnitSpec]) -> None:
        room = self.r.room
        if not room or not room.repo or not getattr(room, "selected_plan_rel", ""):
            return
        sh = (getattr(room, "ssh_host", "") or "").strip()
        ce_sync.persist_refined_units_to_plan(
            room.repo,
            room.selected_plan_rel,
            [u.model_dump() for u in units],
            ssh_host=sh,
        )

    # -------- Implement (Fresh Session per Unit) --------
    def run_unit_implement(
        self,
        unit: UnitSpec,
        *,
        goal: str,
        recent_commits: str,
        error_tail: str = "",
        review_blocking: Sequence[str] = (),
        change_strategy: bool = False,
    ) -> str:
        seat, model = self.r._pick_seat("impl")
        self.r._set(impl_seat=seat, impl_model=model, context_pct=0.0, context_estimated=True)

        parts = [
            f"【專案總目標】\n{goal}",
            f"【本回合唯一鎖定單元】[{unit.id}] {unit.title}",
        ]
        if unit.description:
            parts.append(f"【單元說明】\n{unit.description}")
        if unit.scope_files:
            parts.append("【建議相關檔案】\n" + "\n".join(f"- {p}" for p in unit.scope_files))
        if unit.check_cmd:
            parts.append(f"【本單元驗收指令】\n`{unit.check_cmd}`")
        if recent_commits:
            parts.append(f"【近期提交（交接脈絡）】\n{recent_commits[:1200]}")
        if error_tail:
            parts.append(f"【上次確定性驗證失敗輸出（請直接對症修正）】\n```\n{error_tail[-3000:]}\n```")
        if review_blocking:
            parts.append("【審查阻斷項（請逐一修正）】\n" + "\n".join(f"- {b}" for b in review_blocking))
        if change_strategy:
            parts.append("【防死鎖提示】前次修法未解決同一個根因，請先重新定位問題並改採不同實作策略。")

        # 動態注入使用者在聊天框即時追加的指示
        user_notes = self.r.drain_user_msgs()
        if user_notes:
            joined_notes = "\n".join(f"- {m}" for m in user_notes)
            parts.insert(0, f"【使用者即時補充指示（請優先遵守）】\n{joined_notes}")
            self.r.tracer.record("user_inject", unit_id=unit.id, count=len(user_notes), preview=joined_notes[:300])

        # 動態注入 LLM Wiki 既有知識（隨前面單元與專案文件演化）
        wiki_ctx = self.r.query_wiki_context(f"{unit.id} {unit.title} {unit.description}")
        if wiki_ctx:
            parts.append(f"【專案 LLM Wiki 演化知識（避免重複踩坑）】\n{wiki_ctx}")

        lineage_ctx = self._plan_lineage_context()
        if lineage_ctx:
            parts.append(f"【計畫譜系與衍伸計畫清單】\n{lineage_ctx}")
        ce_role = "fixer" if (error_tail or review_blocking) else "implementer"
        parts.append(
            role_ce_directive(
                ce_role,
                notes_dir=self.r.store._dir(self.r.room_id),
                reference_root=self.r.room.reference_root,
                target_subdir=self.r.room.target_subdir,
                target_subdirs=list(getattr(self.r.room, "target_subdirs", None) or ()),
            )
        )
        parts.append(
            "要求：\n"
            "1. 本對話為全新 Fresh Session，請**只完成上述單一單元**，勿跳去做其他單元。\n"
            "2. 實作完成後請自行跑過驗證指令確認通過，並整理好工作區（可直接 commit 或留待系統提交）。\n"
            "3. 完成後請在最後單獨一行回覆：DONE"
        )
        prompt = "\n\n".join(parts)
        reply = self.r._run_fresh_cli_turn(seat, model, prompt, read_only=False, role="developer")
        if reply and reply.strip():
            self.r.evolve_wiki(
                "rooms",
                f"# 實作進展：[{unit.id}] {unit.title}\n\n{reply.strip()[-900:]}",
                fingerprint=f"v2:impl:{unit.id}:{hash(reply[-300:])}",
                sync_docs=False,
            )
        return reply

    # -------- Verify (SWE-agent Deterministic Check，支援本機與 SSH 遠端) --------
    def git_head_sha(self) -> str:
        wt = self.r._wt
        if wt is None:
            return ""
        try:
            return gitops.git(wt.path, "rev-parse", "HEAD", ssh_host=wt.ssh_host).strip()
        except Exception:
            return ""

    def discover_subplan_units(
        self,
        existing_ids: Sequence[str],
        existing_titles: Sequence[str],
    ) -> Tuple[List[UnitSpec], List[str]]:
        """偵測本回合新建或修改的 `docs/plans/*.md` 子計畫（排除總綱），自動解析未勾選任務展開為 UnitSpec。"""
        wt = self.r._wt
        room = self.r.room
        if wt is None or not room or not room.repo:
            return [], []
        root_rel = str(getattr(room, "selected_plan_rel", "") or "").strip().replace("\\", "/")
        changed_files: List[str] = []
        try:
            head_files = gitops.git(wt.path, "show", "--name-only", "--format=", "HEAD", ssh_host=wt.ssh_host)
            changed_files.extend(ln.strip().replace("\\", "/") for ln in head_files.splitlines() if ln.strip())
        except Exception:
            pass
        try:
            porc = gitops.git(wt.path, "status", "--porcelain", ssh_host=wt.ssh_host)
            for ln in porc.splitlines():
                if len(ln) > 3:
                    changed_files.append(ln[3:].strip().replace("\\", "/"))
        except Exception:
            pass

        subplans: List[str] = []
        for p in changed_files:
            if not p.endswith(".md") or "docs/plans/" not in p:
                continue
            if root_rel and (p == root_rel or p.endswith("/" + root_rel) or root_rel.endswith("/" + p)):
                continue
            if p not in subplans:
                subplans.append(p)
        if not subplans:
            return [], []

        sh = (getattr(room, "ssh_host", "") or "").strip()
        used_ids = set(str(x) for x in existing_ids if x)
        seen_titles = {str(t).strip()[:120] for t in existing_titles if str(t).strip()}
        out_units: List[UnitSpec] = []
        sources: List[str] = []
        for s_idx, rel in enumerate(subplans[:6], start=1):
            try:
                if sh:
                    info = ce_sync.sync_remote_ce_plan(
                        sh,
                        room.repo,
                        reference_root=room.reference_root,
                        target_subdir=room.target_subdir,
                        target_subdirs=list(getattr(room, "target_subdirs", None) or ()),
                        plan_rel=rel,
                    )
                else:
                    info = ce_sync.sync_room_ce_plan(
                        room.repo,
                        reference_root=room.reference_root,
                        plan_rel=rel,
                    )
                raw_crit = list(info.get("criteria") or [])
                if not raw_crit:
                    raw_crit = self._parse_numbered_subplan_fallback(wt, rel, sh=sh)
                added_here = 0
                for c_idx, c in enumerate(raw_crit, start=1):
                    if c.get("done"):
                        continue
                    title_key = str(c.get("text") or "").strip()[:120]
                    if not title_key or title_key in seen_titles:
                        continue
                    u = UnitSpec.from_criterion_dict(c, len(out_units) + 1)
                    if not u.id or u.id in used_ids:
                        u.id = f"SP{s_idx}_{c_idx}"
                    while u.id in used_ids:
                        u.id = f"{u.id}_x"
                    used_ids.add(u.id)
                    seen_titles.add(title_key)
                    u.status = "pending"
                    out_units.append(u)
                    added_here += 1
                if added_here > 0:
                    sources.append(os.path.basename(rel))
            except Exception:
                continue
        if out_units:
            out_units = self._expand_referenced_subplans(out_units)
        return out_units, sources

    def _list_plan_files(self) -> List[str]:
        wt = self.r._wt
        room = self.r.room
        base = wt.path if wt is not None else (room.repo if room else "")
        sh = (getattr(room, "ssh_host", "") or "").strip() if room else ""
        if not base:
            return []
        try:
            if sh:
                r = remote_exec(sh, base, "ls -1 docs/plans/*.md 2>/dev/null | sort", shell=True, timeout=6.0)
                return [ln.strip().replace("\\", "/") for ln in str(r.get("stdout") or "").splitlines() if ln.strip().endswith(".md")]
            pdir = os.path.join(base, "docs", "plans")
            if os.path.isdir(pdir):
                return sorted(f"docs/plans/{fn}" for fn in os.listdir(pdir) if fn.endswith(".md"))
        except Exception:
            pass
        return []

    def _plan_lineage_context(self) -> str:
        """整理目前總綱與所有衍伸子計畫（docs/plans/*.md）清單，讓後續 Agent 掌握計畫譜系與納入規則。"""
        room = self.r.room
        root_rel = str(getattr(room, "selected_plan_rel", "") or "").strip().replace("\\", "/") if room else ""
        plans = self._list_plan_files()
        lines: List[str] = []
        if root_rel:
            lines.append(f"- 主總綱計畫：{root_rel}")
        sub_plans = [p for p in plans if not root_rel or (p != root_rel and not p.endswith("/" + root_rel) and not root_rel.endswith("/" + p))]
        if sub_plans:
            lines.append("- 已登記之衍伸／關聯子計畫（系統會自動展開其未完成項目並依序排入房間計畫清單）：")
            for sp in sub_plans[:15]:
                lines.append(f"  - {sp}")
        lines.append(
            "- 若本回合需形成新的後續／衍伸計畫（`docs/plans/*.md`），請務必在檔頭註明"
            "「`> 衍生自（Parent Plan）：" + (root_rel or "docs/plans/<主計畫>.md") + "`」，"
            "以 `- [ ]` 條列待辦與 `## 驗證` 指令，並在回覆／單元摘要中明確列出新計畫路徑，系統將自動納入計畫清單由後續 Agent 依序執行。"
        )
        return "\n".join(lines)

    def _load_subplan_raw_criteria(self, rel: str) -> List[Dict[str, Any]]:
        wt = self.r._wt
        room = self.r.room
        if not room or not room.repo:
            return []
        sh = (getattr(room, "ssh_host", "") or "").strip()
        try:
            if sh:
                info = ce_sync.sync_remote_ce_plan(
                    sh,
                    room.repo,
                    reference_root=room.reference_root,
                    target_subdir=room.target_subdir,
                    target_subdirs=list(getattr(room, "target_subdirs", None) or ()),
                    plan_rel=rel,
                )
            else:
                info = ce_sync.sync_room_ce_plan(
                    room.repo,
                    reference_root=room.reference_root,
                    plan_rel=rel,
                )
            raw_c = list(info.get("criteria") or [])
            if not raw_c:
                wt_or_repo = wt if wt is not None else type("_W", (), {"path": room.repo, "ssh_host": sh})()
                raw_c = self._parse_numbered_subplan_fallback(wt_or_repo, rel, sh=sh)
            return raw_c
        except Exception:
            return []

    def _expand_referenced_subplans(self, units: List[UnitSpec], *, max_depth: int = 3) -> List[UnitSpec]:
        """若計畫單元內提及延伸子計畫（如 `docs/plans/2026-11-20-d1r2-*.md`），依序遞迴展開其未完成項目。"""
        room = self.r.room
        if not units or not room or not room.repo:
            return units
        root_rel = str(getattr(room, "selected_plan_rel", "") or "").strip().replace("\\", "/")
        all_plans = self._list_plan_files()
        visited = {root_rel} if root_rel else set()
        used_ids = {u.id for u in units if u.id}
        seen_titles = {u.title.strip()[:120] for u in units if u.title.strip()}

        def _extract_refs(text: str) -> List[str]:
            refs: List[str] = []
            # 1. 明確檔案路徑 docs/plans/xxx.md
            for m in re.findall(r"docs/plans/[A-Za-z0-9_.\-/]+\.md", text or ""):
                if m not in refs:
                    refs.append(m)
            # 2. 前綴形式 docs/plans/2026-11-20-d1r2-*
            for pref in re.findall(r"docs/plans/[A-Za-z0-9_.\-/]+-", text or ""):
                for cand in all_plans:
                    if cand.startswith(pref) and cand not in refs:
                        refs.append(cand)
            return refs

        def _expand_file(rel: str, depth: int) -> List[UnitSpec]:
            if depth > max_depth or rel in visited:
                return []
            visited.add(rel)
            raw_c = self._load_subplan_raw_criteria(rel)
            out: List[UnitSpec] = []
            for idx, c in enumerate(raw_c, start=1):
                if c.get("done"):
                    continue
                tk = str(c.get("text") or "").strip()[:120]
                if not tk or tk in seen_titles:
                    continue
                u = UnitSpec.from_criterion_dict(c, idx)
                cid = f"EX_{len(used_ids) + 1}"
                while cid in used_ids:
                    cid += "_x"
                u.id = cid
                u.status = "pending"
                used_ids.add(u.id)
                seen_titles.add(tk)
                # 若子計畫本身又延伸出下一層子計畫，先遞迴展開
                nested_refs = _extract_refs(f"{u.title}\n{u.description}")
                for nrel in nested_refs:
                    out.extend(_expand_file(nrel, depth + 1))
                out.append(u)
            return out

        _spawn_cue = re.compile(r"(產出|衍生|第二輪|下一輪|後續計畫|produced|follow-?up|replan|d1r2|\br2\b)", re.I)
        expanded: List[UnitSpec] = []
        for u in units:
            body = f"{u.title}\n{u.description}"
            should_expand = (u.status not in ("done", "waived")) or bool(_spawn_cue.search(body))
            refs = _extract_refs(body) if should_expand else []
            sub_units: List[UnitSpec] = []
            for rpath in refs:
                sub_units.extend(_expand_file(rpath, 1))
            if u.status in ("done", "waived"):
                expanded.append(u)
                expanded.extend(sub_units)
            else:
                # 未完成的結尾/統整單元：先把延伸子計畫依序排在它前面做完
                expanded.extend(sub_units)
                expanded.append(u)
        return expanded

    @staticmethod
    def _parse_numbered_subplan_fallback(wt: Any, rel: str, *, sh: str = "") -> List[Dict[str, Any]]:
        """當子計畫採用條列設計（如 `## Rust 設計` 底下的 `1. ...`）而非 `- [ ]` 時，自動擷取各階段與對應驗證指令。"""
        text = ""
        try:
            if sh:
                import shlex
                r = remote_exec(sh, wt.path, f"cat {shlex.quote(rel)}", shell=True, timeout=8.0)
                text = str(r.get("stdout") or "")
            else:
                full = os.path.join(wt.path, rel.replace("/", os.sep))
                if os.path.isfile(full):
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
        except Exception:
            return []
        if not text.strip():
            return []

        section = ""
        design_items: List[str] = []
        verify_cmds: List[str] = []
        cur_item: List[str] = []
        in_design = False
        in_verify = False
        for ln in text.splitlines():
            h = re.match(r"^##\s+(.+)$", ln.strip())
            if h:
                if cur_item:
                    design_items.append("\n".join(cur_item).strip())
                    cur_item = []
                section = h.group(1).strip()
                in_design = any(k in section for k in ("設計", "實作", "步驟", "Task", "Unit", "Implementation", "Plan"))
                in_verify = any(k in section for k in ("驗證", "Verification", "Test"))
                continue
            if in_design:
                m_top = re.match(r"^\d+\.\s+(.+)$", ln)
                if m_top:
                    if cur_item:
                        design_items.append("\n".join(cur_item).strip())
                    cur_item = [m_top.group(1).strip()]
                elif cur_item and (ln.startswith("   ") or ln.startswith("\t") or ln.strip().startswith("- ")):
                    cur_item.append(ln.strip())
            elif in_verify:
                m_cmd = re.search(r"`([^`]+)`", ln)
                if m_cmd:
                    verify_cmds.append(m_cmd.group(1).strip())
        if cur_item:
            design_items.append("\n".join(cur_item).strip())

        out: List[Dict[str, Any]] = []
        default_cmd = verify_cmds[-1] if verify_cmds else ""
        for idx, body in enumerate(design_items, start=1):
            matched = ""
            body_low = body.lower()
            for vc in verify_cmds:
                # 擷取如 bof-datalink / bof-types / framework-core / manager_routes 等關鍵詞比對
                tokens = [t for t in re.findall(r"[a-zA-Z0-9_-]{4,}", vc.lower()) if t not in ("cargo", "test", "locked", "python", "python3", "unittest", "discover", "pytest")]
                if any(t in body_low for t in tokens):
                    matched = vc
                    break
            cmd = matched or (verify_cmds[idx - 1] if (not matched and len(design_items) == len(verify_cmds)) else default_cmd)
            out.append({"id": f"S{idx}", "text": body, "check": cmd, "done": False})
        return out



    def has_unit_changes(self, head_before: str) -> bool:
        wt = self.r._wt
        if wt is None or not head_before:
            return True
        try:
            dirty = gitops.git(wt.path, "status", "--porcelain", ssh_host=wt.ssh_host).strip()
            if dirty:
                return True
            if head_before:
                cur_head = self.git_head_sha()
                if cur_head and cur_head != head_before:
                    return True
            return False
        except Exception:
            return True

    @staticmethod
    def _is_doc_or_inventory_unit(unit: UnitSpec) -> bool:
        txt = f"{unit.title} {unit.description}".lower()
        return bool(
            re.search(r"(\.md\b|\.json\b|盤點|基線|矩陣|清單|清冊|報告|對照表|audit|inventory|matrix)", txt)
        )

    def run_unit_verify(self, unit: UnitSpec, global_test_cmd: str) -> VerifyReport:
        unit_cmd = (unit.check_cmd or "").strip()
        g_cmd = (global_test_cmd or "").strip()
        # 純文件／盤點單元若未指定專屬驗證指令，不硬跑整包全域測試（避免被無關既有測試拖死）
        if not unit_cmd and self._is_doc_or_inventory_unit(unit):
            cmd = ""
        else:
            cmd = unit_cmd or g_cmd
        if not cmd or cmd.lower() == "manual":
            rep = VerifyReport(
                unit_id=unit.id,
                cmd=cmd or "artifact-check",
                exit_code=0,
                seconds=0.0,
                tail_output="（本單元已確認有實際檔案產出或變更，直接進入 Decision-1 15 題審查閘門）",
                passed=True,
            )
            self.r.tracer.record("verify", unit_id=unit.id, cmd=rep.cmd, remote=False, exit_code=0, passed=True, seconds=0.0)
            return rep

        hit = _match_deny(cmd, self.r.deny_patterns)
        if hit:
            rep = VerifyReport(
                unit_id=unit.id,
                cmd=cmd,
                exit_code=126,
                seconds=0.0,
                tail_output=f"驗證指令命中危險規則（{hit}），拒絕自動執行：{cmd}",
                passed=False,
            )
            self.r.tracer.record("verify", unit_id=unit.id, cmd=cmd, exit_code=126, passed=False, denied_by=hit)
            return rep

        cwd = self.r._wt.path if self.r._wt is not None else self.r.room.repo
        sh = (getattr(self.r.room, "ssh_host", "") or "").strip()
        t0 = time.monotonic()
        if sh:
            # SSH 遠端工作區執行確定性驗證
            try:
                res = remote_exec(sh, cwd, cmd, shell=True, timeout=300.0)
                elapsed = round(time.monotonic() - t0, 2)
                code = int(res.get("exit_code", res.get("returncode", 1)))
                timed_out = bool(res.get("timed_out", False))
                combined = ((str(res.get("stdout") or "")) + "\n" + (str(res.get("stderr") or ""))).strip()
                if not combined and res.get("error"):
                    combined = str(res["error"])
                tail = "\n".join(combined.splitlines()[-60:])[-4000:]
                rep = VerifyReport(
                    unit_id=unit.id,
                    cmd=cmd,
                    exit_code=code,
                    seconds=elapsed,
                    tail_output=tail,
                    passed=(code == 0 and not timed_out),
                    timed_out=timed_out,
                )
            except Exception as e:
                elapsed = round(time.monotonic() - t0, 2)
                rep = VerifyReport(
                    unit_id=unit.id,
                    cmd=cmd,
                    exit_code=1,
                    seconds=elapsed,
                    tail_output=f"SSH 遠端驗證執行失敗（{sh}）：{type(e).__name__}: {e}",
                    passed=False,
                )
            self.r.tracer.record(
                "verify",
                unit_id=unit.id,
                cmd=cmd,
                remote=True,
                ssh_host=sh,
                exit_code=rep.exit_code,
                passed=rep.passed,
                seconds=rep.seconds,
                tail_preview=rep.tail_output[-300:],
            )
            if not rep.passed:
                self.r.evolve_wiki(
                    "verify_lesson",
                    f"單元 [{unit.id}] {unit.title} 遠端驗證失敗（cmd=`{rep.cmd}`, exit={rep.exit_code}）：\n{rep.tail_output[-800:]}",
                    fingerprint=f"v2:verify:{unit.id}:{rep.exit_code}:{len(rep.tail_output)}",
                )
            return rep

        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with self.r._lock:
                self.r._active_verify_proc = proc
            out_buf: List[str] = []
            err_buf: List[str] = []

            def _drain(stream: Any, buf: List[str]) -> None:
                try:
                    if stream is not None:
                        buf.append(stream.read())
                except Exception:
                    pass

            t_out = threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, err_buf), daemon=True)
            t_out.start()
            t_err.start()
            deadline = time.monotonic() + 300.0
            while proc.poll() is None:
                if self.r._pause_ev.is_set():
                    try:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=3)
                        else:
                            proc.kill()
                    except Exception:
                        pass
                    raise _PausedV2()
                if time.monotonic() >= deadline:
                    try:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=3)
                        else:
                            proc.kill()
                    except Exception:
                        pass
                    raise subprocess.TimeoutExpired(cmd, 300)
                time.sleep(0.2)
            t_out.join(2)
            t_err.join(2)
            elapsed = round(time.monotonic() - t0, 2)
            combined = (("".join(out_buf)) + "\n" + ("".join(err_buf))).strip()
            tail_lines = combined.splitlines()[-60:]
            tail = "\n".join(tail_lines)[-4000:]
            rep = VerifyReport(
                unit_id=unit.id,
                cmd=cmd,
                exit_code=int(proc.returncode or 0),
                seconds=elapsed,
                tail_output=tail,
                passed=(proc.returncode == 0),
            )
        except _PausedV2:
            raise
        except subprocess.TimeoutExpired:
            elapsed = round(time.monotonic() - t0, 2)
            rep = VerifyReport(
                unit_id=unit.id,
                cmd=cmd,
                exit_code=124,
                seconds=elapsed,
                tail_output=f"驗證指令逾時（>{elapsed:.0f}s）：{cmd}",
                passed=False,
                timed_out=True,
            )
        finally:
            with self.r._lock:
                self.r._active_verify_proc = None
        self.r.tracer.record(
            "verify",
            unit_id=unit.id,
            cmd=cmd,
            remote=False,
            exit_code=rep.exit_code,
            passed=rep.passed,
            seconds=rep.seconds,
            tail_preview=rep.tail_output[-300:],
        )
        if not rep.passed:
            self.r.evolve_wiki(
                "verify_lesson",
                f"單元 [{unit.id}] {unit.title} 驗證失敗（cmd=`{rep.cmd}`, exit={rep.exit_code}）：\n{rep.tail_output[-800:]}",
                fingerprint=f"v2:verify:{unit.id}:{rep.exit_code}:{len(rep.tail_output)}",
            )
        return rep

    # -------- Review --------
    def git_diff_info(self) -> Tuple[str, str, str]:
        wt = self.r._wt
        if wt is None:
            return "", "", ""
        try:
            stat = gitops.diff_stat(wt)
        except Exception:
            stat = ""
        try:
            diff_txt = gitops.diff_text(wt)
        except Exception:
            diff_txt = ""
        try:
            porcelain = gitops.git(wt.path, "status", "--porcelain", ssh_host=wt.ssh_host).strip()
        except Exception:
            porcelain = ""
        if porcelain:
            untracked = [ln[3:].strip() for ln in porcelain.splitlines() if ln.startswith("?? ") and ln[3:].strip()]
            if untracked:
                stat = (stat + "\n" if stat else "") + "\n".join(f"?? {p}" for p in untracked[:30])
                new_snippets: List[str] = []
                for rel in untracked[:8]:
                    if rel.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".png", ".jpg", ".zip", ".exe", ".dll")):
                        continue
                    try:
                        if wt.ssh_host:
                            import shlex
                            r_cat = remote_exec(wt.ssh_host, wt.path, f"head -c 3000 {shlex.quote(rel)}", shell=True, timeout=5.0)
                            body = str(r_cat.get("stdout") or "")
                        else:
                            full = os.path.join(wt.path, rel.replace("/", os.sep))
                            if os.path.isfile(full):
                                with open(full, "r", encoding="utf-8", errors="replace") as f:
                                    body = f.read(3000)
                            else:
                                body = ""
                        if body.strip():
                            new_snippets.append(f"+++ new file: {rel}\n{body}")
                    except Exception:
                        pass
                if new_snippets:
                    diff_txt = (diff_txt + "\n\n" if diff_txt.strip() else "") + "\n\n".join(new_snippets)
        elif not (diff_txt or "").strip():
            # 只有當工作區完全乾淨（代表代理已在本回合自行 commit），才改取最近一筆 commit 的變更供審查
            try:
                stat = gitops.git(wt.path, "show", "--stat", "--format=", "HEAD", ssh_host=wt.ssh_host).strip() or stat
                diff_txt = gitops.git(wt.path, "show", "--format=", "HEAD", ssh_host=wt.ssh_host).strip()
            except Exception:
                pass
        try:
            commits = "\n".join(f"{h[:7]} {m}" for h, m in gitops.commits_since(wt)[-8:])
        except Exception:
            commits = ""
        return stat, diff_txt, commits

    def run_micro_review(
        self,
        unit: UnitSpec,
        *,
        goal: str,
        diff_stat: str,
        diff_text: str,
        test_tail: str,
        focus_areas: Sequence[str],
    ) -> Optional[Tuple[List[str], List[str]]]:
        seat, model = self.r._pick_seat("review")
        self.r._set(review_seat=seat, review_model=model)

        focus_block = (
            "【Decision-1 預篩標記的高風險／低分焦點（請優先檢查）】\n"
            + "\n".join(f"- {f}" for f in focus_areas)
            if focus_areas
            else "（無特定異常焦點，請做精簡正確性與邊界抽查）"
        )
        clipped_diff = diff_text[:8000]
        if len(diff_text) > 8000:
            clipped_diff += f"\n\n[... diff 已節錄前 8000 字元（原長 {len(diff_text)} 字元），完整變更檔案請見上方變更統計或於工作區唯讀查閱 ...]"

        ce_rev = role_ce_directive(
            "reviewer",
            notes_dir=self.r.store._dir(self.r.room_id),
            reference_root=self.r.room.reference_root,
            target_subdir=self.r.room.target_subdir,
            target_subdirs=list(getattr(self.r.room, "target_subdirs", None) or ()),
        )
        prompt = (
            f"請以唯讀程式碼審查者身分，只針對以下單一單元進行 Micro-Review。\n\n"
            f"【專案總目標】{goal}\n"
            f"【本單元】[{unit.id}] {unit.title}\n\n"
            f"{focus_block}\n\n"
            f"【變更統計】\n{diff_stat[:2000]}\n\n"
            f"【驗證通過輸出】\n{test_tail[-1500:]}\n\n"
            f"【本單元 Diff】\n{clipped_diff}\n\n"
            f"{ce_rev}\n\n"
            '請務必在回覆最後輸出合法 JSON：{"blocking": ["[嚴重度] 檔案:行號 — 問題"], "suggestions": ["建議"], "compound_learnings": ["重要架構決策或避坑教訓（可空）"]}；'
            '若無阻斷問題請回傳 {"blocking": [], "suggestions": [], "compound_learnings": []}。'
        )

        reply = ""
        try:
            reply = self.r._run_fresh_cli_turn(
                seat,
                model,
                prompt,
                read_only=True,
                role="reviewer",
                timeout_override=min(self.r.turn_timeout, 300.0),
            )
        except _PausedV2:
            raise
        except Exception as e:
            if self.r._pause_ev.is_set():
                raise _PausedV2()
            self.emit("notice", f"審查座位 {seat} 失敗（{type(e).__name__}），改用 GLM-5.3 補位審查")
            try:
                reply = call_ctx(
                    self.r.router.write,
                    prompt,
                    effort="medium",
                    ctx={"room": self.r.room_id, "role": "reviewer", "purpose": "v2_micro_review"},
                )
            except Exception as e2:
                self.emit("notice", f"GLM-5.3 備援審查失敗：{type(e2).__name__}")
                return None

        parsed = self._parse_micro_review(reply)
        if parsed is None and len(reply.strip()) >= 20:
            # 單次輕量 JSON 整理防呆（過短無效回覆不浪費額外請求）
            try:
                repaired = call_ctx(
                    self.r.router.write,
                    '請把以下審查意見轉成純 JSON：{"blocking": [...], "suggestions": [...]}\n\n' + reply[:5000],
                    effort="low",
                    ctx={"room": self.r.room_id, "role": "reviewer", "purpose": "v2_repair_review_json"},
                )
                parsed = self._parse_micro_review(repaired)
            except Exception:
                pass
        if parsed is not None:
            blk, sug = parsed
            raw_obj = personas.parse_json_block(reply)
            learnings = (
                [str(x).strip() for x in (raw_obj.get("compound_learnings") or []) if str(x).strip()]
                if isinstance(raw_obj, dict)
                else []
            )
            if learnings:
                self.r.evolve_wiki(
                    "solutions",
                    f"# Compound Learnings：[{unit.id}] {unit.title}\n\n" + "\n".join(f"- {l}" for l in learnings[:8]),
                    fingerprint=f"v2:compound:{unit.id}:{hash(tuple(learnings[:4]))}",
                )
            if blk or sug:
                lines = [f"# 審查紀錄：[{unit.id}] {unit.title}"]
                if blk:
                    lines.append("## 阻斷項\n" + "\n".join(f"- {b}" for b in blk[:8]))
                if sug:
                    lines.append("## 建議\n" + "\n".join(f"- {s}" for s in sug[:8]))
                self.r.evolve_wiki(
                    "review_blocking",
                    "\n\n".join(lines),
                    fingerprint=f"v2:review:{unit.id}:{hash(tuple(blk[:4]))}",
                )
        return parsed

    @staticmethod
    def _parse_micro_review(text: str) -> Optional[Tuple[List[str], List[str]]]:
        data = personas.parse_json_block(text)
        if not isinstance(data, dict):
            return None
        raw_b = data.get("blocking")
        if not isinstance(raw_b, list):
            for k in ("blockers", "issues", "findings"):
                if isinstance(data.get(k), list):
                    raw_b = data.get(k)
                    break
            else:
                if data.get("approved") is True or data.get("ok") is True or data.get("pass") is True:
                    raw_b = []
                else:
                    return None
        blocking = personas.normalize_review_blocking(raw_b)
        sugg = [str(s).strip() for s in (data.get("suggestions") or []) if str(s).strip()]
        return blocking, sugg

    # -------- Commit & Artifact --------
    def commit_unit(self, unit: UnitSpec, result: UnitResult) -> str:
        wt = self.r._wt
        if wt is None:
            return ""
        try:
            dirty = gitops.git(wt.path, "status", "--porcelain", ssh_host=wt.ssh_host).strip()
            if dirty:
                gitops.git(wt.path, "add", "-A", ssh_host=wt.ssh_host)
                msg = f"{unit.id}: {unit.title[:72]}"
                gitops.git(wt.path, "commit", "-m", msg, ssh_host=wt.ssh_host)
            return gitops.git(wt.path, "rev-parse", "HEAD", ssh_host=wt.ssh_host).strip()
        except GitError:
            return ""

    def save_unit_artifact(self, result: UnitResult) -> None:
        room = self.r.store.get(self.r.room_id) or self.r.room
        arts = dict(getattr(room, "unit_artifacts", None) or {})
        arts[result.unit_id] = result.model_dump()
        self.r._set(unit_artifacts=arts)
        # 單元結案時自動演化 LLM Wiki，並同步代理在工作區新建立或修改的 Markdown 文件
        summary_lines = [
            f"# 單元結案：[{result.unit_id}] {result.title}",
            f"- 狀態：{result.status}（commit={result.commit_sha[:7] if result.commit_sha else '—'}）",
        ]
        if result.verify:
            summary_lines.append(f"- 驗證：`{result.verify.cmd}` -> exit={result.verify.exit_code}")
        if result.review:
            summary_lines.append(
                f"- 審查：D1 risk={result.review.d1_risk}，deep_reviewed={result.review.deep_reviewed}"
            )
            if result.review.suggestions:
                summary_lines.append("- 建議：" + "；".join(result.review.suggestions[:4]))
        self.r.evolve_wiki(
            "unit_done",
            "\n".join(summary_lines),
            fingerprint=f"v2:unit:{result.unit_id}:{result.commit_sha[:7] if result.commit_sha else result.status}",
            sync_docs=True,
        )

    def mark_finished(self) -> None:
        self.r._set(status="done", phase="done", graph_node="done", current_unit_id="", activity="全部單元已完成")
        self.r.store.add_inbox(
            self.r.room_id,
            "merge",
            "全部單元已完成，是否合併到主分支？",
            f"分支：{self.r.room.branch}\n共完成 {len(self.r.room.criteria or [])} 個單元。",
            [{"id": "merge", "label": "合併"}, {"id": "later", "label": "先不要"}],
        )
        self.r._changed()


class EngineV2Runner(threading.Thread):
    """與 RoomRunner 介面相容的 v2 LangGraph 執行器。"""

    def __init__(
        self,
        room_id: str,
        store: RoomStore,
        router: ModelRouter,
        clis: Dict[str, CliInfo],
        deny_patterns: List[str],
        on_change: Callable[[], None],
        cmd_overrides: Optional[Dict[str, List[str]]] = None,
        dispatcher: Any = None,
        quota: Any = None,
        model_cfg: Optional[ModelConfig] = None,
        plugin_dirs: Sequence[str] = (),
        turn_timeout: float = 1800,
        max_fix_attempts: int = 3,
        on_exit: Optional[Callable[["EngineV2Runner"], None]] = None,
        **extra: Any,
    ) -> None:
        super().__init__(daemon=True, name=f"engine-v2-{room_id}")
        self.room_id = room_id
        self.store = store
        self.router = router
        self.clis = clis
        self.deny_patterns = list(deny_patterns or [])
        self._on_change = on_change
        self._on_exit = on_exit
        self.cmd_overrides = dict(cmd_overrides or {})
        self.dispatcher = dispatcher
        self.quota = quota
        self.model_cfg = model_cfg
        self.plugin_dirs = list(plugin_dirs or [])
        self.turn_timeout = turn_timeout
        self.max_fix_attempts = max(1, int(max_fix_attempts))
        self.usage = extra.get("usage")
        self.kb = extra.get("kb")
        self.tracer = RoomTracer(store._dir(room_id), room_id)

        self.room: Room = store.get(room_id)  # type: ignore[assignment]
        self._wt: Optional[Worktree] = None
        self._pause_ev = threading.Event()
        self._answer_ev = threading.Event()
        self._pending_item_id: str = ""
        self._pending_answer: Tuple[str, str] = ("", "")
        self._active_driver: Any = None
        self._active_verify_proc: Any = None
        self._lock = threading.Lock()
        self._user_msgs: List[str] = []

    def _changed(self) -> None:
        try:
            self._on_change()
        except Exception:
            pass

    def evolve_wiki(self, category: str, text: str, *, fingerprint: str = "", sync_docs: bool = False) -> None:
        """隨代理執行過程產生的文件、驗證失敗、審查意見與單元提交自動演化 LLM Wiki。"""
        if self.kb is None or not self.room or not self.room.repo:
            return
        try:
            recorded = False
            if text and text.strip():
                recorded = bool(
                    self.kb.record(
                        self.room.repo,
                        self.room_id,
                        category,
                        text.strip()[:3000],
                        fingerprint=fingerprint,
                        wait=False,
                    )
                )
            synced = 0
            if sync_docs and not getattr(self.room, "ssh_host", ""):
                target_repo = self._wt.path if (self._wt is not None and os.path.isdir(self._wt.path)) else self.room.repo
                if target_repo and os.path.isdir(target_repo):
                    synced = int(
                        self.kb.sync_repo_compound_docs(
                            target_repo,
                            reference_root=self.room.reference_root,
                            target_subdir=self.room.target_subdir,
                            target_subdirs=list(getattr(self.room, "target_subdirs", None) or ()),
                        )
                        or 0
                    )
            if recorded or synced:
                self.tracer.record("wiki_evolve", category=category, recorded=recorded, docs_synced=synced)
        except Exception as e:
            log.debug("LLM Wiki 自動演化略過：%s", type(e).__name__)

    def query_wiki_context(self, query_text: str) -> str:
        """查詢專案 LLM Wiki 既有知識，注入下一個單元的 Fresh Session。"""
        if self.kb is None or not self.room or not self.room.repo or not query_text.strip():
            return ""
        try:
            res = str(self.kb.query(self.room.repo, query_text[:400], room_id=self.room_id, top_k=4) or "").strip()
            return res[:1500]
        except Exception:
            return ""

    def _set(self, **kw: Any) -> Room:
        def mut(r: Room) -> None:
            for k, v in kw.items():
                if hasattr(r, k):
                    setattr(r, k, v)
        self.room = self.store.update(self.room_id, mut)
        self._changed()
        return self.room

    def _event(self, kind: str, text: str, **extra: Any) -> None:
        self.store.event(self.room_id, kind, text, **extra)
        self._changed()

    def pause(self) -> None:
        self._pause_ev.set()
        self._answer_ev.set()
        self._set(activity="暫停中…")
        with self._lock:
            drv = self._active_driver
            vproc = self._active_verify_proc
        if vproc is not None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(vproc.pid)], capture_output=True, timeout=3)
                else:
                    vproc.kill()
            except Exception:
                pass
        if drv is not None:
            try:
                drv.cancel()
            except Exception:
                pass
            # 若 ACP 代理忽略 session/cancel 仍卡在工具呼叫中，1.2 秒後強制關閉驅動讓執行緒立即收尾
            def _force_close(target: Any) -> None:
                with self._lock:
                    still_same = (self._active_driver is target)
                if still_same:
                    try:
                        target.close()
                    except Exception:
                        pass
            t = threading.Timer(1.2, _force_close, args=(drv,))
            t.daemon = True
            t.start()

    def answer(self, item_id: str, ans: str, text: str = "") -> bool:
        with self._lock:
            if not self._pending_item_id or self._pending_item_id != item_id:
                return False
            self._pending_answer = (ans, text)
            self._pending_item_id = ""
            self._answer_ev.set()
        self.store.resolve(item_id, ans, text)
        self._changed()
        return True

    def send(self, text: str) -> None:
        if text and text.strip():
            with self._lock:
                self._user_msgs.append(text.strip())
            self._event("user", text.strip())

    def drain_user_msgs(self) -> List[str]:
        with self._lock:
            out, self._user_msgs = list(self._user_msgs), []
        return out



    def set_delegate(self, on: bool) -> None:
        self._set(delegate=bool(on))

    def _ensure_worktree(self) -> Worktree:
        r = self.room
        sh = (getattr(r, "ssh_host", "") or "").strip()
        # 既有房間若仍指向尚未遷移的舊 worktree 且存在於磁碟上，先沿用以免遺失未提交工作；否則一律直接在目標資料夾本身開發
        if r.worktree and r.worktree != r.repo and (sh or os.path.isdir(r.worktree)):
            return Worktree(r.repo, r.worktree, r.branch, r.base, ssh_host=sh)
        try:
            cur_branch = gitops.git(r.repo, "branch", "--show-current", ssh_host=sh).strip() or "HEAD"
        except Exception:
            cur_branch = r.branch or "HEAD"
        try:
            cur_head = gitops.git(r.repo, "rev-parse", "HEAD", ssh_host=sh).strip() or "HEAD"
        except Exception:
            cur_head = r.base or "HEAD"
        wt = Worktree(r.repo, r.repo, cur_branch, cur_head, ssh_host=sh)
        self._set(worktree=wt.path, branch=wt.branch, base=wt.base)
        return wt

    def _pick_seat(self, role: str) -> Tuple[str, str]:
        r = self.room
        pref = r.impl_pref if role == "impl" else r.review_pref
        seat = str((pref or {}).get("seat") or (r.impl_cli if role == "impl" else r.review_cli) or "auto")
        model = str((pref or {}).get("model") or "")
        if seat and seat != "auto":
            if seat == "agy" and not model:
                model = "gemini-4-hydrogen"
            return seat, model
        if role == "impl":
            if "agy" in self.clis:
                return "agy", model or "gemini-4-hydrogen"
            if "kiro" in self.clis:
                return "kiro", model or "claude-sonnet-5.5"
        else:
            if "kiro" in self.clis:
                return "kiro", model or "claude-sonnet-5.5"
            if "agy" in self.clis:
                return "agy", model or "gemini-4-hydrogen"
        if self.model_cfg is not None:
            return "glm", model or (getattr(self.model_cfg, "glm_model", "") or "glm-5.3")
        for cand in ("claude",):
            if cand in self.clis:
                return cand, model
        return next(iter(self.clis), "glm"), model

    def _ask_inbox_sync(
        self, kind: str, title: str, detail: str, options: List[Dict[str, str]]
    ) -> Tuple[str, str]:
        """同步建立收件匣項目並等待使用者回覆（用於執行中權限把關與人工介入）。"""
        with self._lock:
            item = self.store.add_inbox(self.room_id, kind, title, detail, options)
            self._pending_item_id = item.id
            self._answer_ev.clear()
        prev_status = self.room.status
        self._set(status="waiting_user")
        self.tracer.record("inbox_wait", inbox_kind=kind, item_id=item.id, title=title[:160])
        self._answer_ev.wait()
        if self._pause_ev.is_set():
            raise _PausedV2()
        ans, text = self._pending_answer
        self._set(status=prev_status if prev_status == "running" else "running")
        self.tracer.record("inbox_resolved", inbox_kind=kind, item_id=item.id, answer=ans)
        return ans, text

    def _record_turn_usage(self, seat: str, model: str, drv: Any, *, ok: bool) -> None:
        tu = None
        if hasattr(drv, "turn_usage") and callable(drv.turn_usage):
            try:
                tu = drv.turn_usage()
            except Exception:
                tu = None
        if not isinstance(tu, dict):
            return
        rec = dict(tu, ok=ok)
        cur_tokens = dict(self.room.tokens or {})
        cur_tokens[seat] = merge_tokens(cur_tokens.get(seat), rec)
        updates: Dict[str, Any] = {"tokens": cur_tokens}
        if tu.get("model"):
            if seat == self.room.impl_seat:
                updates["impl_model_actual"] = str(tu["model"])
            elif seat == self.room.review_seat:
                updates["review_model_actual"] = str(tu["model"])
        self._set(**updates)
        if self.usage is not None and hasattr(self.usage, "record_cli"):
            try:
                self.usage.record_cli(
                    self.room_id,
                    seat,
                    str(tu.get("model") or model or ""),
                    tu.get("in"),
                    tu.get("out"),
                    tu.get("in_cache"),
                    tu.get("cost"),
                    str(tu.get("currency") or ""),
                    ok=ok,
                )
            except Exception:
                pass

    @staticmethod
    def _is_safe_read_only_cmd(cmd: str) -> bool:
        """判斷唯讀座位（Planner / Reviewer）發出的指令是否為純查詢／載入技能，嚴格排除任何寫入或狀態變更。"""
        s = (cmd or "").strip()
        if not s:
            return False
        low = s.lower()
        if low.startswith("load skill:"):
            return True
        # 嚴格排除任何重導向寫檔、檔案修改、套件安裝或 Git 寫入
        write_markers = (
            ">", ">>", "out-file", "set-content", "add-content", "remove-item",
            "move-item", "copy-item", "new-item", "clear-content", "tee-object",
            "rm ", "mv ", "cp ", "mkdir ", "rmdir ", "touch ", "chmod ", "chown ",
            "sed -i", "tee ", "git commit", "git push", "git checkout", "git reset",
            "git clean", "git apply", "git merge", "git rebase", "invoke-expression", "iex ",
        )
        if any(m in low for m in write_markers):
            return False
        # 允許純查詢指令（支援以 ; 或 && 串接的多個唯讀命令）
        allowed_prefixes = (
            "get-childitem", "get-content", "select-string", "test-path", "resolve-path",
            "ls", "dir", "cat", "head", "tail", "wc", "stat", "file", "find",
            "rg", "grep", "tree", "pwd", "which",
            "git status", "git diff", "git log", "git show", "git branch", "git rev-parse", "git ls-files",
        )
        segments = [seg.strip() for seg in re.split(r"(?:&&|;|\n)", low) if seg.strip()]
        if not segments:
            return False
        for seg in segments:
            # 管線後段允許常見純過濾／格式化（Where-Object / Select-Object / ForEach-Object / head / sort / uniq / wc / grep / rg）
            pipe_parts = [p.strip() for p in seg.split("|") if p.strip()]
            first = pipe_parts[0]
            if not any(first == ap or first.startswith(ap + " ") for ap in allowed_prefixes):
                return False
        return True

    def _evaluate_permission(self, req: PermissionRequest, *, read_only: bool) -> str:
        """權限把關：危險規則一律拒絕 → 唯讀模式放行讀/搜尋/安全唯讀查詢 → 灰色地帶先問 Decision-1，不確定送收件匣。"""
        cmd = command_text(req)
        hit = _match_deny(cmd, self.deny_patterns)
        if hit:
            self.tracer.record("permission", cmd=cmd[:200], req_kind=req.kind, decision="deny", reason=f"deny_pattern:{hit}")
            return "deny"
        if req.kind in ("read", "search"):
            return "allow"
        if read_only:
            if self._is_safe_read_only_cmd(cmd):
                self.tracer.record("permission", cmd=cmd[:200], req_kind=req.kind, decision="allow", reason="read_only_safe_cmd")
                return "allow"
            self.tracer.record("permission", cmd=cmd[:200], req_kind=req.kind, decision="deny", reason="read_only_seat")
            return "deny"

        # 寫入／執行類權限：透過 ModelRouter.allow（Decision-1 + GLM）判斷灰色地帶
        if self.router is not None and hasattr(self.router, "allow"):
            try:
                verdict = self.router.allow(
                    cmd,
                    goal=self.room.goal or "",
                    scope=self.room.target_subdir or "",
                    autonomy=self.room.autonomy or "balanced",
                    ctx={"room": self.room_id, "role": "supervisor", "purpose": "v2_permission"},
                )
                if getattr(verdict, "allow", False):
                    self.tracer.record("permission", cmd=cmd[:200], req_kind=req.kind, decision="allow", reason="d1_allow")
                    return "allow"
                if getattr(verdict, "ask", False):
                    ans, _ = self._ask_inbox_sync(
                        "permission",
                        f"允許代理執行：{cmd[:120]}",
                        f"類型：{req.kind}\n指令：{cmd}\n理由：{getattr(verdict, 'reason', '')}",
                        [{"id": "allow", "label": "允許一次"}, {"id": "reject", "label": "拒絕"}],
                    )
                    dec = "allow" if ans == "allow" else "deny"
                    self.tracer.record("permission", cmd=cmd[:200], req_kind=req.kind, decision=dec, reason="user_inbox")
                    return dec
                self.tracer.record("permission", cmd=cmd[:200], req_kind=req.kind, decision="deny", reason=str(getattr(verdict, "reason", "d1_deny")))
                return "deny"
            except _PausedV2:
                raise
            except Exception as e:
                self.tracer.record("permission", cmd=cmd[:200], req_kind=req.kind, decision="allow", reason=f"router_err:{type(e).__name__}")
                return "allow"
        return "allow"

    def _run_fresh_cli_turn(
        self,
        seat: str,
        model: str,
        prompt: str,
        *,
        read_only: bool,
        role: str,
        timeout_override: Optional[float] = None,
    ) -> str:
        """建立全新 CLI Session 執行單一回合並於結束後關閉；額度用滿自動切換 GLM 備援並累計 token。"""
        wt = self._wt
        cwd = wt.path if wt is not None else self.room.repo
        sh = (getattr(self.room, "ssh_host", "") or "").strip()
        tmpdir = os.path.join(self.store._dir(self.room_id), "tmp_v2")
        os.makedirs(tmpdir, exist_ok=True)

        if seat == "agy" and not model:
            model = "gemini-4-hydrogen"
        candidates: List[Tuple[str, str]] = [(seat, model)]
        if role == "developer" and seat == "agy" and "kiro" in self.clis:
            for km in ("claude-sonnet-5.5", "claude-opus-5.5"):
                if ("kiro", km) not in candidates:
                    candidates.append(("kiro", km))
        if seat not in GLM_SEATS and self.model_cfg is not None:
            candidates.append(("glm", ""))

        last_err: Optional[Exception] = None
        for idx, (cur_seat, cur_model) in enumerate(candidates):
            if self._pause_ev.is_set():
                raise _PausedV2()
            t0 = time.monotonic()
            cli, env, extra = seat_launch(
                cur_seat,
                self.clis,
                self.model_cfg,
                self.plugin_dirs,
                cwd,
                tmpdir,
                reference_root=self.room.reference_root,
                ssh_host=sh,
            )

            msg_chunks: List[str] = []

            def on_ev(ev: AgentEvent) -> None:
                if ev.kind == "message" and ev.text:
                    msg_chunks.append(ev.text)
                elif ev.kind == "tool" and ev.text:
                    self._set(activity=ev.text[:160])
                    self.tracer.record("cli_tool", role=role, seat=cur_seat, tool=ev.text[:200])
                elif ev.kind == "notice" and ev.text:
                    self.tracer.record("cli_notice", role=role, seat=cur_seat, text=ev.text[:240])

            def on_perm(req: PermissionRequest) -> str:
                return self._evaluate_permission(req, read_only=read_only)

            drv = make_driver(
                cli,
                cwd,
                on_ev,
                on_perm,
                read_only=read_only,
                env=env,
                extra_args=extra,
                model=cur_model,
                ssh_host=sh,
            )
            with self._lock:
                self._active_driver = drv
            ok = False
            try:
                self._event("prompt", prompt[:300], role=role, who=cur_seat, fresh_context=True)
                drv.start()
                effective_timeout = float(timeout_override) if timeout_override is not None else float(self.turn_timeout)
                raw_stop = str(drv.prompt(prompt, timeout=effective_timeout) or "").strip()
                if self._pause_ev.is_set() or raw_stop == "cancelled":
                    if self._pause_ev.is_set():
                        raise _PausedV2()
                msg_txt = "".join(msg_chunks).strip()
                if msg_txt:
                    reply = msg_txt
                elif raw_stop not in ("end_turn", "max_tokens", "stop_sequence", "cancelled", "refusal"):
                    reply = raw_stop
                else:
                    reply = ""
                if reply and len(reply) < 600 and _is_quota_error(reply):
                    raise RuntimeError(f"CLI 回覆額度耗盡：{reply[:200]}")
                ok = True
                elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
                self._record_turn_usage(cur_seat, cur_model, drv, ok=True)
                self.tracer.record(
                    "cli_turn",
                    role=role,
                    seat=cur_seat,
                    model=cur_model,
                    read_only=read_only,
                    prompt_chars=len(prompt),
                    reply_chars=len(reply),
                    reply_preview=reply[:300],
                    elapsed_ms=elapsed_ms,
                    ok=True,
                )
                if _requests_fresh_session(reply):
                    self._event("notice", f"座位 {cur_seat} 回報 context 限制，v2 已於下一回合自動開啟新對話")
                return reply
            except _PausedV2:
                raise
            except Exception as e:
                if self._pause_ev.is_set():
                    raise _PausedV2()
                last_err = e
                elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
                self._record_turn_usage(cur_seat, cur_model, drv, ok=False)
                err_txt = str(e)
                quota_hit = _is_quota_error(err_txt)
                self.tracer.record(
                    "cli_turn",
                    role=role,
                    seat=cur_seat,
                    model=cur_model,
                    read_only=read_only,
                    elapsed_ms=elapsed_ms,
                    ok=False,
                    quota=quota_hit,
                    error=err_txt[:300],
                )
                if idx + 1 < len(candidates):
                    nxt_seat, nxt_model = candidates[idx + 1]
                    if quota_hit and self.quota is not None and hasattr(self.quota, "mark_exhausted"):
                        try:
                            from datetime import datetime
                            self.quota.mark_exhausted(cur_seat, err_txt, datetime.now())
                        except Exception:
                            pass
                    reason_lbl = "用量已滿" if quota_hit else f"執行失敗（{type(e).__name__}）"
                    nxt_lbl = f"{nxt_seat}/{nxt_model}" if nxt_model else "免費 GLM-5.3"
                    self._event("quota" if quota_hit else "notice", f"{cur_seat} {reason_lbl}，自動降級改由 {nxt_lbl} 接手本回合", seat=cur_seat)
                    self._set(fallback_from=cur_seat)
                    continue
                raise
            finally:
                with self._lock:
                    self._active_driver = None
                try:
                    drv.close()
                except Exception:
                    pass
        if last_err is not None:
            raise last_err
        return ""

    def run(self) -> None:
        db_path = os.path.join(self.store._dir(self.room_id), "graph.sqlite")
        saver = None
        conn = None
        try:
            self._set(status="running", error="")
            try:
                ce_status = ce_skills.ensure_ce_installed(self.plugin_dirs)
                self._event("notice", "已確認 compound-engineering 技能就緒（v2 全階段已掛載）", ce=ce_status)
                self.tracer.record("ce_skills", ready=True)
            except Exception:
                log.debug("檢查 compound-engineering 技能失敗", exc_info=True)
            self._wt = self._ensure_worktree()
            if getattr(self.room, "checkpoint_stale", False):
                for fn in ("graph.sqlite", "graph.sqlite-wal", "graph.sqlite-shm"):
                    p = os.path.join(self.store._dir(self.room_id), fn)
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                self._set(checkpoint_stale=False)
            saver, conn = open_sqlite_checkpointer(db_path)
            rt = _V2Runtime(self)
            graph = build_room_workflow(rt, checkpointer=saver)
            config = {"configurable": {"thread_id": self.room_id}}

            # 檢查是否有未完成的 SQLite checkpoint
            snap = graph.get_state(config)
            if snap and snap.next:
                stream_input: Any = None
            else:
                stream_input = RoomGraphState(
                    room_id=self.room_id,
                    goal=self.room.goal,
                    task_mode=getattr(self.room, "task_mode", "standard") or "standard",
                    d1_fast_review=bool(getattr(self.room, "d1_fast_review", True)),
                    autonomy=self.room.autonomy or "balanced",
                    global_test_cmd=self.room.test_cmd or "",
                    max_unit_attempts=self.max_fix_attempts,
                    units=[],
                    current_idx=0,
                    current_unit_id="",
                    plan_ready=False,
                    plan_refinements=0,
                    replan_reason="",
                    replan_count=0,
                    max_replans=2,
                    completed_ledger=[],
                    audit_round=0,
                    max_audit_rounds=2,
                    artifacts=dict(getattr(self.room, "unit_artifacts", None) or {}),
                    completed=False,
                    paused=False,
                )

            while not self._pause_ev.is_set():
                result = graph.invoke(stream_input, config=config)
                if self._pause_ev.is_set():
                    raise _PausedV2()

                # 檢查是否停在 LangGraph interrupt() 人工閘門
                interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
                if interrupts:
                    payload = interrupts[0].value if hasattr(interrupts[0], "value") else dict(interrupts[0])
                    with self._lock:
                        item = self.store.add_inbox(
                            self.room_id,
                            str(payload.get("kind") or "question"),
                            str(payload.get("title") or "需要你的決定"),
                            str(payload.get("detail") or ""),
                            list(payload.get("options") or []),
                        )
                        self._pending_item_id = item.id
                        self._answer_ev.clear()
                    self._set(status="waiting_user")
                    self.tracer.record("interrupt", item_id=item.id, title=str(payload.get("title") or "")[:160])
                    self._answer_ev.wait()
                    if self._pause_ev.is_set():
                        raise _PausedV2()
                    ans, text = self._pending_answer
                    self.tracer.record("resume", item_id=item.id, answer=ans)
                    self._set(status="running")
                    stream_input = Command(resume={"answer": ans, "text": text})
                    continue

                if isinstance(result, dict) and result.get("paused"):
                    self._set(status="paused")
                break
        except _PausedV2:
            self._set(status="paused", activity="已暫停")
        except Exception as e:
            log.exception("EngineV2Runner 發生例外：%s", self.room_id)
            self._set(status="error", error=f"{type(e).__name__}: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if self._on_exit is not None:
                try:
                    self._on_exit(self)
                except Exception:
                    pass
