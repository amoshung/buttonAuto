"""共用的代理事件型別（ACP 客戶端、房間執行器、示範模式共用）。"""
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class AgentEvent:
    # kind: message / thought / tool / tool_update / plan / usage / notice / done / error
    # notice：不影響執行的提示（例如切換模型失敗、沿用預設）
    # usage 的 data: {"used": int, "size": int}（對話已用／上限 token）
    kind: str
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionRequest:
    tool_call_id: str
    title: str
    kind: str
    raw_input: Dict[str, Any]
    # 每項: optionId, name, kind(allow_once/allow_always/reject_once/reject_always)
    options: List[Dict[str, str]]


def command_text(req: PermissionRequest) -> str:
    """從 raw_input 取出指令文字；若無指令欄位但有目標檔案路徑，則附帶路徑資訊以免只剩空泛標題。"""
    raw = req.raw_input or {}
    for key in ("command", "cmd", "commandLine", "args"):
        val = raw.get(key)
        if isinstance(val, (list, tuple)):
            val = " ".join(str(x) for x in val)
        if isinstance(val, str) and val.strip():
            return val
    paths = _paths_of(raw, raw)
    if paths:
        return "%s (%s)" % (req.title or req.kind or "file_op", ", ".join(paths[:4]))
    return req.title


# ---- 時間軸細節（Task 19）：截斷、遮蔽金鑰 ----
import json as _json  # noqa: E402
import re as _re  # noqa: E402

DETAIL_MAX = 4000
MASK_MAX = 400000  # 一次遮蔽的最長輸入（事件細節最後只留幾千字）
# 指令列或輸出裡常見的「金鑰=值」寫法（值未知時也遮）
# 名稱部分都有長度上限並以字界開頭（避免長字串時的二次方回溯）；token 要是完整的字（tokenizer_config 不算）
_KEYISH = _re.compile(r"(?i)(\b(?:api[_-]?key|(?:[a-z0-9]{1,20}[_-]){0,3}token|secret[a-z_]{0,30}|password|passwd|"
                      r"authorization|bearer|x-api-key|private[_-]?key)\b"
                      r"[\"']?\s{0,5}[:=]?\s{0,5}[\"']?(?:bearer\s{1,5})?)([A-Za-z0-9._\-/+=]{8,})")
# 有固定前綴的金鑰（GitHub、OpenAI、中鋼主權 AI 風格）：不論前後文都遮
_PREFIXED = _re.compile(r"\b(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|sk-|moa_)[A-Za-z0-9_\-]{8,}")


def clip(text: Any, n: int = DETAIL_MAX, tail: bool = False) -> str:
    """截斷到 n 字；tail＝保留尾端（輸出最後幾行最有用）。"""
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    if len(s) <= n:
        return s
    if tail:
        return "…（前面省略 %d 字）\n" % (len(s) - n) + s[-n:]
    return s[:n] + "\n…（後面省略 %d 字）" % (len(s) - n)


def mask_text(text: str, secrets: Any = ()) -> str:
    """把已知的金鑰值與看起來像金鑰的「key=值」換成 ***。"""
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    for v in sorted({x for x in (secrets or ()) if isinstance(x, str) and len(x) >= 6}, key=len, reverse=True):
        s = s.replace(v, "***")
    if len(s) > MASK_MAX:  # 太長：留頭尾、中間省略（不會留下沒遮的部分）
        half = MASK_MAX // 2
        s = s[:half] + "\n…（中間省略 %d 字）\n" % (len(s) - 2 * half) + s[-half:]
    s = _PREFIXED.sub(lambda m: m.group(1) + "***", s)
    return _KEYISH.sub(lambda m: m.group(1) + "***", s)


