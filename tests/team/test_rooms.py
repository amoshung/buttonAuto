import json
import os

import pytest

from team import rooms
from team.rooms import RoomStore, Room


def mk(store, **kw):
    base = dict(name="房間", repo="/r", slug="s", goal="目標", source="manual",
                impl_cli="claude", review_cli="kiro", autonomy="balanced",
                allowed_paths=["src"])
    base.update(kw)
    return store.create(**base)


def test_create_get_list_order(tmp_path):
    s = RoomStore(str(tmp_path))
    a = mk(s, name="甲")
    b = mk(s, name="乙")
    assert a.id.startswith("r") and len(a.id) == 9
    assert (a.order, b.order) == (1, 2)
    assert [r.id for r in s.list()] == [a.id, b.id]
    assert s.get(a.id).name == "甲"
    assert s.get("nope") is None
    s.update(a.id, lambda r: setattr(r, "status", "archived"))
    assert [r.id for r in s.list()] == [b.id]
    assert len(s.list(include_archived=True)) == 2


def test_chinese_roundtrip(tmp_path):
    s = RoomStore(str(tmp_path))
    r = mk(s, goal="修正「登入」錯誤", persona="嚴謹")
    got = RoomStore(str(tmp_path)).get(r.id)
    assert got.goal == "修正「登入」錯誤" and got.persona == "嚴謹"
    raw = open(os.path.join(str(tmp_path), r.id, "room.json"), encoding="utf-8").read()
    assert "登入" in raw


def test_update_and_atomic(tmp_path, monkeypatch):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    r2 = s.update(r.id, lambda x: setattr(x, "phase", "spec"))
    assert r2.phase == "spec" and r2.updated_at

    def boom(x):
        x.phase = "build"
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        s.update(r.id, boom)
    assert s.get(r.id).phase == "spec"

    def bad_replace(a, b):
        raise OSError("disk")
    monkeypatch.setattr(os, "replace", bad_replace)
    with pytest.raises(OSError):
        s.update(r.id, lambda x: setattr(x, "phase", "test"))
    monkeypatch.undo()
    assert s.get(r.id).phase == "spec"
    json.load(open(os.path.join(str(tmp_path), r.id, "room.json"), encoding="utf-8"))


def test_replace_retries_permission_error(tmp_path, monkeypatch):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("busy")
        return real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(rooms.time, "sleep", lambda t: None)
    s.update(r.id, lambda x: setattr(x, "phase", "spec"))
    assert calls["n"] == 3


def test_old_file_and_unknown_keys(tmp_path):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    p = os.path.join(str(tmp_path), r.id, "room.json")
    d = json.load(open(p, encoding="utf-8"))
    for k in ("impl_seat", "impl_model", "review_seat", "review_model", "fallback_from",
              "user_pinned", "context_pct", "context_estimated", "rollovers",
              "target_branch", "unverified"):
        d.pop(k)
    d["future_field"] = 1
    json.dump(d, open(p, "w", encoding="utf-8"))
    g = s.get(r.id)
    assert g.impl_seat == "" and g.user_pinned is False and g.context_pct == 0.0
    assert g.rollovers == 0 and g.target_branch == "" and g.unverified == ""


def test_events(tmp_path):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    for i in range(5):
        s.event(r.id, "msg", "第%d" % i, n=i)
    ev = s.events(r.id, limit=3)
    assert [e["text"] for e in ev] == ["第4", "第3", "第2"]
    assert ev[0]["kind"] == "msg" and ev[0]["n"] == 4
    with open(os.path.join(str(tmp_path), r.id, "events.jsonl"), "a", encoding="utf-8") as f:
        f.write('{"kind": "x", "te')
    assert len(s.events(r.id)) == 5
    assert s.events("nope") == []


def test_inbox(tmp_path):
    s = RoomStore(str(tmp_path))
    a, b = mk(s), mk(s)
    i1 = s.add_inbox(a.id, "question", "問", "細節", [{"id": "y", "name": "是"}])
    i2 = s.add_inbox(b.id, "unverified", "無法驗證", "d", [])
    assert [i.id for i in s.inbox()] == [i1.id, i2.id]
    out = s.resolve(i1.id, "y", "好")
    assert out.resolved and out.answer == "y" and out.text == "好"
    assert [i.id for i in s.inbox()] == [i2.id]
    assert len(s.inbox(include_resolved=True)) == 2
    assert s.resolve("i00000000", "x") is None
    with pytest.raises(ValueError):
        s.resolve("zzz", "x")
    assert s.resolve(i1.id, "n") .answer == "y"  # 已解決不覆寫


