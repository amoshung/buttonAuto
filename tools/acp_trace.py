"""ACP 通訊逐行記錄（診斷用）：把 kiro-cli acp 的每一則 JSON-RPC 與 stderr 原樣寫進 recon\acp-trace-<時間>.log。

用法：python tools/acp_trace.py [--cli kiro|agy] [--variants default,v2,v3cli] [--timeout 120]
每個 variant 都會：initialize → (有 authMethods 就記錄) → session/new → 若要求登入則逐一試 authenticate →
session/prompt「請只回覆 OK」。代理反過來呼叫我們的方法都會記錄名稱；權限請求一律拒絕。不寫任何檔案到專案。
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from team.clis import discover  # noqa: E402

VARIANTS = {
    "default": [],
    "v2": ["--agent-engine", "v2"],
    "v3cli": ["--agent-engine", "v3", "--auth-method", "cli"],
    "v3client": ["--agent-engine", "v3", "--auth-method", "client"],
}


class Trace:
    def __init__(self, path: str):
        self.f = open(path, "a", encoding="utf-8")

    def __call__(self, tag: str, text: str) -> None:
        line = "%s %-6s %s" % (time.strftime("%H:%M:%S"), tag, text.rstrip())
        self.f.write(line + "\n")
        self.f.flush()
        print(line[:300])


def export_windows_ca(path: str) -> int:
    """把 Windows 憑證存放區（ROOT + CA，含公司 SSL 檢查用的根憑證）匯出成 PEM，給 Rust/Node 類 CLI 使用。"""
    import ssl
    n = 0
    seen = set()
    with open(path, "w", encoding="ascii") as f:
        for store in ("ROOT", "CA"):
            try:
                items = ssl.enum_certificates(store)  # type: ignore[attr-defined]
            except Exception:
                continue
            for der, enc, trust in items:
                if enc != "x509_asn" or der in seen:
                    continue
                seen.add(der)
                f.write(ssl.DER_cert_to_PEM_cert(der))
                n += 1
    return n


ENV: dict = {}


def run_variant(exe: str, name: str, extra, cwd: str, log: Trace, timeout: float) -> str:
    cmd = [exe, "acp"] + extra + ["-v", "-v"]
    log("====", "variant %s: %s" % (name, " ".join(cmd)))
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=flags, env=dict(os.environ, **ENV))
    except OSError as e:
        log("ERROR", "無法啟動：%s" % e)
        return "spawn-failed"
    msgs: "queue.Queue[dict]" = queue.Queue()

    def rd_out():
        for raw in iter(p.stdout.readline, b""):
            s = raw.decode("utf-8", "replace")
            log("<-", s)
            try:
                msgs.put(json.loads(s))
            except ValueError:
                pass

    def rd_err():
        for raw in iter(p.stderr.readline, b""):
            log("stderr", raw.decode("utf-8", "replace"))

    threading.Thread(target=rd_out, daemon=True).start()
    threading.Thread(target=rd_err, daemon=True).start()
    nid = [0]

    def send(obj):
        s = json.dumps(obj, ensure_ascii=False)
        log("->", s)
        try:
            p.stdin.write((s + "\n").encode("utf-8"))
            p.stdin.flush()
        except OSError as e:
            log("ERROR", "寫入失敗：%s" % e)

    def request(method, params, wait=60.0):
        nid[0] += 1
        rid = nid[0]
        send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end = time.time() + wait
        while time.time() < end:
            try:
                m = msgs.get(timeout=0.5)
            except queue.Empty:
                if p.poll() is not None:
                    log("ERROR", "代理結束 exit %s" % p.returncode)
                    return None
                continue
            if "method" in m and "id" in m:  # 代理呼叫我們
                meth = m.get("method")
                log("AGENT?", "代理要求 %s" % meth)
                if meth == "session/request_permission":
                    send({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                else:
                    send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported"}})
                continue
            if m.get("id") == rid:
                return m
        log("ERROR", "%s 在 %s 秒內沒有回應" % (method, wait))
        return None

    verdict = "?"
    try:
        r = request("initialize", {"protocolVersion": 1,
                                   "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                          "terminal": False},
                                   "clientInfo": {"name": "buttonAuto-trace", "version": "1.0"}})
        methods = ((r or {}).get("result") or {}).get("authMethods") or []
        log("NOTE", "authMethods = %s" % json.dumps(methods, ensure_ascii=False))
        r = request("session/new", {"cwd": cwd, "mcpServers": []})
        if r and "error" in r:
            for m in methods:
                mid = m.get("id") if isinstance(m, dict) else m
                if not mid or "login" in str(mid).lower() or "terminal" in str(mid).lower():
                    continue
                request("authenticate", {"methodId": mid})
            r = request("session/new", {"cwd": cwd, "mcpServers": []})
        sid = ((r or {}).get("result") or {}).get("sessionId")
        if not sid:
            return "no-session"
        r = request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "請只回覆 OK，不要做任何事。"}]},
                    wait=timeout)
        if r and "result" in r:
            verdict = "OK stopReason=%s" % r["result"].get("stopReason")
        elif r:
            verdict = "ERROR %s" % json.dumps(r.get("error"), ensure_ascii=False)
        else:
            verdict = "no-response"
        return verdict
    finally:
        log("====", "variant %s 結果：%s" % (name, verdict))
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                p.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", default="kiro")
    ap.add_argument("--variants", default="default,v2,v3cli")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--win-ca", action="store_true", help="匯出 Windows 根憑證並設 SSL_CERT_FILE 給 CLI")
    a = ap.parse_args()
    if a.win_ca:
        pem = os.path.join(ROOT, "recon", "windows-ca.pem")
        os.makedirs(os.path.dirname(pem), exist_ok=True)
        n = export_windows_ca(pem)
        ENV.update({"SSL_CERT_FILE": pem, "NODE_EXTRA_CA_CERTS": pem})
        print("已匯出 %d 張 Windows 憑證到 %s，並設定 SSL_CERT_FILE" % (n, pem))
    info = discover().get(a.cli)
    if not info:
        print("找不到 %s" % a.cli)
        return 1
    os.makedirs(os.path.join(ROOT, "recon"), exist_ok=True)
    path = os.path.join(ROOT, "recon", "acp-trace-%s-%s.log" % (a.cli, time.strftime("%Y%m%d-%H%M%S")))
    log = Trace(path)
    if ENV:
        log("NOTE", "env SSL_CERT_FILE=%s" % ENV.get("SSL_CERT_FILE"))
    cwd = tempfile.mkdtemp(prefix="act-")
    results = {}
    for v in a.variants.split(","):
        v = v.strip()
        extra = VARIANTS.get(v, []) if a.cli == "kiro" else []
        results[v] = run_variant(info.path, v, extra, cwd, log, a.timeout)
    print("\n== 總結 ==")
    for k, v in results.items():
        print("  %-9s %s" % (k, v))
    print("記錄檔：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
