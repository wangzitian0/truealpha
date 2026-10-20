"""The doc-drift verdict finds each kind of drift that #1410 measured, and nothing else.

On 2026-10-20 init.md cited 17 `libs/` paths after #1373 moved `libs/` to `common/`, named two
`mart` objects that no migration creates, and held five status stamps. Each test below gives
the checker one such defect on a synthetic tree and asserts the exact finding. Each test fails
when its detection is removed. The last tests run the CLI on a real git repository, so the
`git ls-tree` and `git show` path is covered too. No database is needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from truealpha_tools import doc_drift
from truealpha_tools.doc_drift import Finding, check, mart_objects

FILES = (
    "init.md",
    "vision.md",
    "README.md",
    "common/factors/src/factors/base/peg.py",
    "apps/data-engine/src/data_engine/datahub/share_basis.py",
    ".github/CODEOWNERS",
    "db/migrations/0001_schemas.sql",
)
MIGRATIONS = (("0001_schemas.sql", "create table if not exists mart.served_head (id int);"),)


def findings(text: str, files: tuple[str, ...] = FILES, migrations=MIGRATIONS) -> list[Finding]:
    return list(check({"init.md": text}, files, migrations).findings)


def test_a_path_under_a_removed_root_is_a_finding() -> None:
    assert findings("See `libs/factors` and `common/factors`.") == [Finding("init.md", 1, "path", "libs/factors")]


def test_a_tracked_file_a_tracked_directory_and_a_package_tail_resolve() -> None:
    text = (
        "`common/factors/src/factors/base/peg.py` `common/factors` `common/factors/` "
        "`datahub/share_basis.py` `.github/CODEOWNERS` [x](README.md) [y](common/factors)"
    )
    report = check({"init.md": text}, FILES, MIGRATIONS)
    assert report.findings == ()
    assert report.paths_checked == 6  # `README.md` has no slash, so it is not a path citation


def test_a_tail_must_end_at_a_segment_boundary() -> None:
    # `ub/share_basis.py` is a string suffix of a tracked path, but not a tail at a `/`.
    assert findings("`ub/share_basis.py` `datahub/share_basi`") == [
        Finding("init.md", 1, "path", "ub/share_basis.py"),
        Finding("init.md", 1, "path", "datahub/share_basi"),
    ]


def test_a_missing_markdown_link_target_is_a_finding() -> None:
    assert findings("[the gate](docs/gone.md)") == [Finding("init.md", 1, "path", "docs/gone.md")]


@pytest.mark.parametrize(
    "token",
    [
        "/api/health",
        "https://github.com/wangzitian0/infra2",
        "apps/app-web/src/server/mart/*.ts",
        "ghcr.io/wangzitian0/truealpha-app",
        "a b/c",
        "staging",
        "factors.base.peg",
    ],
)
def test_a_token_that_is_not_a_repository_path_is_not_checked(token: str) -> None:
    report = check({"init.md": f"`{token}`"}, FILES, MIGRATIONS)
    assert report.findings == ()
    assert report.paths_checked == 0


def test_a_mart_object_that_no_migration_creates_is_a_finding() -> None:
    assert findings("`mart.served_head` and `mart.data_usage_frequency`") == [
        Finding("init.md", 1, "mart", "mart.data_usage_frequency")
    ]


def test_a_mart_column_reference_checks_its_object_and_a_wildcard_is_skipped() -> None:
    report = check({"init.md": "`mart.served_head.freshness` and `mart.strategy_*`"}, FILES, MIGRATIONS)
    assert report.findings == ()
    assert report.mart_checked == 1


def test_a_dropped_or_renamed_mart_object_no_longer_exists() -> None:
    chain = (
        (
            "0001_a.sql",
            "create table mart.kept (id int); create view mart.old_name as select 1; create table mart.gone (id int);",
        ),
        ("20261001T0000_b.sql", "alter view mart.old_name rename to new_name; drop table if exists mart.gone;"),
    )
    assert mart_objects(chain) == {"kept", "new_name"}
    assert findings("`mart.old_name` `mart.new_name` `mart.gone` `mart.kept`", migrations=chain) == [
        Finding("init.md", 1, "mart", "mart.old_name"),
        Finding("init.md", 1, "mart", "mart.gone"),
    ]


def test_the_chain_applies_in_filename_order() -> None:
    chain = (
        ("20261001T0000_b.sql", "drop view mart.late;"),
        ("0001_a.sql", "create view mart.late as select 1;"),
    )
    assert mart_objects(chain) == set()


def test_a_status_stamp_in_any_case_is_a_finding() -> None:
    text = "*Status 2026-09-15: computed.*\nBaseline as of 2026-09-15.\nSTATUS 2025 and As Of 2027."
    assert [(finding.line, finding.subject) for finding in findings(text)] == [
        (1, "Status 2026"),
        (2, "as of 2026"),
        (3, "STATUS 2025"),
        (3, "As Of 2027"),
    ]


def test_a_decision_date_is_not_a_stamp() -> None:
    assert findings("Owner decision 2026-10-20 (#1410). The status of a row is `stale`.") == []


def test_every_document_is_read() -> None:
    report = check({name: "`libs/x`" for name in doc_drift.DOCUMENTS}, FILES, MIGRATIONS)
    assert [finding.document for finding in report.findings] == list(doc_drift.DOCUMENTS)


def _repo(tmp_path: Path, init_text: str) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    (tmp_path / "init.md").write_text(init_text, encoding="utf-8")
    (tmp_path / "vision.md").write_text("Product scope.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Read `db/migrations` first.\n", encoding="utf-8")
    (tmp_path / "db" / "migrations").mkdir(parents=True)
    (tmp_path / "db" / "migrations" / "0001_schemas.sql").write_text(
        "create table mart.served_head (id int);\n", encoding="utf-8"
    )
    git("add", ".")
    git("commit", "-q", "-m", "fixture")
    return tmp_path


def test_the_cli_reads_the_revision_and_fails_on_a_finding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path, "`libs/factors` `mart.served_head` `mart.missing`\n*Status 2026-10-01*\n")
    assert doc_drift.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "init.md:1: path: 'libs/factors'" in out
    assert "init.md:1: mart: 'mart.missing'" in out
    assert "init.md:2: stamp: 'Status 2026'" in out
    assert "findings: 1 path, 1 mart, 1 stamp" in out


def test_the_cli_passes_on_a_clean_revision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path, "Schema: `db/migrations`, read `mart.served_head`.\n")
    assert doc_drift.main(["--root", str(root)]) == 0
    assert "findings: 0 path, 0 mart, 0 stamp" in capsys.readouterr().out


def test_the_cli_reads_the_committed_text_not_the_working_tree(tmp_path: Path) -> None:
    root = _repo(tmp_path, "`libs/factors`\n")
    (root / "init.md").write_text("`db/migrations`\n", encoding="utf-8")
    assert doc_drift.main(["--root", str(root), "--ref", "HEAD"]) == 1


def test_documents_that_cite_nothing_prove_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path, "No citation here.\n")
    (root / "README.md").write_text("No citation either.\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-am", "empty"], check=True, capture_output=True)
    assert doc_drift.main(["--root", str(root)]) == 1


def test_a_missing_document_or_revision_is_an_error(tmp_path: Path) -> None:
    root = _repo(tmp_path, "`db/migrations`\n")
    assert doc_drift.main(["--root", str(root), "--ref", "no-such-revision"]) == 2
    subprocess.run(["git", "-C", str(root), "rm", "-q", "vision.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "drop"], check=True, capture_output=True)
    assert doc_drift.main(["--root", str(root)]) == 2