def test_recover_after_restart(tmp_path):
    s = RoomStore(str(tmp_path))
    a, b, c = mk(s), mk(s), mk(s)
    s.update(a.id, lambda r: setattr(r, "status", "running"))
    s.update(b.id, lambda r: setattr(r, "status", "waiting_user"))
    p = s.add_inbox(a.id, "permission", "許可", "d", [])
    q = s.add_inbox(a.id, "question", "問", "d", [])
    ids = s.recover_after_restart()
    assert sorted(ids) == sorted([a.id, b.id])
    assert s.get(a.id).status == "paused"
    assert "按繼續接著做" in s.get(a.id).activity
    assert s.get(c.id).status == "queued"
    left = s.inbox()
    assert [i.id for i in left] == [q.id]
    done = [i for i in s.inbox(include_resolved=True) if i.id == p.id][0]
    assert done.answer == "reject" and done.text == "後端重啟，請求已失效"


def test_events_unicode_line_separators(tmp_path):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    s.event(r.id, "m", "a\u2028b\u2029c\x85d")
    s.event(r.id, "m", "next")
    ev = s.events(r.id)
    assert [e["text"] for e in ev] == ["next", "a\u2028b\u2029c\x85d"]
    # json.dumps(ensure_ascii=False) 輸出原始字元
    raw = open(os.path.join(str(tmp_path), r.id, "events.jsonl"), encoding="utf-8").read()
    assert "\u2028" in raw and "\x85" in raw


def test_event_after_partial_line(tmp_path):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    s.event(r.id, "m", "before")
    with open(os.path.join(str(tmp_path), r.id, "events.jsonl"), "a", encoding="utf-8") as f:
        f.write('{"kind": "x", "te')
    s.event(r.id, "m", "after-crash")
    assert [e["text"] for e in s.events(r.id)] == ["after-crash", "before"]


def test_events_tail_read_many_and_big(tmp_path):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    for i in range(300):
        s.event(r.id, "m", "x" * 200 + str(i))
    ev = s.events(r.id, limit=5)
    assert [e["text"][-3:] for e in ev] == ["299", "298", "297", "296", "295"]
    assert len(s.events(r.id, limit=1000)) == 300


def test_id_validation_and_unknown_room(tmp_path):
    s = RoomStore(str(tmp_path))
    for bad in ("../x", os.path.abspath(str(tmp_path)), "nope"):
        with pytest.raises(ValueError):
            s.event(bad, "m", "t")
        with pytest.raises(ValueError):
            s.update(bad, lambda r: None)
        assert s.get(bad) is None and s.events(bad) == []
    before = set(os.listdir(str(tmp_path)))
    with pytest.raises(KeyError):
        s.event("r00000000", "m", "t")
    with pytest.raises(KeyError):
        s.add_inbox("r00000000", "question", "t", "d", [])
    assert set(os.listdir(str(tmp_path))) == before
    assert not os.path.exists(os.path.join(os.path.dirname(str(tmp_path)), "x"))


def test_update_cannot_change_id(tmp_path):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    out = s.update(r.id, lambda x: setattr(x, "id", "r../evil"))
    assert out.id == r.id and s.get(r.id).id == r.id


def test_write_cleans_tmp_when_dump_fails(tmp_path):
    s = RoomStore(str(tmp_path))
    r = mk(s)
    with pytest.raises(TypeError):
        s.update(r.id, lambda x: setattr(x, "criteria", [object()]))
    assert not [n for n in os.listdir(os.path.join(str(tmp_path), r.id)) if n.endswith(".tmp")]
    assert s.get(r.id).criteria == []


def test_recover_writes_event(tmp_path):
    s = RoomStore(str(tmp_path))
    a = mk(s)
    s.update(a.id, lambda r: setattr(r, "status", "running"))
    s.recover_after_restart()
    assert s.events(a.id)[0]["text"] == "後端重新啟動，已暫停"


def test_list_and_inbox_ignore_other_files_in_root(tmp_path):
    s = RoomStore(str(tmp_path))
    a = mk(s)
    (tmp_path / "quota.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    assert [r.id for r in s.list()] == [a.id]
    it = s.add_inbox(a.id, "question", "q", "d", [])
    assert [i.id for i in s.inbox()] == [it.id]