def mask_detail(obj: Any, secrets: Any = ()) -> Any:
    """遞迴遮蔽 detail 裡的每個字串。"""
    if isinstance(obj, str):
        return mask_text(obj, secrets)
    if isinstance(obj, dict):
        return {str(k): mask_detail(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_detail(v, secrets) for v in obj]
    return obj


def _cmd_of(raw: Dict[str, Any]) -> str:
    for key in ("command", "cmd", "commandLine", "args"):
        val = raw.get(key)
        if isinstance(val, (list, tuple)):
            val = " ".join(str(x) for x in val)
        if isinstance(val, str) and val.strip():
            return val
    return ""


PATH_MAX = 260  # 單一路徑最長（保留尾端：檔名最重要）
_PATH_KEYS = ("path", "file_path", "filePath", "file", "target", "paths", "files", "notebook_path")


def _paths_of(data: Dict[str, Any], raw: Dict[str, Any]) -> List[str]:
    out = []  # type: List[str]
    for key in _PATH_KEYS:
        v = raw.get(key)
        for p in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(p, str) and p.strip() and p not in out:
                out.append(clip(p.strip(), PATH_MAX, tail=True))
    locs = data.get("locations")
    for loc in locs if isinstance(locs, (list, tuple)) else []:  # ACP：locations [{path, line}]
        p = loc.get("path") if isinstance(loc, dict) else None
        if isinstance(p, str) and p.strip() and p not in out:
            out.append(clip(p.strip(), PATH_MAX, tail=True))
    return out[:20]


def tool_detail(title: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """工具事件的細節：{title, kind, command, paths, input}（rawInput 其餘欄位 JSON）。"""
    data = data if isinstance(data, dict) else {}
    raw = data.get("rawInput") if isinstance(data.get("rawInput"), dict) else {}
    cmd = _cmd_of(raw)
    rest = {k: v for k, v in raw.items() if k not in ("command", "cmd", "commandLine", "args") and k not in _PATH_KEYS}
    try:
        inp = _json.dumps(rest, ensure_ascii=False, indent=1) if rest else ""
    except (TypeError, ValueError):
        inp = str(rest)
    out = {"title": clip(title or data.get("title") or "", 300), "kind": str(data.get("kind") or "")[:40],
           "command": clip(cmd), "paths": _paths_of(data, raw), "input": clip(inp)}
    return {k: v for k, v in out.items() if v}


def _text_of(v: Any) -> str:
    """ACP 的 content／rawOutput 形狀不一：取出文字。"""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(t for t in (_text_of(x) for x in v) if t)
    if isinstance(v, dict):
        for key in ("text", "output", "stdout", "content", "result"):
            if key in v:
                t = _text_of(v[key])
                if key == "stdout" and isinstance(v.get("stderr"), str) and v["stderr"].strip():
                    t = (t + "\n" if t else "") + v["stderr"]
                if t:
                    return t
        if isinstance(v.get("stderr"), str):
            return v["stderr"]
        if "exitCode" in v or "exit_code" in v:
            return "exit %s" % v.get("exitCode", v.get("exit_code"))
    return ""


def tool_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """tool_update 完成／失敗時的結果：{status, output}（輸出保留尾端）。"""
    data = data if isinstance(data, dict) else {}
    text = _text_of(data.get("content")) or _text_of(data.get("rawOutput"))
    out = {"status": str(data.get("status") or "")[:20]}
    if text:
        out["output"] = clip(text, tail=True)
    return out


def tool_patch(data: Dict[str, Any]) -> Dict[str, Any]:
    """tool_call_update 帶來的補充：只放更新裡真的有的欄位（title／kind／rawInput 推出的 command、paths、input／locations）。"""
    data = data if isinstance(data, dict) else {}
    out = {}  # type: Dict[str, Any]
    if isinstance(data.get("title"), str) and data["title"].strip():
        out["title"] = clip(data["title"], 300)
    if isinstance(data.get("kind"), str) and data["kind"].strip():
        out["kind"] = data["kind"][:40]
    full = tool_detail(out.get("title", ""), data)
    if isinstance(data.get("rawInput"), dict) and data["rawInput"]:
        for k in ("command", "input", "paths"):
            if full.get(k):
                out[k] = full[k]
    elif data.get("locations") and full.get("paths"):
        out["paths"] = full["paths"]
    return out
