# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import json; from pathlib import Path; import pytest; from truealpha_runtime.testing import load_tool; REPO_ROOT = Path(__file__).resolve().parents[3]; _module = load_tool("issue_close_guard"); run = _module.run
def _timeline(*events: dict, pages: list[list[dict]] | None = None):
    chunks = pages if pages is not None else [list(events)]
    def gh_api(path: str) -> str: assert "/timeline" in path; page = int(path.rsplit("page=", 1)[1]); return json.dumps(chunks[page - 1] if page <= len(chunks) else [])
    return gh_api
def _git(contained: bool = False, code: int | None = None):
    def git(args): return (code if code is not None else (0 if contained else 1)), ""
    return git
def _serving(tag: str = "v0.0.19"):
    def http_get(url: str) -> tuple[int, str]: return 200, json.dumps({"status": "ok", "git_sha": tag})
    return http_get
def _capture() -> tuple[list[tuple[int, str]], object]: calls: list[tuple[int, str]] = []; return calls, lambda issue, reason: calls.append((issue, reason))
def test_a_merge_closing_an_unreleased_issue_is_reopened( capsys: pytest.CaptureFixture[str], ) -> None:
    calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline({"event": "closed", "commit_id": "1acd82c" + "0" * 33}), git=_git(), http_get=_serving(), reopen=reopen, ); assert exit_code == 0; assert len(calls) == 1 and calls[0][0] == 494; assert "staging is NOT serving" in calls[0][1]
    assert "#562" in calls[0][1], "the comment must name the mechanism, not just scold"
def test_the_real_close_event_shape_is_recognised() -> None: calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline( {"event": "cross-referenced", "created_at": "2026-08-14T10:34:19Z", "commit_id": None}, {"event": "closed", "created_at": "2026-08-14T10:45:28Z", "commit_id": None}, { "event": "referenced", "created_at": "2026-08-14T10:45:28Z", "commit_id": "3057e9704594df7613469e0dbdf3b22fad057a48", }, ), git=_git(), http_get=_serving(), reopen=reopen, ); assert exit_code == 0; assert len(calls) == 1, "an auto-close whose commit lives on a sibling event must be caught"; assert "3057e970" in calls[0][1]
def test_a_human_close_is_left_alone(capsys: pytest.CaptureFixture[str]) -> None: calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline({"event": "closed", "created_at": "2026-08-17T03:00:00Z"}), git=_git(), http_get=_serving(), reopen=reopen, ); assert exit_code == 0; assert calls == []; assert "closed by a person" in capsys.readouterr().out
def test_a_released_capability_stays_closed(capsys: pytest.CaptureFixture[str]) -> None: calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline({"event": "closed", "commit_id": "abc1234" + "0" * 33}), git=_git(contained=True), http_get=_serving("v0.0.20"), reopen=reopen, ); assert exit_code == 0; assert calls == []; assert "staging is serving" in capsys.readouterr().out
def test_the_latest_close_is_the_one_judged() -> None: calls, reopen = _capture(); run( 371, gh_api=_timeline( {"event": "closed"}, {"event": "reopened"}, {"event": "closed", "commit_id": "fca4ba8" + "0" * 33}, ), git=_git(), http_get=_serving(), reopen=reopen, ); assert len(calls) == 1, "an old hand close must not excuse a later auto close"
def test_an_unanswerable_history_fails_loudly(capsys: pytest.CaptureFixture[str]) -> None: calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline({"event": "closed", "commit_id": "abc1234" + "0" * 33}), git=_git(code=128), http_get=_serving(), reopen=reopen, ); assert exit_code == 1; assert calls == []; assert "fetch tags and full history" in capsys.readouterr().err
def test_a_tag_that_exists_but_is_not_deployed_is_not_evidence( capsys: pytest.CaptureFixture[str], ) -> None: calls, reopen = _capture(); run( 494, gh_api=_timeline( {"event": "closed", "created_at": "t", "commit_id": None}, {"event": "referenced", "created_at": "t", "commit_id": "3057e970" + "0" * 32}, ), git=_git(contained=False), http_get=_serving("v0.0.19"), reopen=reopen, ); assert len(calls) == 1; assert "serves v0.0.19" in calls[0][1]
def test_an_unreachable_production_fails_rather_than_guesses( capsys: pytest.CaptureFixture[str], ) -> None: calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline({"event": "closed", "created_at": "t", "commit_id": "a" * 40}), git=_git(), http_get=lambda _url: (503, "down"), reopen=reopen, ); assert exit_code == 1; assert calls == []; assert "HTTP 503" in capsys.readouterr().err
def test_the_latest_close_is_found_beyond_the_first_page() -> None: filler = [{"event": "commented", "created_at": f"t{i}"} for i in range(100)]; calls, reopen = _capture(); run( 494, gh_api=_timeline( pages=[ filler, [ {"event": "closed", "created_at": "late", "commit_id": None}, {"event": "referenced", "created_at": "late", "commit_id": "b" * 40}, ], ] ), git=_git(contained=False), http_get=_serving("v0.0.19"), reopen=reopen, ); assert len(calls) == 1, "the close on page 2 must be the one judged"
def test_non_object_health_json_is_a_verdict_failure_not_a_crash( capsys: pytest.CaptureFixture[str], ) -> None: calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline({"event": "closed", "created_at": "t", "commit_id": "a" * 40}), git=_git(), http_get=lambda _url: (200, json.dumps(["not", "an", "object"])), reopen=reopen, ); assert exit_code == 1; assert calls == []; assert "not an object" in capsys.readouterr().err
def test_a_release_identity_git_could_read_as_an_option_never_reaches_git( capsys: pytest.CaptureFixture[str], ) -> None:
    reached_git = False
    def git(args): nonlocal reached_git; reached_git = True; return 0, ""
    calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline({"event": "closed", "created_at": "t", "commit_id": "a" * 40}), git=git, http_get=_serving("--upload-pack=touch /tmp/x"), reopen=reopen, ); assert exit_code == 1; assert not reached_git; assert "not a usable release identifier" in capsys.readouterr().err
