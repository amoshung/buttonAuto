"""辦公室冒煙測試：讓每個真 CLI 在拋棄式 sandbox 裡建立一個檔案，確認「啟動 → 交握 → 權限 → 完成」整條路真的通。

用法（在命令提示字元，或雙擊 tools\\team_smoke.bat）：
  python tools\\team_smoke.py                    測試找得到的 kiro-cli／agy／claude
  python tools\\team_smoke.py --only kiro        只測一個（kiro／agy／claude）
  python tools\\team_smoke.py --sandbox D:\\x     指定 sandbox（預設：buttonAuto 上一層的 ai-team-sandbox）
  python tools\\team_smoke.py --seat glm         測 GLM 座位（claude CLI 走公司 LiteLLM；glm_high 同理）
  python tools\\team_smoke.py --probe-all        對每個座位做用量試探，印出 QuotaBook 狀態
  --cli-override 名稱=指令   （測試用，可重複）以指令取代該 CLI 的啟動方式，例：kiro=python fake.py

結果另存 recon\\team-smoke-<時間>.json。全部成功結束碼 0，否則 1。金鑰不會印出或存檔。
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from team.clis import CLI_SPECS, CliInfo, discover  # noqa: E402
from team.drivers import GLM_SEATS, SEATS, make_driver, seat_launch, with_cert_env  # noqa: E402
from team.events import AgentEvent, PermissionRequest, command_text  # noqa: E402
from team.fleet import SEAT_LABELS, QuotaBook, detect_quota  # noqa: E402
from team import netcerts  # noqa: E402
from team.secrets import merge_env  # noqa: E402
from team.models import ModelRouter, load_model_config  # noqa: E402
from team import policy  # noqa: E402

TIMEOUT = 300.0
PROBE_PROMPT = "回覆 OK"
PROBE_TIMEOUT = 90.0
GIT_ID = ["-c", "user.name=ai-team", "-c", "user.email=ai-team@local", "-c", "commit.gpgsign=false",
          "-c", "core.longpaths=true"]
EDIT_KINDS = {"edit", "delete", "move"}

LOGIN_HINT = {
    "kiro": "請在命令提示字元執行 kiro-cli login（用 kiro-cli whoami 確認已登入）",
    "agy": "請先執行 agy 並完成登入（登入後再重跑）",
    "claude": "請在命令提示字元執行 claude 並完成登入（/login）",
}
INSTALL_HINT = {
    "kiro": "找不到 kiro-cli：請確認已安裝，或把安裝資料夾加入 PATH 後重開視窗",
    "agy": "找不到 agy：請確認已安裝，或把安裝資料夾加入 PATH 後重開視窗",
    "claude": "找不到 claude：請確認已安裝（claude.exe），或把安裝資料夾加入 PATH 後重開視窗",
}


# ---- 小工具 ----
def mask(text: str, secrets: List[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def say(msg: str = "") -> None:
    print(msg, flush=True)


def parse_override(spec: str) -> Tuple[str, List[str]]:
    name, sep, cmd = spec.partition("=")
    name = name.strip()
    if not sep or not cmd.strip():
        raise ValueError("--cli-override 格式：名稱=指令（例：kiro=python fake.py）")
    parts = shlex.split(cmd, posix=(os.name != "nt"))
    parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return name, parts


def short_tmp(prefix: str = "ats-") -> str:
    """盡量短的暫存路徑（Windows MAX_PATH）。"""
    for _ in range(20):
        p = os.path.join(tempfile.gettempdir(), prefix + uuid.uuid4().hex[:4])
        if not os.path.exists(p):
            os.makedirs(p)
            return p
    return tempfile.mkdtemp(prefix=prefix)


def _onerror(func, path, _exc):
    """.git 物件是唯讀檔：改權限後重試一次。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def cleanup_tmp(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    shutil.rmtree(path, onerror=_onerror)
    if os.path.exists(path):
        say("（暫存資料夾沒刪乾淨，請手動刪除：%s）" % path)


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)


MARKER = ".ai-team-sandbox"


