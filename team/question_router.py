"""問題分流中樞（Question Router）：
- 選項類問題（Multiple-Choice，含 2 個以上候選方案） → 交由 Decision-1（ModelRouter.choose）選出最不偏離開發目標的方案。
- 描述類問題（Descriptive / Open-Ended / NEEDS_CONTEXT） → 交由 GLM-5.3（ModelRouter.write）結合開發目標、規格計畫與母資料夾 A 的唯讀參考資料解答。
- 具備連續追問上限（預設 3 次）與破壞性操作攔截，超過上限才轉交人類收件匣。
"""
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from team.models import ModelRouter, call_ctx
from team.personas import Fence

MAX_QUESTION_STREAK = 3

# 系統在 NEEDS_CONTEXT 時產生的通用按鈕（實質內容需看 detail 內是否為選項題或開放式描述題）
_GENERIC_OPTION_SETS = (
    {"answer", "pause"},
)

_DESTRUCTIVE_KEYWORDS = re.compile(
    r"(?:刪除整個|格式化|drop\s+database|rm\s+-rf\s+/|reset\s+--hard\s+origin|force\s+push|修改唯讀參考)",
    re.I,
)

# 偵測代理文字中內嵌的選項清單：例如 A) ... B) ... / 1. ... 2. ... / 方案一：... 方案二：...
_INLINE_OPT_PATTERNS = (
    re.compile(r"(?:^|\n)\s*[-*•]?\s*(?:選項\s*)?([A-D])[.、:：)\]]\s*(\S[^\n]{1,200})"),
    re.compile(r"(?:^|\n)\s*[-*•]?\s*\(([A-D])\)\s*(\S[^\n]{1,200})"),
    re.compile(r"(?:^|\n)\s*(方案\s*[一二三四1-4A-D])[.、:：)\]]\s*(\S[^\n]{1,200})"),
)

_REF_PATH_RE = re.compile(r"([A-Za-z0-9_.\-/\\]+\.[A-Za-z0-9]{1,8})")


def is_destructive_question(title: str, detail: str) -> bool:
    text = f"{title or ''}\n{detail or ''}"
    return bool(_DESTRUCTIVE_KEYWORDS.search(text))


def _extract_inline_options(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    for pat in _INLINE_OPT_PATTERNS:
        matches = pat.findall(text)
        if len(matches) >= 2:
            seen = set()
            opts: List[Dict[str, str]] = []
            for key, label in matches:
                oid = key.strip()
                if oid in seen:
                    continue
                seen.add(oid)
                opts.append({"id": oid, "label": f"{oid}: {label.strip()}"})
            if len(opts) >= 2:
                return opts
    return []


extract_inline_options = _extract_inline_options


def classify_question(
    title: str,
    detail: str,
    options: Optional[Sequence[Dict[str, str]]] = None,
) -> Tuple[str, List[Dict[str, str]]]:
    """判斷問題屬於 'multiple_choice'（選項類）或 'descriptive'（描述類），並回傳正規化後的選項清單。"""
    valid_opts = [
        {"id": str(o.get("id")).strip(), "label": str(o.get("label") or o.get("id")).strip()}
        for o in (options or [])
        if isinstance(o, dict) and str(o.get("id") or "").strip()
    ]
    opt_ids = {o["id"] for o in valid_opts}

    # 若傳入的只是 NEEDS_CONTEXT 預設按鈕 {"answer", "pause"}，先檢查內文是否含有選項清單
    if opt_ids in _GENERIC_OPTION_SETS or len(valid_opts) < 2:
        inline = _extract_inline_options(f"{title or ''}\n{detail or ''}")
        if len(inline) >= 2:
            return "multiple_choice", inline
        return "descriptive", []

    return "multiple_choice", valid_opts


def _gather_reference_snippets(
    question_text: str,
    reference_root: str = "",
    worktree: str = "",
    max_chars: int = 3000,
) -> str:
    """從問題文字提及的相對檔名中，讀取母資料夾 A（唯讀參考區）或工作區 B 的檔案片段供 GLM-5.3 參考。"""
    roots = [r for r in (reference_root, worktree) if r and os.path.isdir(r)]
    if not roots:
        return ""
    candidates = []
    for m in _REF_PATH_RE.finditer(question_text or ""):
        rel = m.group(1).strip().strip("'\"`")
        if rel and rel not in candidates:
            candidates.append(rel)

    chunks: List[str] = []
    used = 0
    # 1. 先跨所有 roots 直接匹配問題提到的相對路徑
    for root in roots:
        root_real = os.path.normcase(os.path.realpath(root))
        for rel in candidates[:5]:
            full = os.path.join(root, rel.replace("/", os.sep))
            try:
                full_real = os.path.normcase(os.path.realpath(full))
                if os.path.commonpath([root_real, full_real]) != root_real:
                    continue
                if os.path.isfile(full):
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        snippet = f.read(1200)
                    piece = f"[{os.path.relpath(full, root)}]\n{snippet}"
                    if used + len(piece) <= max_chars:
                        chunks.append(piece)
                        used += len(piece)
            except (OSError, ValueError):
                continue

    # 2. 若未直接命中完整路徑，才在 roots 搜尋同名檔案（最多取 2 個）
    if not chunks and candidates:
        target_basenames = {os.path.basename(c).lower() for c in candidates[:5]}
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".venv", "venv")]
                for fn in filenames:
                    if fn.lower() in target_basenames:
                        full = os.path.join(dirpath, fn)
                        try:
                            with open(full, "r", encoding="utf-8", errors="replace") as f:
                                snippet = f.read(1000)
                            piece = f"[{os.path.relpath(full, root)}]\n{snippet}"
                            chunks.append(piece)
                            used += len(piece)
                        except OSError:
                            pass
                        if len(chunks) >= 2 or used >= max_chars:
                            break
                if len(chunks) >= 2 or used >= max_chars:
                    break
            if len(chunks) >= 2 or used >= max_chars:
                break
    return "\n\n".join(chunks)


