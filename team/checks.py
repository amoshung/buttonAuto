"""關卡檢查：每個房間在「實作完成」後由助理自己跑，不採信代理的自述。

- build_issues：commit、未提交改動、越界檔案、空殼跡象。
- test_gate：跑測試指令、每條驗收條件的檢查指令，最後做「換回原狀」檢查（mutation_check）。
- mutation_check 在暫存的 detached worktree 裡進行，絕不改動代理的 worktree；結果是三態
  （True 通過／False 測試沒有真正檢查／None 無法驗證，交給使用者判斷）。
"""
import locale
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import verifier
from team import events, gitops, rounds
from team.gitops import GitError, Worktree

log = logging.getLogger(__name__)

TAIL_CHARS = 4000
ISSUE_TAIL = 1500
_IS_WIN = os.name == "nt"


@dataclass
class CmdResult:
    cmd: str
    exit_code: int
    seconds: float
    timed_out: bool
    tail: str
    cancelled: bool = False


CANCELLED = "已取消（房間暫停）"
_POLL = 0.1


def _kill_tree(p: subprocess.Popen) -> None:
    try:
        if _IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        p.kill()
    except OSError:
        pass


def _decode(data: bytes) -> str:
    """先當 UTF-8；出現很多替代字元（例如 Windows 主控台的 cp950 輸出）時改用系統編碼。"""
    text = data.decode("utf-8", errors="replace")
    bad = text.count("\ufffd")
    if bad and bad >= max(5, len(text) // 200):
        try:
            alt = data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
            if alt.count("\ufffd") < bad:
                return alt
        except LookupError:
            pass
    return text


def run_cmd(cwd: str, cmd: str, timeout: float = 600, env: Optional[Dict[str, str]] = None,
            cancel: Optional[threading.Event] = None, log_path: str = "",
            secrets: Sequence[str] = ()) -> CmdResult:
    """以 shell 執行；stdout 與 stderr 合併，只留最後 4000 字。逾時殺掉整個程序樹，exit_code=-1。
    cancel 被設定時也殺掉整個程序樹，回傳 cancelled=True、exit_code=-1（每 0.1 秒檢查一次）。"""
    start = time.monotonic()
    kw: Dict = {}
    if _IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    full_env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", **(env or {}))
    try:
        p = subprocess.Popen(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=full_env, **kw)
    except OSError as e:
        return CmdResult(cmd, -1, time.monotonic() - start, False, "無法執行指令：%s" % e)
    timed_out = cancelled = False
    deadline = start + timeout
    out = b""
    while True:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        try:
            out, _ = p.communicate(timeout=min(left, _POLL) if cancel is not None else left)
            break
        except subprocess.TimeoutExpired:
            continue
    if timed_out or cancelled:
        _kill_tree(p)
        try:
            out, _ = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = b""
    text = _decode(out or b"")
    if timed_out:
        text += "\n…（逾時 %s 秒，已停止）" % timeout
    if cancelled:
        text += "\n…（%s）" % CANCELLED
    if log_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
            with open(log_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(events.mask_text(text, secrets))
        except OSError:
            pass
    code = -1 if (timed_out or cancelled) else p.returncode
    return CmdResult(cmd, code, time.monotonic() - start, timed_out, text[-TAIL_CHARS:], cancelled)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _unquote(name: str) -> str:
    """git 對含特殊字元的路徑會加引號並以 C 跳脫（八進位位元組）。"""
    name = name.rstrip("\r\n\t")
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        raw = name[1:-1].encode("utf-8").decode("unicode_escape").encode("latin-1")
        name = raw.decode("utf-8", errors="replace")
    return name


def added_lines(wt: Worktree) -> Dict[str, Set[int]]:
    """從 base 到工作目錄（含已提交與未提交）新增的行號；未追蹤的新檔整個算新增（以 None 標示全部）。"""
    out: Dict[str, Set[int]] = {}
    cur = ""
    for line in gitops.git(wt.path, "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "diff", "-U0",
                           "--no-renames", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/",
                           wt.base)\
            .splitlines():
        if line.startswith("+++ "):
            name = _unquote(line[4:])
            cur = name[2:] if name.startswith("b/") else ""
            if cur:
                out.setdefault(cur, set())
            continue
        m = _HUNK.match(line)
        if m and cur:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out[cur].update(range(start, start + count))
    return out


def build_issues(wt: Worktree, allowed: List[str]) -> List[str]:
    issues: List[str] = []
    if not gitops.commits_since(wt):
        issues.append("還沒有 commit")
    dirty = gitops.uncommitted(wt)
    if dirty:
        issues.append("有未提交的改動：" + "、".join(dirty[:5]) + ("…" if len(dirty) > 5 else ""))
    files = gitops.changed_files(wt)
    outside = gitops.outside_allowed(files, allowed)
    if outside:
        issues.append("改到範圍外的檔案：" + "、".join(outside[:10]) + ("…" if len(outside) > 10 else ""))
    added = added_lines(wt)
    untracked = set(gitops.git(wt.path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
    stubs = 0
    for f in files:
        if not os.path.isfile(os.path.join(wt.path, f)):
            continue
        new_lines = None if f in untracked else added.get(f, set())
        for s in verifier.scan_stubs(wt.path, [f]):  # 每個檔案各自掃，舊的空殼不會擠掉新的
            if new_lines is not None and int(s["line"]) not in new_lines:
                continue  # 只看這次新增的行：檔案裡原本就有的 TODO 不算
            issues.append("疑似空殼：%s:%s %s（%s）" % (s["file"], s["line"], s["kind"], s["text"]))
            stubs += 1
            if stubs >= 40:
                return issues
    return issues


@dataclass
class TestGate:
    __test__ = False  # 不是 pytest 測試類別
    ok: bool
    issues: List[str] = field(default_factory=list)
    results: List[CmdResult] = field(default_factory=list)
    unverified: str = ""  # 非空＝無法驗證（交給使用者，不送給代理）；此時 ok 一定是 False
    cancelled: bool = False  # 被取消（房間暫停）；結果不完整，呼叫端不要採用
    failed: Optional[List[str]] = None
    new_failures: List[str] = field(default_factory=list)
    env_failures: List[str] = field(default_factory=list)
    criteria_passed: List[str] = field(default_factory=list)
    fingerprint: str = ""
    logs: List[str] = field(default_factory=list)


def _fail_text(r: CmdResult) -> str:
    if r.timed_out:
        return "逾時（%d 秒）" % int(r.seconds)
    return "結束碼 %d" % r.exit_code


_SEP = object()  # 指令分隔（; && || | & 換行 ( $( 反引號）
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"env", "command", "exec", "sudo", "xargs", "nohup", "time", "nice", "call", "start"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "powershell", "pwsh"}


def _tokenize(cmd: str, escapes: bool) -> List[object]:
    """很小的 shell 斷詞：處理引號（相鄰引號會黏成同一詞）、分隔符號；escapes=True 時反斜線跳脫下一個字元，
    False 時反斜線當一般字元（Windows 路徑的讀法）。分隔處放 _SEP。"""
    out: List[object] = []
    cur: List[str] = []
    has = False
    i, n = 0, len(cmd)

    def flush() -> None:
        nonlocal cur, has
        if has:
            out.append("".join(cur))
        cur, has = [], False

    while i < n:
        c = cmd[i]
        if c in "'\"":
            j = i + 1
            while j < n and cmd[j] != c:
                if escapes and c == '"' and cmd[j] == "\\" and j + 1 < n and cmd[j + 1] in '"\\$`':
                    j += 1
                cur.append(cmd[j])
                j += 1
            has = True
            i = j + 1
            continue
        if c == "\\" and escapes:
            if i + 1 < n:
                cur.append(cmd[i + 1])
                has = True
            i += 2
            continue
        if c == "$" and i + 1 < n and cmd[i + 1] == "(":
            flush()
            out.append(_SEP)
            i += 2
            continue
        if c in ";&|()\n\r`":
            flush()
            out.append(_SEP)
            i += 1
            continue
        if c.isspace():
            flush()
            i += 1
            continue
        cur.append(c)
        has = True
        i += 1
    flush()
    return out


def _cmd_name(tok: str) -> str:
    name = re.split(r"[\\/]", tok)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _segment_problem(toks: List[str], depth: int) -> str:
    i = 0
    while i < len(toks):
        t = toks[i]
        if _ASSIGN.match(t):
            if _cmd_name(t.split("=", 1)[1]) == "git":
                return "測試指令不可以用 git"
            i += 1
            continue
        name = _cmd_name(t)
        if name in _WRAPPERS:
            i += 1
            while i < len(toks) and toks[i].startswith("-"):  # 包裝程式自己的選項
                i += 1
            continue
        if name == "cmd":
            rest = toks[i + 1:]
            if rest and rest[0].lower() in ("/c", "/k", "/s/c"):
                return _problem(" ".join(rest[1:]), depth + 1)
            return ""
        if name in _SHELLS:
            for k in range(i + 1, len(toks)):
                if toks[k].lower() in ("-c", "-command", "-com", "-cmd") and k + 1 < len(toks):
                    return _problem(" ".join(toks[k + 1:]), depth + 1)
            return ""
        break
    if i >= len(toks):
        return ""
    first = toks[i]
    if first.startswith("$") or first.startswith("%"):
        return "測試指令不可以用變數當指令"
    if _cmd_name(first) == "git":
        return "測試指令不可以用 git"
    return ""


def _problem(cmd: str, depth: int = 0) -> str:
    if depth > 5:
        return "測試指令巢狀太深"
    for escapes in (True, False):  # shell 跳脫與 Windows 路徑兩種讀法，任一種有問題就算
        seg: List[str] = []
        for tok in _tokenize(cmd, escapes) + [_SEP]:
            if tok is _SEP:
                prob = _segment_problem(seg, depth) if seg else ""
                if prob:
                    return prob
                seg = []
            else:
                seg.append(tok)  # type: ignore[arg-type]
    return ""


def git_problem(cmd: str) -> str:
    """測試指令在「指令位置」用了 git（含路徑、引號、跳脫、包裝程式、sh -c、cmd /c、變數）就回說明，否則回空字串。
    只出現在參數裡（例如 pytest -k "not git"、路徑中的 git 資料夾）不算。"""
    return _problem(cmd or "")


def uses_git(cmd: str) -> bool:
    return bool(git_problem(cmd))


def deny_hit(cmd: str, deny_patterns: Optional[List[str]]) -> str:
    """指令命中危險規則（automation.check_deny）就回該規則，否則回空字串。延後匯入：automation 會載入 UI 模組。"""
    if not deny_patterns or not (cmd or "").strip():
        return ""
    from automation import check_deny
    return check_deny([cmd], list(deny_patterns)) or ""


def deny_issue(cmd: str, pattern: str) -> str:
    return "驗收指令命中危險規則：%s（%s）" % (pattern, cmd)


def _strip_env_failures(tail: str, env_failures: Sequence[str]) -> str:
    """從測試輸出摘要中濾除環境既有失敗（env_failures）的區塊與摘要行，避免送給代理盲修。"""
    if not tail or not env_failures:
        return tail
    env_ids = set(env_failures)
    env_funcs = {x.rsplit("::", 1)[-1] for x in env_ids if "::" in x}
    out_lines: List[str] = []
    skip_section = False
    for line in tail.splitlines():
        s = line.strip()
        if s.startswith("___") and s.endswith("___"):
            title = s.strip("_ ").strip()
            skip_section = title in env_ids or title in env_funcs or any(title.endswith(fn) for fn in env_funcs)
            if skip_section:
                continue
        elif s.startswith("===") and s.endswith("==="):
            skip_section = False
        if skip_section:
            continue
        if any(eid in line for eid in env_ids) or any(fn in line for fn in env_funcs):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def test_gate(wt: Worktree, test_cmd: str, criteria: List[Dict[str, Any]],
              cancel: Optional[threading.Event] = None,
              deny_patterns: Optional[List[str]] = None,
              baseline: Optional[Any] = None,
              env_accepted: Sequence[str] = (),
              log_dir: str = "",
              gate_no: int = 0,
              secrets: Sequence[str] = ()) -> TestGate:
    """deny_patterns：命中危險規則的指令不執行，記為缺口（縱深防禦；spec 階段已要求使用者確認）。"""
    issues: List[str] = []
    results: List[CmdResult] = []
    logs: List[str] = []
    cache: Dict[str, CmdResult] = {}
    unverified = ""
    tests_ok = False
    failed: Optional[List[str]] = None
    new_failures: List[str] = []
    env_failures: List[str] = []
    criteria_passed: List[str] = []
    base_failed = set(
        getattr(baseline, "failed", None)
        or (baseline.get("failed", []) if isinstance(baseline, dict) else [])
    ) | set(env_accepted or ())

    def _exec(c_str: str) -> CmdResult:
        if c_str in cache:
            return cache[c_str]
        lp = os.path.join(log_dir, "gate-%d-%d.txt" % (gate_no, len(results) + 1)) if log_dir else ""
        res = run_cmd(wt.path, c_str, cancel=cancel, log_path=lp, secrets=secrets)
        results.append(res)
        if lp and os.path.isfile(lp):
            logs.append(lp)
        cache[c_str] = res
        return res

    test_res: Optional[CmdResult] = None
    tcmd = (test_cmd or "").strip()
    if not tcmd:
        issues.append("沒有測試指令，無法驗證")
    elif git_problem(tcmd):
        issues.append("%s：%s" % (git_problem(tcmd), tcmd))
    elif deny_hit(tcmd, deny_patterns):
        issues.append(deny_issue(tcmd, deny_hit(tcmd, deny_patterns)) + "，不執行")
    else:
        test_res = _exec(tcmd)
        if test_res.cancelled:
            return TestGate(False, [CANCELLED], results, "", True, logs=logs)
        failed = rounds.parse_failures(test_res.tail)
        if failed is not None:
            new_failures = sorted(set(failed) - base_failed)
            env_failures = sorted(set(failed) & base_failed)
        tests_ok = (test_res.exit_code == 0) or (failed is not None and len(new_failures) == 0)
        if not tests_ok:
            shown_tail = _strip_env_failures(test_res.tail[-ISSUE_TAIL:], env_failures)
            if failed is not None and new_failures:
                issues.append(
                    "測試沒通過（%s，新增失敗：%s）：%s\n%s"
                    % (_fail_text(test_res), "、".join(new_failures[:10]), tcmd, shown_tail)
                )
            else:
                issues.append("測試沒通過（%s）：%s\n%s" % (_fail_text(test_res), tcmd, shown_tail))
    for c in criteria or []:
        cid = str(c.get("id", "?"))
        check = (c.get("check") or "").strip()
        if not check or check.lower() == "manual" or bool(c.get("env_dependent")):
            continue
        if git_problem(check):
            issues.append("條件 %s：%s（%s）" % (cid, git_problem(check), check))
            continue
        hit = deny_hit(check, deny_patterns)
        if hit:
            issues.append("條件 %s：%s，不執行" % (cid, deny_issue(check, hit)))
            continue
        r = _exec(check)
        if r.cancelled:
            return TestGate(False, [CANCELLED], results, "", True, logs=logs)
        if r.exit_code == 0 or (check == tcmd and tests_ok):
            criteria_passed.append(cid)
        else:
            issues.append("條件 %s 的檢查沒通過（%s）：%s\n%s"
                          % (cid, _fail_text(r), check, r.tail[-ISSUE_TAIL:]))

    fp = ""
    if not tests_ok and test_res is not None:
        fp = rounds.fingerprint(new_failures if failed is not None else None, test_res.tail, test_res.exit_code)
    elif issues:
        bad_r = next((x for x in results if x.exit_code != 0), None)
        fp = rounds.fingerprint(
            new_failures or None,
            bad_r.tail if bad_r else "\n".join(issues),
            bad_r.exit_code if bad_r else 1,
        )

    if tests_ok:  # 測試本身沒過時，換回原狀檢查沒有意義
        if cancel is not None and cancel.is_set():
            return TestGate(False, [CANCELLED], results, "", True, logs=logs)
        mut_cmd = tcmd
        if test_res is not None and test_res.seconds > 120 and "pytest" in tcmd:
            tfiles = [
                f for f in gitops.changed_files(wt)
                if is_test_path(f) and not f.replace("\\", "/").startswith("docs/")
                and os.path.isfile(os.path.join(wt.path, f))
            ]
            if tfiles:
                mut_cmd = "%s %s" % (tcmd, " ".join('"%s"' % f for f in tfiles))
        ok, msg = mutation_check(wt, mut_cmd, cancel=cancel, env_failures=base_failed)
        if cancel is not None and cancel.is_set():
            return TestGate(False, [CANCELLED], results, "", True, logs=logs)
        if ok is None:
            unverified = msg
        elif not ok:
            issues.append(msg)
    return TestGate(
        not issues and not unverified,
        issues,
        results,
        unverified,
        False,
        failed=failed,
        new_failures=new_failures,
        env_failures=env_failures,
        criteria_passed=criteria_passed,
        fingerprint=fp,
        logs=logs,
    )


def quick_check(
    wt: Worktree,
    criteria: List[Dict[str, Any]],
    test_cmd: str,
    cancel: Optional[threading.Event] = None,
    deny_patterns: Optional[List[str]] = None,
) -> List[str]:
    """輕量量測：只跑各條件的 check（跳過 manual、env_dependent、與 test_cmd 相同、git/deny 命中），回傳通過的條件 id。"""
    tcmd = (test_cmd or "").strip()
    cache: Dict[str, int] = {}
    passed: List[str] = []
    for c in criteria or []:
        cid = str(c.get("id", "?"))
        check = (c.get("check") or "").strip()
        if not check or check.lower() == "manual" or bool(c.get("env_dependent")) or check == tcmd:
            continue
        if git_problem(check) or deny_hit(check, deny_patterns):
            continue
        if check not in cache:
            r = run_cmd(wt.path, check, cancel=cancel)
            if r.cancelled:
                break
            cache[check] = r.exit_code
        if cache.get(check) == 0:
            passed.append(cid)
    return passed


_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs"}
_TEST_NAMES = re.compile(r"^(test_.*|.*_test\..*|.*\.test\..*|.*\.spec\..*|conftest\.py)$", re.I)
# 常見被 .gitignore 排除、但跑測試需要的相依資料夾：在暫存副本裡做連結
DEP_DIRS = ["node_modules", ".venv", "venv", "env", "vendor", ".tox"]
# 換回原狀／基準執行時不寫 .pyc，避免同秒同大小的改動讀到舊的位元組碼
MUT_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
MUTATION_DIR = ".ai-team-mutation"


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.startswith("docs/"):
        return True
    parts = p.split("/")
    return bool(_TEST_NAMES.match(parts[-1])) or any(seg.lower() in _TEST_DIRS for seg in parts[:-1])


def _blob(wt: Worktree, rev: str, rel: str) -> Optional[bytes]:
    """回傳 rev:rel 的 bytes；不存在回 None。"""
    try:
        gitops.git(wt.path, "cat-file", "-e", "%s:%s" % (rev, rel))
    except GitError:
        return None
    p = subprocess.run(["git", "-c", "core.quotepath=false", "show", "%s:%s" % (rev, rel)], cwd=wt.path,
                       capture_output=True, timeout=60)
    if p.returncode != 0:
        raise GitError("讀取 %s:%s 失敗" % (rev, rel))
    return p.stdout


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.remove(path)


def _link_dir(src: str, dst: str) -> None:
    """建立資料夾連結；Windows 沒有 symlink 權限時改用 junction。失敗丟 OSError。"""
    try:
        os.symlink(src, dst, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as e:
        if not _IS_WIN:
            raise OSError("無法連結 %s：%s" % (os.path.basename(src), e))
    p = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0 or not os.path.isdir(dst):
        raise OSError("無法連結 %s：%s" % (os.path.basename(src),
                                          p.stderr.decode("utf-8", "replace").strip() or p.returncode))


def _unlink_dir(path: str) -> bool:
    """只移除連結本身，不碰連結指向的內容（symlink 用 unlink，junction 用 rmdir）。成功回 True。"""
    try:
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            os.rmdir(path)  # junction：rmdir 只移除連結；真的資料夾有內容時會失敗，不會刪內容
        return not os.path.lexists(path)
    except OSError:
        return False


class _Unsafe(Exception):
    pass


def _inside(root_real: str, path: str) -> bool:
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real.rstrip(os.sep) + os.sep)


def _safe_target(root_real: str, tmp: str, rel: str) -> str:
    """副本裡 rel 的路徑；若實際會落在副本外（經過連結）就丟 _Unsafe，絕不透過連結寫入。"""
    full = os.path.join(tmp, rel)
    if not _inside(root_real, full):
        raise _Unsafe(rel)
    return full


def _touch_later(path: str) -> None:
    t = time.time() + 2
    os.utime(path, (t, t))


def _clear_pycache(tmp: str, links: List[str]) -> None:
    """刪掉副本內的 __pycache__（不經過連結）。"""
    skip = {os.path.normcase(os.path.abspath(x)) for x in links}
    root_real = os.path.realpath(tmp)
    for dirpath, dirnames, _ in os.walk(tmp, followlinks=False):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if os.path.normcase(os.path.abspath(full)) in skip or os.path.islink(full) \
                    or not _inside(root_real, full):
                continue
            if d == "__pycache__":
                shutil.rmtree(full, ignore_errors=True)
            else:
                keep.append(d)
        dirnames[:] = keep


def mutation_check(wt: Worktree, test_cmd: str,
                   cancel: Optional[threading.Event] = None,
                   env_failures: Sequence[str] = ()) -> Tuple[Optional[bool], str]:
    """把這次改動的原始碼換回 base 版本後跑測試，期望測試失敗。

    回傳 (True, 說明)＝測試確實檢查了改動；(False, 說明)＝換回後仍通過，測試是假的；
    (None, 原因)＝無法驗證（沒有原始碼改動、暫存副本建立失敗、副本裡測試本身就跑不過、
    要換回的檔案位在連結的相依資料夾裡…），交給使用者。

    在代理 worktree 旁邊的 .ai-team-mutation/<名稱>-<hex>（同一磁碟）建立 detached worktree，疊上代理
    尚未提交的檔案、連結被忽略的相依資料夾，再換回原狀；代理的 worktree 完全不動，任何寫入都不經過連結。
    換回後測試失敗時，把副本恢復成改動後的樣子重跑一次當基準：基準也失敗代表副本環境不完整，回 None。
    兩次執行都不寫 .pyc，執行前清掉副本內的 __pycache__，寫回的檔案 mtime 往後調 2 秒。
    cancel 被設定 → 停掉執行中的測試、清掉副本，回 (None, 已取消)。
    """
    sources = [f for f in gitops.changed_files(wt) if not is_test_path(f)]
    if not sources:
        return None, "沒有原始碼改動，無法換回原狀檢查"
    parent = os.path.join(os.path.dirname(os.path.abspath(wt.path)), MUTATION_DIR)
    tmp = os.path.join(parent, "%s-%s" % (os.path.basename(os.path.abspath(wt.path)), secrets.token_hex(4)))
    state: Dict[str, Any] = {"added": False, "links": []}
    result: Tuple[Optional[bool], str] = (None, "換回原狀檢查沒有完成")
    try:
        result = _mutate(wt, test_cmd, sources, parent, tmp, state, cancel, set(env_failures or ()))
    finally:
        leftover = _cleanup_copy(wt, parent, tmp, state)
    if leftover:
        result = (result[0], result[1] + "\n注意：暫存副本 %s 裡的連結無法移除，已保留不刪；請確認後手動刪除。" % leftover)
    return result


def _mutate(wt: Worktree, test_cmd: str, sources: List[str], parent: str, tmp: str,
            state: Dict[str, Any], cancel: Optional[threading.Event] = None,
            env_set: Optional[Set[str]] = None) -> Tuple[Optional[bool], str]:
    links: List[str] = state["links"]
    env_s = set(env_set or ())
    try:
        os.makedirs(parent, exist_ok=True)
        gitops.git(wt.path, "worktree", "add", "--detach", tmp, "HEAD", timeout=gitops.WORKTREE_ADD_TIMEOUT)
        state["added"] = True
        root_real = os.path.realpath(tmp)
        for rel in gitops.uncommitted(wt):  # 疊上未提交與未追蹤的檔案，讓副本等於代理目前的狀態
            src, dst = os.path.join(wt.path, rel), _safe_target(root_real, tmp, rel)
            if os.path.isfile(src):
                with open(src, "rb") as f:
                    _write(dst, f.read())
            else:
                _remove(dst)
        for name in DEP_DIRS:
            src, dst = os.path.join(wt.path, name), os.path.join(tmp, name)
            if os.path.isdir(src) and not os.path.lexists(dst):
                _link_dir(src, dst)
                links.append(dst)
        current: Dict[str, Optional[bytes]] = {}
        for rel in sources:
            full = _safe_target(root_real, tmp, rel)
            if os.path.isfile(full):
                with open(full, "rb") as f:
                    current[rel] = f.read()
            else:
                current[rel] = None
            old = _blob(wt, wt.base, rel)
            if old is None:
                _remove(full)  # base 沒有的新檔：移走
            else:
                _write(full, old)
                _touch_later(full)
        _clear_pycache(tmp, links)
        r = run_cmd(tmp, test_cmd, env=MUT_ENV, cancel=cancel)
        if r.cancelled:
            return None, CANCELLED
        mut_failed = rounds.parse_failures(r.tail) if env_s else None
        if r.exit_code == 0 or (mut_failed is not None and set(mut_failed) <= env_s):
            return False, "把改動換回原狀後測試仍然通過：測試沒有真正檢查這次的改動"
        for rel, data in current.items():  # 恢復成改動後的樣子，確認副本本身能通過測試
            full = _safe_target(root_real, tmp, rel)
            if data is None:
                _remove(full)
            else:
                _write(full, data)
                _touch_later(full)
        _clear_pycache(tmp, links)
    except _Unsafe as e:
        return None, "無法安全地換回 %s：它位在連結到代理 worktree 的資料夾裡" % e
    except (GitError, OSError, subprocess.SubprocessError) as e:
        return None, "無法建立暫存副本做換回原狀檢查：%s" % e
    base_run = run_cmd(tmp, test_cmd, env=MUT_ENV, cancel=cancel)
    if base_run.cancelled:
        return None, CANCELLED
    base_failed = rounds.parse_failures(base_run.tail) if env_s else None
    base_ok = (base_run.exit_code == 0) or (base_failed is not None and set(base_failed) <= env_s)
    if not base_ok:
        return None, ("暫存副本裡測試本身就無法通過（%s），無法做換回原狀檢查；"
                      "可能是測試依賴被 .gitignore 排除的檔案：\n%s"
                      % (_fail_text(base_run), base_run.tail[-ISSUE_TAIL:]))
    return True, "把 %d 個原始碼檔換回原狀後測試失敗，測試確實檢查了這次的改動" % len(current)


def _cleanup_copy(wt: Worktree, parent: str, tmp: str, state: Dict[str, Any]) -> str:
    """拆連結後刪掉副本。連結拆不掉就保留副本（不 remove、不 prune），回傳副本路徑讓使用者手動處理。"""
    unlinked = all([_unlink_dir(ln) for ln in state["links"]])  # 先拆連結，避免刪副本時碰到代理的相依資料夾
    if not unlinked:
        log.warning("暫存副本 %s 的連結無法移除，保留副本不刪，請手動檢查後刪除", tmp)
        return tmp
    if state["added"]:
        try:
            gitops.git(wt.path, "worktree", "remove", "--force", tmp)
        except GitError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        gitops.git(wt.path, "worktree", "prune")
    except GitError:
        pass
    try:
        os.rmdir(parent)  # 空了才會成功
    except OSError:
        pass
    return ""
