"""每輪進展量測：失敗測試名單解析、錯誤指紋、RoundStat 與有進展判斷。"""
import hashlib
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

ROUNDS_KEEP = 50

# pytest: FAILED path::test - reason / ERROR path::test
_PYTEST_FAIL = re.compile(r"^\s*(?:FAILED|ERROR)\s+([^\s]+::[^\s]+?)(?:\s+-\s+.*)?\s*$")
_PYTEST_ERROR_FILE = re.compile(r"^\s*ERROR\s+([^\s]+\.py)(?:\s+-\s+.*)?\s*$")
# jest: ✕ name (5 ms) / × name (5 ms) / ● suite › name
_JEST_CROSS = re.compile(r"^\s*[✕×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
_JEST_BULLET = re.compile(r"^\s*●\s+(.+? › .+?)\s*$")
# go: --- FAIL: TestName (0.00s)
_GO_FAIL = re.compile(r"^\s*---\s*FAIL:\s*([^\s(]+)")

_ALL_PASSED = re.compile(
    r"(?:\b\d+\s+passed\b|\bTests:\s+\d+\s+passed\b|^PASS\s*$|^ok\s+\S+)",
    re.MULTILINE,
)
_HAS_FAIL_SUMMARY = re.compile(r"(?:\b\d+\s+(?:failed|errors?)\b|^FAIL\b)", re.MULTILINE)
_ERROR_LINE = re.compile(r"(?i)(error|exception|assert|failed)")

_HEX_ADDR = re.compile(r"\b0x[0-9a-fA-F]+\b")
_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s:,'\"()]+")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9._\-]+/)+[A-Za-z0-9._\-]+")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_WS = re.compile(r"\s+")


def parse_failures(output: str) -> Optional[List[str]]:
    """從測試輸出解析排序去重的失敗測試 id；解析不到回 None，全部通過回 []。"""
    if not output:
        return None
    found = set()
    for raw in output.splitlines():
        line = raw.rstrip("\r\n")
        for pat in (_PYTEST_FAIL, _PYTEST_ERROR_FILE, _JEST_CROSS, _JEST_BULLET, _GO_FAIL):
            m = pat.match(line)
            if m:
                found.add(m.group(1).strip())
                break
    if found:
        return sorted(found)
    if _ALL_PASSED.search(output) and not _HAS_FAIL_SUMMARY.search(output):
        return []
    return None


def normalize_line(line: str) -> str:
    """正規化單行錯誤：0x...→<addr>、Windows/POSIX 路徑→<path>、數字→#、連續空白壓成一格。"""
    s = line or ""
    s = _HEX_ADDR.sub("<addr>", s)
    s = _WIN_PATH.sub("<path>", s)
    s = _POSIX_PATH.sub("<path>", s)
    s = _NUM.sub("#", s)
    return _WS.sub(" ", s).strip()


def first_error_line(output: str) -> str:
    """取出第一個含 error / exception / assert / failed（不分大小寫）的行。"""
    for line in (output or "").splitlines():
        s = line.strip()
        if s and _ERROR_LINE.search(s):
            return s
    return ""


def fingerprint(failed: Optional[List[str]], output: str, exit_code: int) -> str:
    """失敗測試名單＋第一個錯誤行正規化後的雜湊前 12 碼；全部通過回空字串。"""
    if exit_code == 0 and failed == []:
        return ""
    items = sorted(set(failed)) if failed else ["exit=%d" % exit_code]
    err = normalize_line(first_error_line(output))
    payload = "\n".join(items) + "\n" + err
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:12]


@dataclass
class RoundStat:
    n: int
    at: str
    level: str
    seat: str
    model: str
    prompt_versions: Dict[str, str] = field(default_factory=dict)
    criteria_passed: List[str] = field(default_factory=list)
    new_failures: List[str] = field(default_factory=list)
    env_failures: List[str] = field(default_factory=list)
    fingerprint: str = ""
    commits_added: int = 0
    evidence_mismatch: bool = False
    tokens: int = 0
    seconds: float = 0.0
    review_blocking: Optional[int] = None
    stage: str = "build"
    done: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RoundStat":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


def progressed(prev: Optional[RoundStat], cur: RoundStat) -> bool:
    """依設計 §5.1.1 判斷這一輪是否有進展。"""
    if cur.evidence_mismatch:
        return False
    if prev is None:
        return bool(cur.criteria_passed) or cur.commits_added > 0
    if len(cur.new_failures) > len(prev.new_failures):
        return False
    if len(cur.criteria_passed) > len(prev.criteria_passed):
        return True
    if len(cur.new_failures) < len(prev.new_failures):
        return True
    if cur.fingerprint != prev.fingerprint and cur.commits_added > 0:
        return True
    if cur.review_blocking is not None and prev.review_blocking is not None:
        if cur.review_blocking < prev.review_blocking:
            return True
    return False


def append_round(rounds: List[Dict[str, Any]], stat: RoundStat) -> List[Dict[str, Any]]:
    """附加一筆 RoundStat（轉成 dict）並只保留最後 ROUNDS_KEEP 筆。"""
    out = list(rounds or []) + [stat.to_dict()]
    return out[-ROUNDS_KEEP:]
