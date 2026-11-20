import threading
from team.kb import KnowledgeBase
from team.models import ModelConfig


class FakeRag:
    def __init__(self, kb_dir, kb):
        self.kb_dir = kb_dir
        self.inserted = []
        self.queries = []
        self.threads = []

    def insert(self, doc):
        if doc:
            self.inserted.append(doc)
            self.threads.append(threading.get_ident())

    def query(self, q, **kw):
        self.queries.append(q)
        return f"知識命中：secret-sk-live1234567890 -> {q}"


def _cfg():
    return ModelConfig(
        "http://glm.local",
        "k1",
        "k2",
        sovereign_base_url="http://172.16.249.171:31305",
        sovereign_key="sov-secret-1234567890",
    )


def test_kb_tags_dedups_masks_and_single_writer(tmp_path):
    rag_holder = {}

    def factory(kb_dir, kb):
        rag_holder["rag"] = FakeRag(kb_dir, kb)
        return rag_holder["rag"]

    kb = KnowledgeBase(str(tmp_path), config=_cfg(), secrets=["sk-live1234567890"], rag_factory=factory)
    try:
        repo = str(tmp_path / "projA")
        assert kb.record(repo, "room-1", "test", "用 sk-live1234567890 失敗", fingerprint="fp1", wait=True) is True
        # 同專案同指紋去重
        assert kb.record(repo, "room-2", "test", "重複內容", fingerprint="fp1", wait=True) is False
        # 同指紋但不同專案允許寫入
        repo_b = str(tmp_path / "projB")
        assert kb.record(repo_b, "room-3", "test", "不同專案同指紋", fingerprint="fp1", wait=True) is True
        rag = rag_holder["rag"]
        assert len(rag.inserted) == 2
        assert "[room:room-1]" in rag.inserted[0]
        assert "sk-live1234567890" not in rag.inserted[0]
        assert len(set(rag.threads)) == 1  # 單一寫入執行緒

        ans = kb.query(repo, "查 sk-live1234567890 怎麼解", room_id="room-1")
        assert "sk-live1234567890" not in ans
        assert "[project:" in rag_holder["rag"].queries[0]
    finally:
        kb.close()


def test_kb_embed_and_rerank_call_sovereign_ai(tmp_path):
    seen = []

    def fake_post(url, key, payload, timeout):
        seen.append((url, key, payload))
        if url.endswith("/v1/embeddings"):
            return {"data": [{"index": 0, "embedding": [0.1] * 2560}]}
        return {"results": [{"index": 0, "relevance_score": 0.9}]}

    kb = KnowledgeBase(str(tmp_path), config=_cfg(), post_fn=fake_post)
    try:
        vecs = kb.embed(["hello"])
        assert len(vecs) == 1 and len(vecs[0]) == 2560
        rr = kb.rerank("q", ["doc1"], top_n=1)
        assert rr[0]["relevance_score"] == 0.9
        assert seen[0][0] == "http://172.16.249.171:31305/v1/embeddings"
        assert seen[0][2]["model"] == "qwen3-embedding"
        assert seen[1][0] == "http://172.16.249.171:31305/v1/rerank"
        assert seen[1][2]["model"] == "bge-reranker-v2-m3"
    finally:
        kb.close()


def test_kb_offline_falls_back_to_lessonbook(tmp_path):
    kb = KnowledgeBase(str(tmp_path), config=None)
    try:
        repo = str(tmp_path / "projC")
        assert kb.record(repo, "r1", "review", "務必先跑 pytest 再提交", fingerprint="fp-off", wait=True) is True
        out = kb.query(repo, "提交前要注意什麼？")
        assert "務必先跑 pytest 再提交" in out
    finally:
        kb.close()


def test_llm_wiki_pages_index_and_ce_sync(tmp_path):
    from team import ce_sync
    repo = tmp_path / "projWiki"
    plans_dir = repo / "docs" / "plans"
    sols_dir = repo / "docs" / "solutions"
    plans_dir.mkdir(parents=True)
    sols_dir.mkdir(parents=True)
    plan_file = plans_dir / "2026-11-18-feat-wiki-plan.md"
    plan_file.write_text(
        "---\ntitle: Wiki Rebuild Plan\ndate: 2026-11-18\n---\n\n"
        "## Goal Capsule\n- **Objective:** 將知識庫全面升級為雙向連結 LLM Wiki\n\n"
        "## Implementation Units\n"
        "### U1: 建立 LLMWikiEngine 與 index.md\n"
        "### U2: 整合房間封存與接續開發 Handoff\n\n"
        "Verification: `pytest tests/team/test_kb.py -q`\n",
        encoding="utf-8",
    )
    sol_file = sols_dir / "wiki-backlinks.md"
    sol_file.write_text("# Wiki Backlinks\n使用 [[plans/wiki-rebuild]] 串聯上下文。", encoding="utf-8")

    ce_info = ce_sync.sync_room_ce_plan(str(repo))
    assert ce_info["has_plan"] is True
    assert "雙向連結 LLM Wiki" in ce_info["objective"]
    assert len(ce_info["criteria"]) == 2
    assert ce_info["criteria"][0]["id"] == "U1"
    assert ce_info["test_cmd"] == "pytest tests/team/test_kb.py -q"

    kb = KnowledgeBase(str(tmp_path / "sessions"), config=None)
    try:
        v = kb.wiki_view(str(repo))
        assert len(v["pages"]) >= 2
        assert "專案 LLM Wiki 知識總覽" in v["index_md"]
        ans = kb.query(str(repo), "Wiki Backlinks 雙向連結")
        assert "LLM Wiki 檢索結果" in ans
    finally:
        kb.close()

