"""專案掃描：progress.md 的目前任務、計畫／規格檔、git 狀態；Kiro／Antigravity 開過的資料夾。"""
import json
import os
import subprocess

from team.scan import known_projects, scan_project


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def make_repo(root):
    root.mkdir()
    git(root, "init", "-q")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    (root / "progress.md").write_text("# 進度\n\n## 已完成\n- 登入頁\n\n## 目前任務\n\n- 加入 CSV 匯出\n- 之後再說\n"
                                      "\n## 備註\n無\n", encoding="utf-8")
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "p.md").write_text("# 匯出功能計畫\n\n- [ ] CSV\n", encoding="utf-8")
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_scan_repo_progress_docs_and_git(tmp_path):
    r = make_repo(tmp_path / "proj 中文 @x")
    (r / "app.py").write_text("print(2)\n", encoding="utf-8")
    (r / ".kiro" / "specs" / "login").mkdir(parents=True)
    (r / ".kiro" / "specs" / "login" / "requirements.md").write_text("# 登入需求\n", encoding="utf-8")
    s = scan_project(str(r))
    assert s["problem"] == ""
    assert s["name"] == "proj 中文 @x" and s["path"] == str(r)
    assert s["branch"] == "main"
    assert any("app.py" in d for d in s["dirty"])
    assert s["progress"]["file"] == "progress.md"
    assert s["progress"]["task"] == "加入 CSV 匯出"
    assert s["progress"]["excerpt"].startswith("## 目前任務") and "備註" not in s["progress"]["excerpt"]
    rel = {d["relpath"].replace("\\", "/"): d for d in s["docs"]}
    assert rel["docs/plans/p.md"]["title"] == "匯出功能計畫"
    assert rel[".kiro/specs/login"]["title"] == "登入需求"
    assert all(d["modified"] for d in s["docs"])
    assert len(s["docs"]) <= 15


def test_scan_progress_fallbacks(tmp_path):
    r = tmp_path / "p"
    (r / "docs").mkdir(parents=True)
    (r / "docs" / "PROGRESS_2026.md").write_text("# 進度\n\n- [x] 一\n- [ ] 二號工作\n", encoding="utf-8")
    s = scan_project(str(r))
    assert s["progress"]["file"].replace("\\", "/") == "docs/PROGRESS_2026.md"
    assert s["progress"]["task"] == "二號工作"
    (r / "progress.md").write_text("目前任務：加入 mul\n", encoding="utf-8")
    assert scan_project(str(r))["progress"]["task"] == "加入 mul"  # 根目錄優先；也認得「目前任務：」這種寫法


def test_scan_not_a_repo(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    s = scan_project(str(d))
    assert s["problem"] and s["branch"] == "" and s["dirty"] == []
    assert s["progress"] == {"file": "", "task": "", "excerpt": ""}
    assert scan_project(str(tmp_path / "missing"))["problem"]


def _ws(appdata, app_dir, key, uri, mtime):
    d = appdata / app_dir / "User" / "workspaceStorage" / key
    d.mkdir(parents=True)
    (d / "workspace.json").write_text(json.dumps({"folder": uri}), encoding="utf-8")
    os.utime(d, (mtime, mtime))


def test_known_projects(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    appdata = tmp_path / "appdata"
    _ws(appdata, "Kiro", "k1", a.as_uri(), 1000)
    _ws(appdata, "Antigravity IDE", "g1", b.as_uri(), 2000)
    _ws(appdata, "Antigravity IDE", "g2", a.as_uri(), 500)  # 重複
    _ws(appdata, "Kiro", "k3", (tmp_path / "gone").as_uri(), 4000)  # 已不存在的資料夾略過
    _ws(appdata, "Kiro", "k2", "vscode-remote://ssh-remote+host/home/x/proj", 3000)  # 遠端略過
    out = known_projects([str(tmp_path / "extra")], appdata=str(appdata))
    paths = [os.path.normcase(p["path"]) for p in out]
    assert paths == [os.path.normcase(str(x)) for x in (tmp_path / "extra", b, a)]
    assert out[1]["source"] == "Antigravity" and out[2]["source"] == "Kiro"
    assert out[1]["name"] == "b"
    assert len(known_projects(appdata=str(appdata))) == 2


def test_progress_nested_headings_stay_in_section():
    from team.scan import progress_task
    text = ("# 進度\n\n## 目前任務\n\n### 細節\n- 加入 CSV 匯出\n\n#### 備忘\n- 編碼用 UTF-8\n\n"
            "## 已完成\n- 登入\n")
    info = progress_task(text)
    assert info["task"] == "加入 CSV 匯出"
    assert "### 細節" in info["excerpt"] and "編碼用 UTF-8" in info["excerpt"]
    assert "已完成" not in info["excerpt"]
