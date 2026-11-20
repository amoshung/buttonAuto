"""Compound-Engineering (CE) 開工前檢查、跨 CLI 自動同步掛載與全角色（含 GLM-5.3）專屬技能規範。"""
import os
import shutil
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

_CE_LOCK = threading.Lock()

REQUIRED_CE_SKILLS: Tuple[str, ...] = (
    "ce-brainstorm",
    "ce-strategy",
    "ce-plan",
    "ce-doc-review",
    "ce-work",
    "ce-debug",
    "ce-simplify-code",
    "ce-commit",
    "ce-test-browser",
    "ce-code-review",
    "ce-compound",
)

_SUPERVISOR_CE: Dict[str, Any] = {
    "skills": ("ce-brainstorm", "ce-strategy", "ce-plan", "ce-doc-review"),
    "title": "高層主管：規格計畫定錨、權限把關與修正方向裁決（ce-brainstorm + ce-strategy + ce-plan + ce-doc-review）",
    "playbook": (
        "1. 依 ce-brainstorm 與 ce-strategy 鎖定開發目標、成功標準與不做的事（Non-goals），將可修改範圍（allowed_paths）收斂到最小必要集合。\n"
        "2. 依 ce-plan 與 ce-doc-review 直接對齊既有 docs/plans/*.md 或單次產出結構化實作單元與驗證指令，避免多輪空轉浪費 Token。\n"
        "3. 雙模型協作：由 Decision-1 進行機率與多選一裁決，搭配使用者指定之協作生成模型完成拆解、背景補充與修正方向拍板。\n"
        "4. 若存在唯讀參考母目錄，僅可將其列為閱讀參考，嚴禁將母目錄或未勾選兄弟子資料夾列入 allowed_paths。\n"
        "5. 若需拆分或衍生後續子計畫（docs/plans/*.md），必須在檔頭標註 `status: active` 與「衍生自（Parent Plan）：<父計畫路徑>」、使用 `- [ ]` 條列任務並附 `## 驗證` 指令；同時嚴禁將已完成／已作廢的歷史舊計畫寫入未完成單元，也嚴禁把 Goal/Requirements/Verification 等說明欄位拆成獨立任務。"
    ),
}

_DEVELOPER_CE: Dict[str, Any] = {
    "skills": ("ce-work", "ce-debug", "ce-simplify-code", "ce-test-browser", "ce-commit"),
    "title": "研發工程師：全流程 TDD 實作、根因除錯、精簡與提交（ce-work + ce-debug + ce-simplify-code + ce-commit）",
    "playbook": (
        "1. 依 ce-work 流程：閱讀計畫與既有慣例 → 撰寫涵蓋快樂路徑與邊界情境的測試 → 實作最小完整解法 → 親自執行驗證指令確認通過。\n"
        "2. 若遇測試失敗或審查工程師指出偏離位置，依 ce-debug 先定位根因（檔案:行號）並自主決定最佳修復實作，嚴禁重複先前失敗做法或偽造假測試。\n"
        "3. 測試通過後，依 ce-simplify-code 消除重複邏輯與死碼，再依 ce-commit 進行結構化 Git Commit（保持工作目錄乾淨），並如實回報交接摘要與顧慮（concerns）。\n"
        "4. 若涉及前端或 UI 變更，結合 ce-test-browser 驗證畫面與互動行為；嚴格遵守工作區寫入邊界。\n"
        "5. 若本回合產出新的後續／衍伸子計畫（docs/plans/*.md），務必於檔頭標註「衍生自（Parent Plan）：<父計畫路徑>」並以 `- [ ]` 條列待辦與驗證指令，系統會自動將其展開並列入房間計畫清單交由後續 Agent 依序完成。"
    ),
}

_REVIEWER_CE: Dict[str, Any] = {
    "skills": ("ce-code-review", "ce-compound"),
    "title": "審查工程師：盤中增量防偏離審查、唯讀深度審查與知識沉澱（ce-code-review + ce-compound）",
    "playbook": (
        "1. 依 ce-code-review 進行盤中增量審查與最終唯讀深度審查，並結合 Decision-1 檢驗開發過程是否緊密對齊原始目標，嚴查越界修改、空殼實作、安全漏洞與假測試。\n"
        "2. 職責分際：一旦發現偏離目標或阻擋合併（blocking）的問題，請精確指出「是哪裡偏離或出錯（檔案:行號、對應條件與原因）」，把「怎麼改」的策略與實作交由高層主管與研發工程師決定。\n"
        "3. 依 ce-compound 原則，在 JSON 回覆的 `compound_learnings` 欄位提煉本次關鍵架構決策或防坑教訓，自動沉澱至專案 LLM Wiki。"
    ),
}