def test_a_non_product_commit_stays_closed_without_reopening( capsys: pytest.CaptureFixture[str], ) -> None:
    calls, reopen = _capture()
    def git(args):
        if args[0] == "merge-base": return 1, ""
        if args[0] == "diff-tree": return 0, "docs/index.md\nskills/ssot/SKILL.md\nAGENTS.md\n"
        return 0, ""
    exit_code = run( 1159, gh_api=_timeline({"event": "closed", "commit_id": "c0ffee1" + "0" * 33}), git=git, http_get=_serving("v0.0.19"), reopen=reopen, ); assert exit_code == 0; assert calls == []; out = capsys.readouterr().out
    assert "leaving #1159 closed" in out
    assert "only changes non-product paths" in out
def test_a_mixed_product_and_non_product_commit_is_reopened( capsys: pytest.CaptureFixture[str], ) -> None:
    calls, reopen = _capture()
    def git(args):
        if args[0] == "merge-base": return 1, ""
        if args[0] == "diff-tree": return 0, "docs/index.md\napps/app-web/src/app/page.tsx\n"
        return 0, ""
    exit_code = run( 1159, gh_api=_timeline({"event": "closed", "commit_id": "c0ffee2" + "0" * 33}), git=git, http_get=_serving("v0.0.19"), reopen=reopen, ); assert exit_code == 0; assert len(calls) == 1; assert calls[0][0] == 1159; assert "staging is NOT serving" in calls[0][1]
def test_a_commit_modifying_tests_and_e2e_stays_closed_without_reopening( capsys: pytest.CaptureFixture[str], ) -> None:
    calls, reopen = _capture()
    def git(args):
        if args[0] == "merge-base": return 1, ""
        if args[0] == "diff-tree": return 0, "apps/app-web/e2e/walk-tree.mjs\ncommon/runtime/tests/test_foo.py\n"
        return 0, ""
    exit_code = run( 1159, gh_api=_timeline({"event": "closed", "commit_id": "c0ffee3" + "0" * 33}), git=git, http_get=_serving("v0.0.19"), reopen=reopen, ); assert exit_code == 0; assert calls == []; out = capsys.readouterr().out
    assert "leaving #1159 closed" in out
    assert "only changes non-product paths" in out
def test_default_health_url_points_to_staging() -> None: assert _module.DEFAULT_HEALTH == "https://truealpha-staging.truealpha.club/api/health"; assert _module.DEFAULT_HEALTH == _module.STAGING_HEALTH; assert _module.PRODUCTION_HEALTH == "https://truealpha.club/api/health"; assert ( _module.resolve_app_hostname( "truealpha", "staging", base_domain="truealpha.club", custom_prod_domain="truealpha.club", ) in _module.STAGING_HEALTH )
def test_explicit_production_health_url_targets_production( capsys: pytest.CaptureFixture[str], ) -> None: calls, reopen = _capture(); exit_code = run( 494, gh_api=_timeline({"event": "closed", "commit_id": "1acd82c" + "0" * 33}), git=_git(), health_url=_module.PRODUCTION_HEALTH, http_get=_serving(), reopen=reopen, ); assert exit_code == 0; assert len(calls) == 1 and calls[0][0] == 494; assert "production is NOT serving" in calls[0][1]
def test_cli_parser_defaults_to_staging_health_and_accepts_custom_url() -> None: parser = _module._parser(); args_default = parser.parse_args(["--issue", "494"]); assert args_default.health_url == _module.STAGING_HEALTH; args_custom = parser.parse_args(["--issue", "494", "--health-url", "https://example.com/health"]); assert args_custom.health_url == "https://example.com/health"