def route_question(
    router: ModelRouter,
    *,
    goal: str,
    title: str,
    detail: str,
    options: Optional[Sequence[Dict[str, str]]] = None,
    spec_text: str = "",
    plan_text: str = "",
    reference_root: str = "",
    worktree: str = "",
    streak: int = 0,
    max_streak: int = MAX_QUESTION_STREAK,
    ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """將代理或收件匣提出的問題自動分流：
    - 選項類問題（multiple_choice） → Decision-1（router.choose）挑選最不偏離開發目標的選項。
    - 描述類問題（descriptive） → GLM-5.3（router.write）結合開發目標、規格與 A 唯讀參考資料作答。
    - 若連續追問超過 max_streak 或涉及破壞性操作 → 回傳 handoff=True 轉交人類收件匣。
    """
    if streak >= max_streak:
        return {
            "handoff": True,
            "kind": "escalated",
            "by": "human",
            "option_id": "",
            "answer_text": "",
            "probs": {},
            "reason": f"同一階段連續提問已達上限（{max_streak} 次），轉交使用者決定",
        }
    if is_destructive_question(title, detail):
        return {
            "handoff": True,
            "kind": "escalated",
            "by": "human",
            "option_id": "",
            "answer_text": "",
            "probs": {},
            "reason": "問題涉及高風險或破壞性操作，轉交使用者決定",
        }

    kind, resolved_opts = classify_question(title, detail, options)
    ref_snippets = _gather_reference_snippets(f"{title}\n{detail}", reference_root=reference_root, worktree=worktree)

    if kind == "multiple_choice":
        # 排除選了「暫停交給人類」的選項再送給 Decision-1，除非只有 pause
        non_pause = [o for o in resolved_opts if o["id"] != "pause"]
        target_opts = non_pause if len(non_pause) >= 2 else resolved_opts
        labels = {o["id"]: o["label"] for o in target_opts}
        state_parts = [
            f"開發目標：\n{goal}",
            f"規格與驗收條件：\n{spec_text[:2000]}" if spec_text else "",
            f"實作計畫：\n{plan_text[:1500]}" if plan_text else "",
            f"唯讀參考資料片段：\n{ref_snippets}" if ref_snippets else "",
            f"問題標題：{title}",
            f"問題詳情：\n{(detail or '')[:3000]}",
            "候選方案：" + "、".join(f"{v}（{k}）" for k, v in labels.items()),
        ]
        state_text = "\n\n".join(p for p in state_parts if p)
        q_prompt = f"{title}（請選擇最符合開發目標、最不會偏離規格與範圍的方案）"
        c_ctx = {**(ctx or {}), "purpose": "Decision-1 選項題決策（最不偏離開發目標）"}
        pick, probs = call_ctx(router.choose, state_text, q_prompt, target_opts, ctx=c_ctx)
        if not pick or pick not in labels:
            return {
                "handoff": True,
                "kind": "multiple_choice",
                "by": "Decision-1",
                "option_id": "",
                "answer_text": "",
                "probs": probs or {},
                "reason": "Decision-1 無法判斷最佳選項，轉交使用者決定",
            }
        if pick == "pause":
            return {
                "handoff": True,
                "kind": "multiple_choice",
                "by": "Decision-1",
                "option_id": "pause",
                "answer_text": "",
                "probs": probs or {},
                "reason": f"Decision-1 建議交由使用者處理（選了「{labels[pick]}」）",
            }
        chosen_label = labels[pick]
        # 若原問題是內嵌於 NEEDS_CONTEXT 的文字選項，回傳 option_id="answer" 並將選定的方案文字放入 answer_text
        orig_ids = {str(o.get("id")) for o in (options or []) if isinstance(o, dict)}
        out_opt_id = pick if (not orig_ids or pick in orig_ids) else ("answer" if "answer" in orig_ids else pick)
        nums = "、".join(f"{labels.get(k, k)} {v:.2f}" for k, v in (probs or {}).items())
        return {
            "handoff": False,
            "kind": "multiple_choice",
            "by": "Decision-1",
            "option_id": out_opt_id,
            "chosen_id": pick,
            "answer_text": f"【Decision-1 決策】請採用方案「{chosen_label}」（最符合開發目標且不偏離範圍）。",
            "probs": probs or {},
            "reason": f"Decision-1 選出最不偏離目標方案：{chosen_label}" + (f"（{nums}）" if nums else ""),
        }

    # 描述類問題（descriptive） → 交由 GLM-5.3 作答
    f = Fence()
    prompt_parts = [
        "你是高階技術主管（GLM-5.3），負責回答開發或審查工程師提出的描述類／背景補充問題，確保團隊不偏離開發目標。",
        f.notice,
        f"開發目標：\n{goal}",
        f.wrap("規格與驗收條件", spec_text[:2500]) if spec_text else "",
        f.wrap("實作計畫", plan_text[:2000]) if plan_text else "",
        f.wrap("母資料夾 A 唯讀參考資料", ref_snippets) if ref_snippets else "",
        f.wrap("工程師提出的問題", f"標題：{title}\n內容：\n{(detail or '')[:3000]}"),
        "請直接給出具體、明確、可立即執行的繁體中文解答（2–6 句內），並提醒工程師嚴格遵守目標資料夾修改邊界。",
    ]
    prompt = "\n\n".join(p for p in prompt_parts if p)
    w_ctx = {**(ctx or {}), "purpose": "GLM-5.3 描述題解答"}
    try:
        reply = (call_ctx(router.write, prompt, effort="normal", ctx=w_ctx) or "").strip()
    except Exception as e:
        return {
            "handoff": True,
            "kind": "descriptive",
            "by": "GLM-5.3",
            "option_id": "",
            "answer_text": "",
            "probs": {},
            "reason": f"GLM-5.3 無法產生描述題解答（{type(e).__name__}），轉交使用者決定",
        }
    if not reply:
        return {
            "handoff": True,
            "kind": "descriptive",
            "by": "GLM-5.3",
            "option_id": "",
            "answer_text": "",
            "probs": {},
            "reason": "GLM-5.3 回傳空白解答，轉交使用者決定",
        }
    orig_ids = {str(o.get("id")) for o in (options or []) if isinstance(o, dict)}
    out_opt_id = "answer" if "answer" in orig_ids else (next(iter(orig_ids)) if orig_ids else "answer")
    return {
        "handoff": False,
        "kind": "descriptive",
        "by": "GLM-5.3",
        "option_id": out_opt_id,
        "chosen_id": out_opt_id,
        "answer_text": reply,
        "probs": {},
        "reason": "GLM-5.3 已依開發目標與唯讀參考資料完成解答",
    }
