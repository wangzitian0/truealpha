"""Ensure .claude/skills/ stays byte-identical to .agents/skills/."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _tree_bytes(base: Path) -> dict[str, bytes]:
    return {
        p.relative_to(base).as_posix(): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def test_claude_skills_mirror_agents_skills() -> None:
    """Keep .claude/skills/ synchronized with .agents/skills/ for Claude Code discovery."""
    agents_skills = ROOT / ".agents" / "skills"
    claude_skills = ROOT / ".claude" / "skills"
    assert agents_skills.is_dir() and claude_skills.is_dir()
    assert _tree_bytes(claude_skills) == _tree_bytes(agents_skills)