ROLE_CE_MAP: Dict[str, Dict[str, Any]] = {
    "supervisor": _SUPERVISOR_CE,
    "developer": _DEVELOPER_CE,
    "reviewer": _REVIEWER_CE,
    # 向下相容舊鍵映射至三大核心角色
    "po": _SUPERVISOR_CE,
    "tech_lead": _SUPERVISOR_CE,
    "scrum": _SUPERVISOR_CE,
    "qa": _SUPERVISOR_CE,
    "implementer": _DEVELOPER_CE,
    "fixer": _DEVELOPER_CE,
    "impl": _DEVELOPER_CE,
    "review": _REVIEWER_CE,
}


def default_canonical_skills_dir() -> str:
    """回傳本機預設的 ~/.gemini/config/skills 目錄。"""
    home = os.path.expanduser("~")
    return os.path.join(home, ".gemini", "config", "skills")


def _candidate_source_dirs(plugin_dirs: Sequence[str] = ()) -> List[str]:
    out: List[str] = []
    for d in plugin_dirs or ():
        if d and os.path.isdir(d):
            out.append(os.path.abspath(d))
            skills_sub = os.path.join(d, "skills")
            if os.path.isdir(skills_sub):
                out.append(os.path.abspath(skills_sub))
    gem = default_canonical_skills_dir()
    if os.path.isdir(gem):
        out.append(os.path.abspath(gem))
    return out


def _find_skill_src(skill_name: str, sources: Sequence[str]) -> str:
    for src in sources:
        cand = os.path.join(src, skill_name)
        if os.path.isfile(os.path.join(cand, "SKILL.md")):
            return cand
    return ""


def _write_fallback_skill(dst_skill_dir: str, skill_name: str) -> None:
    """若離線環境連本機母源都缺少某個 ce-* 技能，自動建立包含標準規範的本地 SKILL.md，確保房間永不中斷。"""
    os.makedirs(dst_skill_dir, exist_ok=True)
    skill_md = os.path.join(dst_skill_dir, "SKILL.md")
    if os.path.isfile(skill_md):
        return
    role_lines = []
    for role, info in ROLE_CE_MAP.items():
        if skill_name in info["skills"]:
            role_lines.append(f"- **{role}**: {info['title']}\n{info['playbook']}")
    body = "\n\n".join(role_lines) or f"Execute {skill_name} workflow with strict verification and scope discipline."
    content = (
        f"---\n"
        f"name: {skill_name}\n"
        f"description: Compound-Engineering {skill_name} skill provisioned by AI Office.\n"
        f"---\n\n"
        f"# {skill_name}\n\n"
        f"{body}\n"
    )
    with open(skill_md, "w", encoding="utf-8") as f:
        f.write(content)


def ensure_ce_installed(
    plugin_dirs: Sequence[str] = (),
    target_root: Optional[str] = None,
) -> Dict[str, Any]:
    """開工前確認所有 REQUIRED_CE_SKILLS 已安裝並同步至各 CLI 可讀取的技能／外掛目錄。

    - target_root：可指定測試或房間自用的 CE 外掛根目錄；未指定時預設同步至
      `~/.gemini/config/skills`（AGY 與母源）、`~/.kiro/skills`（Kiro）與
      `~/.gemini/config/ce-plugin`（供 Claude / GLM `--plugin-dir` 掛載）。
    """
    with _CE_LOCK:
        return _ensure_ce_installed_unlocked(plugin_dirs=plugin_dirs, target_root=target_root)


