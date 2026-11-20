import os

from team import ce_skills, drivers, personas


def test_ensure_ce_installed_syncs_all_required_skills_across_clis(tmp_path):
    # Create a mock local canonical source with ce-plan and ce-code-review
    canonical_src = tmp_path / "canonical_skills"
    for name in ("ce-plan", "ce-code-review"):
        sdir = canonical_src / name
        sdir.mkdir(parents=True)
        (sdir / "SKILL.md").write_text(f"# {name}\nCanonical skill content", encoding="utf-8")

    target_root = tmp_path / "provisioned"
    res = ce_skills.ensure_ce_installed(
        plugin_dirs=[str(canonical_src)],
        target_root=str(target_root),
    )
    assert res["ok"] is True
    assert set(res["skills"]) == set(ce_skills.REQUIRED_CE_SKILLS)

    # Verify every required skill exists in AGY, Kiro, and Claude plugin directories
    for skill in ce_skills.REQUIRED_CE_SKILLS:
        assert os.path.isfile(os.path.join(res["agy_skills_dir"], skill, "SKILL.md"))
        assert os.path.isfile(os.path.join(res["kiro_skills_dir"], skill, "SKILL.md"))
        assert os.path.isfile(os.path.join(res["claude_plugin_dir"], "skills", skill, "SKILL.md"))


def test_role_ce_directive_maps_all_seven_roles_and_subfolder_scope():
    all_roles = ("po", "tech_lead", "scrum", "implementer", "fixer", "qa", "reviewer")
    for role in all_roles:
        directive = ce_skills.role_ce_directive(
            role,
            notes_dir="D:/A/B/.ai-team-worktrees/B-slug.notes",
            reference_root="D:/A",
            target_subdir="B",
        )
        assert "【Compound-Engineering 角色專屬技能規範" in directive
        for skill in ce_skills.ROLE_CE_MAP[role]["skills"]:
            assert f"`{skill}`" in directive
        assert "Decision-1" in directive
        assert "GLM-5.3" in directive
        assert "D:/A/B/.ai-team-worktrees/B-slug.notes" in directive
        assert "D:/A" in directive
        assert "唯讀參考資料來源" in directive


def test_composer_injects_role_ce_directive_for_all_roles(tmp_path):
    comp = personas.Composer(
        notes_dir="/tmp/room-notes",
        reference_root="D:/WorkspaceA",
        target_subdir="ServiceB",
    )
    for role in ("po", "tech_lead", "scrum", "implementer", "fixer", "qa", "reviewer"):
        p = comp.preview(role)
        assert "【Compound-Engineering 角色專屬技能規範" in p.text
        for skill in ce_skills.ROLE_CE_MAP[role]["skills"]:
            assert f"`{skill}`" in p.text
        assert "D:/WorkspaceA" in p.text
        assert "ServiceB" in p.text