def ensure_repo(path: str, require_marker: bool = True) -> Optional[str]:
    """sandbox 必須是空資料夾（或不存在），或含 .ai-team-sandbox 標記檔；
    不存在／還不是 repo 就 git init 並只提交 README.md 與標記檔。回傳錯誤訊息或 None。"""
    try:
        if os.path.exists(path) and not os.path.isdir(path):
            return "sandbox 路徑不是資料夾：%s" % path
        if require_marker and os.path.isdir(path):
            entries = os.listdir(path)
            if entries and MARKER not in entries:
                return ("拒絕使用 %s 當 sandbox：資料夾不是空的，也沒有 %s 標記檔。"
                        "請用 --sandbox 指定空資料夾；若這是本工具先前建立的 sandbox，"
                        "在裡面建立一個空檔 %s 即可" % (path, MARKER, MARKER))
        os.makedirs(path, exist_ok=True)
        marker = os.path.join(path, MARKER)
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as f:
                f.write("ai-team smoke-test sandbox; safe to delete\n")
        inside = _git(path, "rev-parse", "--is-inside-work-tree")
        head = _git(path, "rev-parse", "--verify", "-q", "HEAD")
        if inside.returncode == 0 and head.returncode == 0:
            return None
        if inside.returncode != 0:
            r = _git(path, "init")
            if r.returncode != 0:
                return "git init 失敗：" + (r.stderr or r.stdout).strip()[-200:]
        readme = os.path.join(path, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as f:
                f.write("# ai-team sandbox\n\n辦公室冒煙測試用的拋棄式資料夾。\n")
        _git(path, "add", "README.md", MARKER)
        r = subprocess.run(["git"] + GIT_ID + ["commit", "-m", "init sandbox"], cwd=path,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60)
        if r.returncode != 0:
            return "git commit 失敗：" + (r.stderr or r.stdout).strip()[-200:]
    except (OSError, subprocess.SubprocessError) as e:
        return "無法準備 sandbox（%s）：%s" % (path, e)
    return None


# ---- 權限 ----
def _deny_patterns() -> List[str]:
    try:
        from config import _default_deny_patterns
        return list(_default_deny_patterns())
    except Exception:
        return []


def decide_permission(req: PermissionRequest, scope: str, router: Optional[ModelRouter],
                      deny: List[str]) -> Tuple[str, str, str]:
    """回傳 (option_id, outcome, reason)。有模型設定 → policy.decide（真 ModelRouter），
    交給使用者者在冒煙測試中一律拒絕；讀不到模型設定 → edit 類允許、execute 類拒絕。"""
    if router is not None:
        d = policy.decide(req, goal="冒煙測試：在目前資料夾建立一個 hello 檔案", scope=scope,
                          deny_patterns=deny, router=router, read_only=False)
        if d.outcome == "allow":
            return d.option_id, "allow", d.reason
        opt = d.option_id or policy.pick_option(req.options, "reject")
        return opt, "reject", ("需要使用者決定，冒煙測試一律拒絕：" if d.outcome == "escalate" else "") + d.reason
    if req.kind in policy.READ_KINDS:
        return policy.pick_option(req.options, "allow"), "allow", "唯讀操作"
    if req.kind in EDIT_KINDS:
        return policy.pick_option(req.options, "allow"), "allow", "沒有模型設定：edit 類允許"
    return policy.pick_option(req.options, "reject"), "reject", "沒有模型設定：execute 類拒絕"


# ---- 結果分類 ----
_MODEL_RE = re.compile(r"model\b.{0,80}(not found|does not exist|not exist|unknown)|invalid model|"
                       r"unknown model|model_not_found", re.I | re.S)
_404_MSG_RE = re.compile(r"404.{0,200}messages|messages.{0,200}404|not found.{0,80}/v1/messages", re.I | re.S)
_AUTH_RE = re.compile(r"\b(401|403)\b|unauthori[sz]ed|forbidden|invalid api key|incorrect api key", re.I)
_CLI_AUTH_RE = re.compile(r"\b(log ?in|logged in|not logged|sign ?in|credentials?|unauthori[sz]ed|401|"
                          r"auth(entication|orization|orized)?)\b", re.I)
_TIMEOUT_RE = re.compile(r"逾時|timed out|timeout|沒有完成|沒有回應", re.I)


def glm_reason(text: str, no_file: bool = False) -> str:
    t = text or ""
    if _MODEL_RE.search(t):
        return "LiteLLM 不認得模型（model not found／invalid model）：請確認 litellm.local.json 的 glm_model（預設 glm-5.3）是否已開通"
    if _404_MSG_RE.search(t):
        return "公司 LiteLLM 沒有 /v1/messages，GLM 座位不可用（claude CLI 需要 Anthropic Messages 端點；請找 LiteLLM 管理者開啟，或改用其他座位）"
    if _AUTH_RE.search(t):
        return "LiteLLM 拒絕金鑰（401/403）：請檢查 litellm.local.json 的 key 是否有效、有沒有 glm-5.3 的權限"
    if _TIMEOUT_RE.search(t):
        return "等不到 LiteLLM 回應（逾時）：請確認公司網路／VPN 後再試"
    if re.search(r"connection|resolve|unreachable|ECONNREFUSED", t, re.I):
        return "連不上 LiteLLM：請檢查 litellm.local.json 的 base_url 與網路／VPN"
    if not t.strip() and no_file:
        return "模型沒有照指示建立檔案（代理回報完成，但 hello.txt 不存在或內容不對）"
    return "GLM 座位執行失敗（原因不明）：" + (t.strip()[-200:] or "沒有更多訊息")


_NET_RE = re.compile(r"UnknownIssuer|InvalidCertificate|UNABLE_TO_GET_ISSUER_CERT|SELF_SIGNED_CERT|"
                     r"self.signed certificate|certificate verify failed|dispatch failure|"
                     r"ECONNRESET|ENOTFOUND|ETIMEDOUT", re.I)
NET_REASON = "連不上服務（公司網路憑證或連線）"
NET_HINT = "憑證已自動套用仍失敗：請把 recon\\acp-trace 記錄給助理，或請 IT 提供公司代理設定"


def whoami_logged_in(info: CliInfo, timeout: float = 15.0) -> bool:
    """kiro-cli whoami 顯示已登入嗎（15 秒逾時；任何錯誤都當作不是）。"""
    try:
        r = subprocess.run([info.path, "whoami"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=merge_env(with_cert_env(None)))
    except Exception:
        return False
    out = ((r.stdout or "") + (r.stderr or "")).lower()
    return r.returncode == 0 and bool(out.strip()) and "not logged in" not in out and "login" not in out.replace("logged in", "")


def cli_reason(cli: str, text: str, spawn_failed: bool = False,
               logged_in: bool = False) -> Tuple[str, str]:
    """回傳 (原因, 建議)。logged_in：CLI 自己的 whoami 說已登入（此時「登入」類訊息其實是網路問題）。"""
    if spawn_failed:
        return "CLI 無法啟動", INSTALL_HINT.get(cli, "請確認已安裝")
    if _NET_RE.search(text or ""):
        return NET_REASON, NET_HINT
    if _CLI_AUTH_RE.search(text or ""):
        if logged_in:
            return NET_REASON + "（whoami 顯示已登入，訊息其實是連線問題）", NET_HINT
        return "可能尚未登入或授權失效", LOGIN_HINT.get(cli, "請先登入該 CLI")
    if _TIMEOUT_RE.search(text or ""):
        return "等不到回應（逾時）", "請手動執行該 CLI 看有沒有卡在登入或確認畫面，再重試"
    return ("原因不明：" + ((text or "").strip()[-200:] or "代理沒有回傳任何訊息"),
            "請看下面的「原始訊息」；也可手動執行該 CLI 確認。若尚未登入：" + LOGIN_HINT.get(cli, "請先登入"))


def _check_file(path: str, expected: str) -> Tuple[bool, str]:
    if not os.path.isfile(path):
        return False, "檔案 %s 沒有被建立" % os.path.basename(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError as e:
        return False, "無法讀取 %s：%s" % (os.path.basename(path), e)
    if body.strip() != expected:
        return False, "檔案內容不對（要的是「%s」，實際是「%s」）" % (expected, body.strip()[:80])
    return True, ""


def _head(repo: str) -> str:
    try:
        r = _git(repo, "rev-parse", "--verify", "-q", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _untrack(repo: str, fname: str) -> None:
    """先前測試已提交過這個檔：先移除並提交，讓這次代理的提交一定是新的。"""
    try:
        if _git(repo, "cat-file", "-e", "HEAD:%s" % fname).returncode != 0:
            return
        _git(repo, "rm", "-q", "-f", "--", fname)
        subprocess.run(["git"] + GIT_ID + ["commit", "-q", "--no-verify", "-m", "smoke: reset %s" % fname],
                       cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def _check_commit(repo: str, fname: str, before: str) -> Tuple[bool, str]:
    """git log 要看到新的提交，而且提交裡有 fname（headless 的 git commit 靠 --allowedTools 放行）。"""
    after = _head(repo)
    if not after or after == before:
        return False, "git log 看不到新的提交（代理沒有提交 %s）" % fname
    try:
        if _git(repo, "cat-file", "-e", "HEAD:%s" % fname).returncode != 0:
            return False, "最新的提交裡沒有 %s" % fname
    except (OSError, subprocess.SubprocessError) as e:
        return False, "無法讀取 git log：%s" % e
    return True, ""


COMMIT_ASK = "然後執行 `git add %s` 與 `git commit -m \"smoke: %s\"` 提交它，"
COMMIT_HINT = ("claude／GLM 座位以 headless 模式執行，git commit 要靠 --allowedTools 放行；"
               "請看上面的原始訊息，或更新 claude CLI 後再試")


# ---- 跑一個 CLI／座位 ----
DETAIL_MAX = 1500


class _Run:
    def __init__(self, secrets: List[str]):
        self.secrets = secrets
        self.events = 0
        self.errors = []  # type: List[str]
        self.perms = []  # type: List[Dict[str, str]]
        self.rejected = []  # type: List[str]
        self.stderr = ""
        self.handshake = False
        self.spawn_failed = False
        self.lock = threading.Lock()

    def on_event(self, ev: AgentEvent) -> None:
        with self.lock:
            self.events += 1
            if ev.kind == "error":
                self.errors.append(mask(ev.text, self.secrets))

    def detail(self, err: str, stop: str) -> str:
        parts = []
        if err:
            parts.append(err)
        parts += [e for e in self.errors if e not in err]
        if self.stderr.strip():
            parts.append("[stderr] " + self.stderr.strip())
        if not parts:
            parts.append("（代理沒有回傳任何錯誤訊息；stopReason=%s，事件 %d 個）" % (stop or "無", self.events))
        text = mask("\n".join(parts), self.secrets)
        return text[-DETAIL_MAX:]


def _hook_rejections(drv: Any, run: _Run) -> None:
    """不改 team/acp.py：包住 AcpClient._write，記下被我們以 -32601 回絕的代理→用戶端請求。"""
    client = getattr(drv, "client", None)
    orig = getattr(client, "_write", None)
    if client is None or orig is None:
        return

    def wrapped(msg: Dict[str, Any]) -> Any:
        try:
            err = msg.get("error") if isinstance(msg, dict) else None
            if isinstance(err, dict) and err.get("code") == -32601:
                m = str(err.get("message", "")).replace("Method not found: ", "")
                with run.lock:
                    if m not in run.rejected:
                        run.rejected.append(m)
        except Exception:
            pass
        return orig(msg)

    client._write = wrapped


def _drive(drv: Any, run: _Run, prompt: str, timeout: float) -> Tuple[str, str]:
    """回傳 (stop_reason, 錯誤文字)；交握結果寫進 run.handshake。"""
    _hook_rejections(drv, run)
    try:
        drv.start()
        run.handshake = True
        stop = drv.prompt(prompt, timeout)
        return stop, ""
    except Exception as e:  # noqa: BLE001 - 一律轉成中文原因，不給 traceback
        msg = mask("%s" % e, run.secrets)
        run.spawn_failed = isinstance(e, OSError) or "無法啟動" in msg
        return "", msg
    finally:
        try:
            run.stderr = "\n".join(list(getattr(getattr(drv, "client", None), "_stderr", []))[-8:])
        except Exception:
            pass
        try:
            drv.close()
        except Exception:
            pass


def _blank(name: str, label: str, path: str = "") -> Dict[str, Any]:
    return {"cli": name, "label": label, "path": path, "ok": False, "handshake": False,
            "events": 0, "permissions": [], "stop_reason": "", "file_ok": False,
            "reason": "", "hint": "", "detail": "", "rejected_methods": []}


def _print_common(run: _Run, res: Dict[str, Any], stop: str, show_perms: bool = True) -> None:
    say("  啟動與交握：%s" % ("成功" if res["handshake"] else "失敗"))
    say("  收到事件：%d 個" % run.events)
    if show_perms:
        for p in run.perms:
            say("  權限請求：%s（%s）→ %s，%s" % (p["title"] or p["command"], p["kind"],
                                              "允許" if p["outcome"] == "allow" else "拒絕", p["reason"]))
        if not run.perms:
            say("  權限請求：無")
    if run.rejected:
        say("  代理要求但不支援而被回絕的方法：%s" % "、".join(run.rejected))
    say("  stopReason：%s" % (stop or "（沒有）"))


def _finish(res: Dict[str, Any], run: _Run, err: str, stop: str) -> None:
    res["detail"] = run.detail(err, stop)
    res["rejected_methods"] = list(run.rejected)
    say("  結果：%s" % ("✅ 成功" if res["ok"] else "❌ 失敗：%s\n  建議：%s" % (res["reason"], res["hint"])))
    if not res["ok"]:
        say("  原始訊息：%s" % res["detail"])
    say()


def run_cli(name: str, info: CliInfo, cmd_override: Optional[List[str]], sandbox: str,
            router: Optional[ModelRouter], deny: List[str], timeout: float,
            secrets: List[str]) -> Dict[str, Any]:
    fname = "hello_%s.txt" % name
    target = os.path.join(sandbox, fname)
    expected = "hello from %s" % name
    try:
        if os.path.exists(target):
            os.remove(target)
    except OSError:
        pass
    want_commit = info.protocol == "headless"
    if want_commit:
        _untrack(sandbox, fname)
    before = _head(sandbox)
    prompt = "在目前資料夾建立 %s，內容寫一行 `%s`，%s完成後回覆 DONE。不要做其他事。" % (
        fname, expected, COMMIT_ASK % (fname, fname) if want_commit else "")
    res = _blank(name, info.label, info.path)
    run = _Run(secrets)

    def on_perm(req: PermissionRequest) -> str:
        try:
            opt, outcome, why = decide_permission(req, sandbox, router, deny)
        except Exception as e:  # noqa: BLE001
            opt, outcome, why = policy.pick_option(req.options, "reject"), "reject", "判斷失敗：%s" % type(e).__name__
        with run.lock:
            run.perms.append({"kind": req.kind, "title": mask(req.title, secrets)[:120],
                              "command": mask(command_text(req), secrets)[:160],
                              "outcome": outcome, "reason": mask(why, secrets)[:200]})
        return opt

    say("== %s（%s）==" % (info.label, name))
    say("  路徑：%s" % info.path)
    stop, err = "", ""
    try:
        drv = make_driver(info, sandbox, run.on_event, on_perm, read_only=False,
                          cmd_override=cmd_override)
        stop, err = _drive(drv, run, prompt, timeout)
    except Exception as e:  # noqa: BLE001
        err = mask("%s: %s" % (type(e).__name__, e), secrets)
        run.spawn_failed = isinstance(e, OSError)
    res["handshake"] = run.handshake
    res["events"] = run.events
    res["permissions"] = list(run.perms)
    res["stop_reason"] = stop
    all_err = "\n".join([err] + run.errors).strip()
    ok_file, why_file = _check_file(target, expected)
    res["file_ok"] = ok_file
    ok_commit, why_commit = _check_commit(sandbox, fname, before) if want_commit else (True, "")
    if want_commit:
        res["commit_ok"] = ok_commit
    _print_common(run, res, stop)
    say("  檔案：%s" % ("存在且內容正確" if ok_file else why_file))
    if want_commit:
        say("  提交：%s" % ("git log 看得到" if ok_commit else why_commit))
    if stop == "end_turn" and ok_file and ok_commit:
        res["ok"] = True
    elif stop == "end_turn" and ok_file and not ok_commit:
        res["reason"], res["hint"] = why_commit, COMMIT_HINT
    elif stop == "end_turn" and not ok_file and not all_err:
        res["reason"] = why_file
        res["hint"] = "代理說完成了但檔案不對；請看上面的權限請求是否被拒絕，或手動執行該 CLI 確認"
    else:
        logged_in = False
        if (name == "kiro" and not cmd_override and not run.spawn_failed
                and not _NET_RE.search(all_err) and _CLI_AUTH_RE.search(all_err)):
            logged_in = whoami_logged_in(info)
        res["reason"], res["hint"] = cli_reason(name, all_err, run.spawn_failed, logged_in)
    _finish(res, run, err, stop)
    return res


def _strip_serena(extra: List[str]) -> List[str]:
    if "--mcp-config" in extra:
        i = extra.index("--mcp-config")
        return extra[:i] + extra[i + 2:]
    return extra


def run_glm(seat: str, clis: Dict[str, CliInfo], overrides: Dict[str, List[str]], cfg: Any,
            timeout: float, secrets: List[str]) -> Dict[str, Any]:
    label = SEAT_LABELS.get(seat, seat)
    res = _blank(seat, label)
    say("== %s（%s 座位）==" % (label, seat))
    if cfg is None:
        res["reason"] = "讀不到 litellm.local.json（需要 base_url、key、decision1_key），GLM 座位不可用"
        res["hint"] = "在 buttonAuto 資料夾建立 litellm.local.json，或用 --litellm 指定位置"
        say("  結果：❌ 失敗：%s\n  建議：%s\n" % (res["reason"], res["hint"]))
        return res
    tmp = ""
    try:
        tmp = short_tmp()
        repo = os.path.join(tmp, "repo")
        err = ensure_repo(repo, require_marker=False)
        if err:
            res["reason"], res["hint"] = err, "請確認 git 已安裝且在 PATH"
            say("  結果：❌ 失敗：%s\n" % err)
            return res
        try:
            info, env, extra = seat_launch(seat, clis, cfg, (), repo, os.path.join(tmp, "mcp"))
        except ValueError as e:
            res["reason"], res["hint"] = str(e), INSTALL_HINT["claude"]
            say("  結果：❌ 失敗：%s\n  建議：%s\n" % (e, res["hint"]))
            return res
        extra = _strip_serena(list(extra))
        res["path"] = info.path
        target = os.path.join(repo, "hello.txt")
        before = _head(repo)
        prompt = "在目前資料夾建立 hello.txt，內容寫一行 `hello from glm`，%s完成後回覆 DONE。不要做其他事。" % (
            COMMIT_ASK % ("hello.txt", "hello.txt"))
        run = _Run(secrets)

        def on_perm(req: PermissionRequest) -> str:
            with run.lock:
                run.perms.append({"kind": req.kind, "title": mask(req.title, secrets)[:120],
                                  "command": mask(command_text(req), secrets)[:160],
                                  "outcome": "reject", "reason": "GLM 測試一律拒絕權限請求"})
            return policy.pick_option(req.options, "reject")

        stop, err_text = "", ""
        try:
            drv = make_driver(info, repo, run.on_event, on_perm, read_only=False,
                              cmd_override=overrides.get("claude"), env=env, extra_args=extra)
            stop, err_text = _drive(drv, run, prompt, timeout)
        except Exception as e:  # noqa: BLE001
            err_text = mask("%s: %s" % (type(e).__name__, e), secrets)
            run.spawn_failed = isinstance(e, OSError)
        res["handshake"] = run.handshake
        res["events"] = run.events
        res["permissions"] = list(run.perms)
        res["stop_reason"] = stop
        all_err = "\n".join([err_text] + run.errors).strip()
        ok_file, why_file = _check_file(target, "hello from glm")
        res["file_ok"] = ok_file
        ok_commit, why_commit = _check_commit(repo, "hello.txt", before)
        res["commit_ok"] = ok_commit
        _print_common(run, res, stop)
        say("  檔案：%s" % ("存在且內容正確" if ok_file else why_file))
        say("  提交：%s" % ("git log 看得到" if ok_commit else why_commit))
        if stop == "end_turn" and ok_file and ok_commit:
            res["ok"] = True
        elif stop == "end_turn" and ok_file:
            res["reason"], res["hint"] = why_commit, COMMIT_HINT
        else:
            if run.spawn_failed:
                res["reason"], res["hint"] = cli_reason("claude", all_err, True)
            else:
                res["reason"] = glm_reason(all_err, no_file=(stop == "end_turn" and not ok_file))
                res["hint"] = "改用 claude／kiro／agy 座位，或請 LiteLLM 管理者確認 /v1/messages 與模型 %s" % cfg.glm_model
        _finish(res, run, err_text, stop)
        return res
    finally:
        cleanup_tmp(tmp)


# ---- 用量試探 ----
def probe_all(clis: Dict[str, CliInfo], overrides: Dict[str, List[str]], cfg: Any,
              timeout: float, secrets: List[str]) -> List[Dict[str, str]]:
    from datetime import datetime
    tmp = ""
    out = []  # type: List[Dict[str, str]]
    try:
        tmp = short_tmp("atp-")
        quota = QuotaBook(os.path.join(tmp, "quota.json"))
        say("== 座位用量試探（每個座位送「%s」）==" % PROBE_PROMPT)
        for seat in SEATS:
            d = os.path.join(tmp, seat)
            os.makedirs(d, exist_ok=True)
            rec = {"seat": seat, "state": "unavailable", "detail": ""}
            try:
                info, env, extra = seat_launch(seat, clis, cfg, (), d, d)
            except ValueError as e:
                rec["detail"] = mask(str(e), secrets)
                out.append(rec)
                continue
            extra = _strip_serena(list(extra))
            key = seat if seat in ("kiro", "agy") else "claude"
            run = _Run(secrets)
            stop, err = "", ""
            try:
                drv = make_driver(info, d, run.on_event,
                                  lambda req: policy.pick_option(req.options, "reject"), True,
                                  cmd_override=overrides.get(key), env=env, extra_args=extra)
                stop, err = _drive(drv, run, PROBE_PROMPT, timeout)
            except Exception as e:  # noqa: BLE001
                err = mask("%s: %s" % (type(e).__name__, e), secrets)
            text = "\n".join([err] + run.errors).strip()
            if stop and stop != "error" and not run.errors:
                quota.mark_ok(seat)
                rec["state"] = "ok"
            elif detect_quota(text):
                quota.mark_exhausted(seat, text, datetime.now())
                rec["state"] = "exhausted"
                rec["detail"] = "用量已滿：" + text[-120:]
            else:
                rec["state"] = "failed"
                rec["detail"] = run.detail(err, stop)[-300:]
            out.append(rec)
        snap = quota.snapshot()
        say("QuotaBook 狀態：")
        icons = {"ok": "✅", "exhausted": "⏳", "failed": "❌", "unavailable": "－"}
        for rec in out:
            s = rec["seat"]
            extra_txt = ""
            if snap.get(s, {}).get("state") == "exhausted":
                extra_txt = "（下次試探 %s）" % snap[s]["until"]
            say("  %s %-9s %-12s %s%s" % (icons[rec["state"]], s, rec["state"], rec["detail"], extra_txt))
        say()
        return out
    finally:
        cleanup_tmp(tmp)


# ---- 主程式 ----
def cert_line() -> str:
    try:
        env = with_cert_env(None)
        pem = env.get("SSL_CERT_FILE") or env.get("NODE_EXTRA_CA_CERTS")
        if pem:
            return "公司網路憑證：%s（已套用）" % pem
        user = netcerts.user_cert_file()
        if user:
            return "公司網路憑證：使用你自己設定的 %s（已套用）" % user
        why = "非 Windows 或憑證存放區沒有可匯出的憑證" if not hasattr(__import__("ssl"), "enum_certificates") \
            else "憑證匯出失敗（存放區讀不到或無法寫入 %s）" % netcerts.bundle_path()
        return "公司網路憑證：未套用（%s）" % why
    except Exception as e:  # noqa: BLE001
        return "公司網路憑證：未套用（匯出失敗：%s）" % type(e).__name__


def _mask_obj(obj: Any, secrets: List[str]) -> Any:
    if isinstance(obj, str):
        return mask(obj, secrets)
    if isinstance(obj, list):
        return [_mask_obj(x, secrets) for x in obj]
    if isinstance(obj, dict):
        return {k: _mask_obj(v, secrets) for k, v in obj.items()}
    return obj


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    secrets = []  # type: List[str]
    try:
        return _main(argv, secrets)
    except KeyboardInterrupt:
        say("\n已中斷")
        return 130
    except Exception as e:  # noqa: BLE001
        say("❌ 測試工具本身發生錯誤：%s: %s" % (type(e).__name__, mask(str(e), secrets)))
        return 1


def _main(argv: Optional[List[str]], secrets: List[str]) -> int:
    ap = argparse.ArgumentParser(description="辦公室冒煙測試（真 CLI）")
    ap.add_argument("--only", choices=sorted(CLI_SPECS), help="只測這個 CLI")
    ap.add_argument("--sandbox", default=os.path.join(os.path.dirname(BASE_DIR), "ai-team-sandbox"),
                    help="必須是空資料夾，或含 .ai-team-sandbox 標記檔")
    ap.add_argument("--seat", choices=list(GLM_SEATS), help="測 GLM 座位（用暫存 git repo）")
    ap.add_argument("--probe-all", action="store_true", help="對每個座位做用量試探")
    ap.add_argument("--timeout", type=float, default=TIMEOUT, help="每個 CLI 的逾時秒數（預設 300）")
    ap.add_argument("--litellm", default=os.path.join(BASE_DIR, "litellm.local.json"))
    ap.add_argument("--out-dir", default=os.path.join(BASE_DIR, "recon"))
    ap.add_argument("--cli-override", action="append", default=[], metavar="名稱=指令",
                    help="（測試用）以指令取代該 CLI 的啟動方式，可重複")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        overrides = dict(parse_override(s) for s in a.cli_override)
    except ValueError as e:
        say("❌ %s" % e)
        return 1

    cfg = load_model_config(a.litellm)
    if cfg:
        secrets.extend([cfg.glm_key, cfg.decision1_key])
    say("辦公室冒煙測試 %s" % datetime_now())
    say("模型設定：%s" % ("已讀取" if cfg else "讀不到（edit 類權限一律允許、execute 類一律拒絕；GLM 座位不可用）"))
    say(cert_line())
    say()

    clis = dict(discover())
    for name, cmd in overrides.items():
        if name not in CLI_SPECS:
            say("❌ --cli-override 不認得 %s（可用：%s）" % (name, "、".join(CLI_SPECS)))
            return 1
        label, _exe, proto = CLI_SPECS[name]
        clis[name] = CliInfo(name, label, cmd[0], proto)

    results = []  # type: List[Dict[str, Any]]
    skipped = []  # type: List[str]
    probe = None  # type: Optional[List[Dict[str, str]]]

    if a.seat:
        results.append(run_glm(a.seat, clis, overrides, cfg, a.timeout, secrets))
    if a.probe_all:
        probe = probe_all(clis, overrides, cfg, min(a.timeout, PROBE_TIMEOUT), secrets)
    if (not a.seat and not a.probe_all) or a.only:
        names = [a.only] if a.only else list(CLI_SPECS)
        todo = []
        for n in names:
            if n in clis:
                todo.append(n)
            elif a.only:
                r = _blank(n, CLI_SPECS[n][0])
                r["reason"], r["hint"] = "找不到 CLI", INSTALL_HINT[n]
                say("== %s ==\n  結果：❌ 失敗：%s\n" % (CLI_SPECS[n][0], INSTALL_HINT[n]))
                results.append(r)
            else:
                skipped.append(n)
                say("－ 略過 %s：找不到（%s）" % (CLI_SPECS[n][0], INSTALL_HINT[n]))
        if todo:
            err = ensure_repo(a.sandbox)
            if err:
                say("❌ %s" % err)
                r = _blank("sandbox", "sandbox")
                r["reason"], r["hint"] = err, "用 --sandbox 指定一個空資料夾"
                results.append(r)
            else:
                say("sandbox：%s\n" % a.sandbox)
                router = ModelRouter(cfg) if cfg else None
                deny = _deny_patterns()
                for n in todo:
                    results.append(run_cli(n, clis[n], overrides.get(n), a.sandbox, router, deny,
                                           a.timeout, secrets))
        elif not a.only and not results:
            say("❌ 一個 CLI 都找不到，沒有東西可測")
            r = _blank("none", "無")
            r["reason"], r["hint"] = "找不到任何 CLI", "安裝 kiro-cli／agy／claude 其中之一"
            results.append(r)

    probe_failed = bool(probe) and any(p["state"] == "failed" for p in probe)
    ok = all(r.get("ok") for r in results) and not probe_failed
    say("== 總結 ==")
    for r in results:
        say("  %s %s" % ("✅" if r.get("ok") else "❌", r.get("label") or r.get("cli")))
    for n in skipped:
        say("  － %s（找不到，略過）" % CLI_SPECS[n][0])
    if probe_failed:
        say("  ❌ 用量試探有座位失敗（見上方 QuotaBook 狀態）")
    say("全部成功" if ok else "有失敗的項目，請看上面的原因與建議")

    try:
        os.makedirs(a.out_dir, exist_ok=True)
        path = os.path.join(a.out_dir, "team-smoke-%s.json" % datetime_now("%Y%m%d-%H%M%S"))
        payload = {"time": datetime_now(), "sandbox": a.sandbox, "ok": ok, "results": results,
                   "skipped": skipped}
        if probe is not None:
            payload["probe"] = probe
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_mask_obj(payload, secrets), f, ensure_ascii=False, indent=2)
        say("結果已存到：%s" % path)
    except OSError as e:
        say("（結果檔寫入失敗：%s）" % e)
    return 0 if ok else 1


def datetime_now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.datetime.now().strftime(fmt)


if __name__ == "__main__":
    sys.exit(main())