def _ensure_ce_installed_unlocked(
    plugin_dirs: Sequence[str] = (),
    target_root: Optional[str] = None,
) -> Dict[str, Any]:
    sources = _candidate_source_dirs(plugin_dirs)
    home = os.path.expanduser("~")
    if target_root:
        agy_skills_dir = os.path.join(target_root, "agy_skills")
        kiro_skills_dir = os.path.join(target_root, "kiro_skills")
        claude_plugin_dir = os.path.join(target_root, "claude_plugin")
    else:
        agy_skills_dir = default_canonical_skills_dir()
        kiro_skills_dir = os.path.join(home, ".kiro", "skills")
        claude_plugin_dir = os.path.join(home, ".gemini", "config", "ce-plugin")

    claude_skills_dir = os.path.join(claude_plugin_dir, "skills")
    claude_meta_dir = os.path.join(claude_plugin_dir, ".claude-plugin")
    for d in (agy_skills_dir, kiro_skills_dir, claude_skills_dir, claude_meta_dir):
        os.makedirs(d, exist_ok=True)
    plugin_manifest = os.path.join(claude_meta_dir, "plugin.json")
    if not os.path.isfile(plugin_manifest):
        with open(plugin_manifest, "w", encoding="utf-8") as mf:
            mf.write(
                '{\n  "name": "ce-plugin",\n  "version": "1.0.0",\n'
                '  "description": "Compound-Engineering skills provisioned by buttonAuto"\n}\n'
            )

    synced: List[str] = []
    provisioned_fallback: List[str] = []

    for skill in REQUIRED_CE_SKILLS:
        src = _find_skill_src(skill, sources)
        for dest_parent in (agy_skills_dir, kiro_skills_dir, claude_skills_dir):
            dest = os.path.join(dest_parent, skill)
            dest_md = os.path.join(dest, "SKILL.md")
            if os.path.isfile(dest_md):
                continue
            if src and os.path.abspath(src) != os.path.abspath(dest):
                shutil.copytree(src, dest, dirs_exist_ok=True)
                if skill not in synced:
                    synced.append(skill)
            else:
                _write_fallback_skill(dest, skill)
                if skill not in provisioned_fallback:
                    provisioned_fallback.append(skill)
        # 一旦 agy_skills_dir 補齊，也可作為後續 dest_parent 的來源
        if not src and os.path.isfile(os.path.join(agy_skills_dir, skill, "SKILL.md")):
            sources = [agy_skills_dir] + list(sources)

    return {
        "ok": True,
        "skills": list(REQUIRED_CE_SKILLS),
        "synced": synced,
        "provisioned_fallback": provisioned_fallback,
        "claude_plugin_dir": claude_plugin_dir,
        "agy_skills_dir": agy_skills_dir,
        "kiro_skills_dir": kiro_skills_dir,
    }


def role_ce_directive(
    role: str,
    notes_dir: str = "",
    reference_root: str = "",
    target_subdir: str = "",
    target_subdirs: Optional[Sequence[str]] = None,
) -> str:
    """回傳指定角色的 Compound-Engineering 能力與邊界強制規範（注入至所有角色提示詞）。"""
    info = ROLE_CE_MAP.get(role) or ROLE_CE_MAP["developer"]
    skills_str = "、".join(f"`{s}`" for s in info["skills"])
    nd = notes_dir or "{notes_dir}"
    lines = [
        f"【Compound-Engineering 角色專屬技能規範（{info['title']}）】",
        f"- 本角色綁定之 CE 核心技能：{skills_str}（無論採用 Claude、Kiro、Antigravity 或 GLM-5.3／LiteLLM 模型皆須嚴格依此規範執行）。",
        info["playbook"],
        f"- 產出路徑與合併邊界：所有規劃、診斷、斷點續傳 `progress.md` 與知識沉澱文件一律寫在房間專屬目錄 `{nd}`，嚴禁自行執行 `git push`／發起 PR；最終合併由使用者於房間審查通過後決定。",
        "- 三角色提問與糾偏契約：執行途中若遇「選項類問題（2 個以上候選方案）」或目標對齊疑慮，交由高層主管的 `Decision-1` 裁決；若遇「描述類問題（開放式規格或背景詢問）」，交由高層主管的協作生成模型結合目標與唯讀參考資料解答。",
    ]
    subs: List[str] = []
    if target_subdirs:
        subs.extend(str(s).strip() for s in target_subdirs if str(s).strip())
    elif target_subdir:
        subs.extend(s.strip() for s in target_subdir.replace(";", ",").split(",") if s.strip())
    if reference_root:
        if len(subs) > 1:
            targets_fmt = "、".join(f"`{s}/`" for s in subs)
            lines.append(
                f"- 多目標資料夾讀寫隔離（A 唯讀參考 vs. 複數目標 B 協同可改）：本房間同時指定多個可讀寫目標資料夾 B：{targets_fmt}（例如前後端雙專案協同開發）。你可以在這些目標資料夾內自由新增、修改與測試；母資料夾 `{reference_root}` 底下其餘未勾選的子資料夾均為「唯讀參考資料來源」，嚴禁修改。"
            )
        else:
            target_label = subs[0] if subs else (target_subdir or "目標子資料夾 B")
            lines.append(
                f"- 資料夾讀寫隔離（A 唯讀參考 vs. B 唯一可改）：母資料夾 `{reference_root}` 及其下除 `{target_label}` 以外的所有子資料夾均為「唯讀參考資料來源」，僅允許讀取檢索；任何新增、修改、刪除與 Git 操作一律只能在 `{target_label}` 的工作區內進行。"
            )
    return "\n".join(lines)
