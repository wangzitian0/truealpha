"""A tag re-tags what main already published at its SHA; what main did not publish still builds — #860.

Every tag run rebuilt three images from a warm cache (3 s each) and re-published all of them
(~1 min each) for a SHA whose main run had already published them: 2.2 min producing a SECOND
digest of the same bytes, and moving main's own `sha-<short>` pointer onto it in passing.
`tools/verified_sha_images.py` asks the registry which images main published at this exact
SHA, so release-images re-tags those digests and builds only the rest.

The registry's three answers are the whole contract, so each is driven through a fake
transport the way test_health_check.py drives the SDK's resolver: a digest (re-tag), no such
tag (build -- #731 left that image unpublished), and anything else (fail, never rebuild
quietly). The anonymous-token round trip is asserted request by request because a wrong
scope or a missing Bearer is a 401 that reads as "not found" in a less careful client.
"""

from __future__ import annotations

import io
import json

import pytest
from truealpha_runtime.testing import load_tool

SHA = "7de78132d644fa3b270cc847a47c0fc4ba2be1db"
DIGEST = "sha256:" + "a" * 64
PLAN = [
    {"image": "truealpha-app-web", "dockerfile": "apps/app-web/Dockerfile", "context": "apps/app-web"},
    {"image": "truealpha-llm-service", "dockerfile": "apps/llm-service/Dockerfile", "context": "."},
    {"image": "truealpha-data-engine", "dockerfile": "apps/data-engine/Dockerfile", "context": "."},
]
CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:wangzitian0/{image}:pull"'


def _registry(answers: dict[str, list[tuple[int, dict[str, str]]]], seen: list[tuple[str, str, dict[str, str]]]):
    """A GHCR that challenges every unauthenticated manifest read, then answers per image
    from `answers` (a queue per image, so a transient failure can precede a real answer)."""
    tool = load_tool("verified_sha_images")

    def transport(method: str, url: str, headers: dict[str, str]) -> tool.Response:
        seen.append((method, url, dict(headers)))
        if url.startswith("https://ghcr.io/token?"):
            return tool.Response(200, {}, b'{"token": "anon"}')
        image = url.split("/v2/wangzitian0/", 1)[1].split("/manifests/", 1)[0]
        if "Authorization" not in headers:
            return tool.Response(401, {"Www-Authenticate": CHALLENGE.format(image=image)})
        status, response_headers = answers[image].pop(0)
        return tool.Response(status, response_headers)

    return transport


def test_an_image_main_published_at_this_sha_is_retagged_at_its_digest() -> None:
    tool = load_tool("verified_sha_images")
    seen: list[tuple[str, str, dict[str, str]]] = []
    registry = _registry({"truealpha-data-engine": [(200, {"Docker-Content-Digest": DIGEST})]}, seen)

    result = tool.split([PLAN[2]], owner="wangzitian0", sha=SHA, transport=registry)

    assert result == {
        "retag": [
            {
                "image": "truealpha-data-engine",
                "source": "ghcr.io/wangzitian0/truealpha-data-engine:sha-7de7813",
                "digest": DIGEST,
            }
        ],
        "build": [],
    }
    anonymous, token, authorised = seen
    assert anonymous[0] == "HEAD" and anonymous[1].endswith(
        "/v2/wangzitian0/truealpha-data-engine/manifests/sha-7de7813"
    )
    assert "application/vnd.oci.image.index.v1+json" in anonymous[2]["Accept"], (
        "without the index media type the registry answers with a per-platform manifest and a different digest"
    )
    assert token[0] == "GET" and token[1].endswith("scope=repository%3Awangzitian0%2Ftruealpha-data-engine%3Apull")
    assert authorised[0] == "HEAD" and authorised[2]["Authorization"] == "Bearer anon"


def test_an_image_main_did_not_publish_at_this_sha_builds_exactly_as_planned() -> None:
    """#731: a main push publishes only the images whose inputs the merge touched, so an
    untouched image has no `sha-<short>` at this SHA. It builds -- and the plan entry passes
    through whole, dockerfile and context included, because the build matrix reads them."""
    tool = load_tool("verified_sha_images")
    seen: list[tuple[str, str, dict[str, str]]] = []
    registry = _registry(
        {
            "truealpha-app-web": [(404, {})],
            "truealpha-llm-service": [(200, {"docker-content-digest": DIGEST})],
            "truealpha-data-engine": [(200, {"docker-content-digest": DIGEST})],
        },
        seen,
    )

    result = tool.split(PLAN, owner="wangzitian0", sha=SHA, transport=registry)

    assert result["build"] == [PLAN[0]]
    assert [entry["image"] for entry in result["retag"]] == ["truealpha-llm-service", "truealpha-data-engine"]
    # Never `latest`: by tag time it may already be a later merge's image. The only reference
    # ever asked about is the exact-SHA one.
    manifests = [url for method, url, _ in seen if "/manifests/" in url]
    assert manifests and all(url.endswith("/manifests/sha-7de7813") for url in manifests), manifests


def test_the_short_sha_is_metadata_actions_spelling() -> None:
    """`type=sha,format=short` is seven characters; a longer or shorter reference names a tag
    main never pushed and every image would "need building" -- the optimisation silently off."""
    tool = load_tool("verified_sha_images")
    assert tool.short_sha(SHA) == "7de7813"
    assert tool.SHORT_SHA_LENGTH == 7
    for junk in ("7de7813", SHA.upper(), "v0.0.63", ""):
        with pytest.raises(ValueError, match="full 40-hex commit sha"):
            tool.short_sha(junk)


def test_a_refused_registry_fails_instead_of_rebuilding_quietly() -> None:
    """403 is not 404. Treating it as "not published" would rebuild everything and stay green
    -- the optimisation measured as working while doing nothing, which is the failure mode
    this repository keeps finding (#645, #560)."""
    tool = load_tool("verified_sha_images")
    registry = _registry({"truealpha-data-engine": [(403, {})]}, [])
    with pytest.raises(tool.RegistryError, match="status 403"):
        tool.split([PLAN[2]], owner="wangzitian0", sha=SHA, transport=registry)


def test_an_answer_without_a_usable_digest_is_refused() -> None:
    tool = load_tool("verified_sha_images")
    for header in ({}, {"Docker-Content-Digest": "sha256:nope"}, {"Docker-Content-Digest": "md5:" + "a" * 64}):
        registry = _registry({"truealpha-data-engine": [(200, dict(header))]}, [])
        with pytest.raises(tool.RegistryError, match="without a usable digest"):
            tool.split([PLAN[2]], owner="wangzitian0", sha=SHA, transport=registry)


def test_a_transient_registry_failure_is_retried_and_then_fails() -> None:
    tool = load_tool("verified_sha_images")
    slept: list[float] = []

    registry = _registry(
        {"truealpha-data-engine": [(503, {}), (429, {}), (200, {"Docker-Content-Digest": DIGEST})]}, []
    )
    result = tool.split([PLAN[2]], owner="wangzitian0", sha=SHA, transport=registry, sleep=slept.append)
    assert result["retag"][0]["digest"] == DIGEST
    assert len(slept) == 2 and slept == sorted(slept), "each retry backs off longer than the last"

    exhausted = _registry({"truealpha-data-engine": [(503, {})] * tool.ATTEMPTS}, [])
    with pytest.raises(tool.RegistryError, match="status 503"):
        tool.split([PLAN[2]], owner="wangzitian0", sha=SHA, transport=exhausted, sleep=slept.append)


def test_a_throttled_token_endpoint_is_retried_like_the_manifest_read() -> None:
    """Copilot review on #868: the manifest HEAD retried 429/5xx and the token GET did
    not, so a throttled token endpoint failed a tag run the manifest policy would have
    ridden out. One `_send` now carries the policy for both."""
    tool = load_tool("verified_sha_images")
    token_answers = [503, 429, 200]
    slept: list[float] = []

    def registry(method: str, url: str, headers: dict[str, str]) -> tool.Response:
        if url.startswith("https://ghcr.io/token?"):
            status = token_answers.pop(0)
            return tool.Response(status, {}, b'{"token": "anon"}' if status == 200 else b"")
        if "Authorization" not in headers:
            return tool.Response(401, {"Www-Authenticate": CHALLENGE.format(image="truealpha-data-engine")})
        return tool.Response(200, {"Docker-Content-Digest": DIGEST})

    digest = tool.published_digest(
        "wangzitian0/truealpha-data-engine", "sha-7de7813", transport=registry, sleep=slept.append
    )
    assert digest == DIGEST
    assert len(slept) == 2 and token_answers == []

    exhausted = [503] * tool.ATTEMPTS

    def throttled(method: str, url: str, headers: dict[str, str]) -> tool.Response:
        if url.startswith("https://ghcr.io/token?"):
            return tool.Response(exhausted.pop(0), {})
        return tool.Response(401, {"Www-Authenticate": CHALLENGE.format(image="truealpha-data-engine")})

    with pytest.raises(tool.RegistryError, match=r"anonymous pull token refused .*status 503"):
        tool.published_digest(
            "wangzitian0/truealpha-data-engine", "sha-7de7813", transport=throttled, sleep=slept.append
        )
    assert exhausted == [], "the token fetch gave up before spending its attempts"


def test_no_answer_at_all_is_retried_and_then_red_through_the_same_path() -> None:
    """Copilot review on #868: a refused connection or a DNS failure escaped as a
    traceback, which says nothing about the image or the SHA and skips the `::error::`
    line. The transport turns any OSError into a TransportError, which `_send` retries
    like a 5xx and then raises as the one RegistryError the CLI reports."""
    tool = load_tool("verified_sha_images")
    failures = [tool.TransportError("HEAD https://ghcr.io/x: no answer (refused)")] * 2
    slept: list[float] = []

    def flaky(method: str, url: str, headers: dict[str, str]) -> tool.Response:
        if failures:
            raise failures.pop(0)
        return tool.Response(200, {"Docker-Content-Digest": DIGEST})

    digest = tool.published_digest(
        "wangzitian0/truealpha-data-engine", "sha-7de7813", transport=flaky, sleep=slept.append
    )
    assert digest == DIGEST
    assert len(slept) == 2

    def dead(method: str, url: str, headers: dict[str, str]) -> tool.Response:
        raise tool.TransportError(f"{method} {url}: no answer (refused)")

    with pytest.raises(tool.RegistryError, match=r"no answer \(refused\) after 3 attempts"):
        tool.published_digest("wangzitian0/truealpha-data-engine", "sha-7de7813", transport=dead, sleep=slept.append)

    # The real transport, against a port nothing listens on: the OSError becomes the
    # TransportError above, not a traceback.
    with pytest.raises(tool.TransportError, match="no answer"):
        tool.urllib_transport("HEAD", "https://127.0.0.1:9/v2/x/manifests/y", {})


def test_a_challenge_that_is_not_bearer_is_refused() -> None:
    tool = load_tool("verified_sha_images")

    def basic(method: str, url: str, headers: dict[str, str]) -> tool.Response:
        return tool.Response(401, {"Www-Authenticate": 'Basic realm="ghcr"'})

    with pytest.raises(tool.RegistryError, match="not a Bearer challenge"):
        tool.published_digest("wangzitian0/truealpha-data-engine", "sha-7de7813", transport=basic)


def test_the_cli_splits_the_plan_and_names_both_halves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = load_tool("verified_sha_images")
    registry = _registry(
        {
            "truealpha-app-web": [(404, {})],
            "truealpha-llm-service": [(200, {"Docker-Content-Digest": DIGEST})],
            "truealpha-data-engine": [(200, {"Docker-Content-Digest": DIGEST})],
        },
        [],
    )
    monkeypatch.setattr(tool, "urllib_transport", registry)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(PLAN)))

    assert tool.main(["--owner", "wangzitian0", "--sha", SHA]) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert [entry["image"] for entry in result["retag"]] == ["truealpha-llm-service", "truealpha-data-engine"]
    assert result["build"] == [PLAN[0]]
    assert (
        "retag truealpha-llm-service: main published ghcr.io/wangzitian0/truealpha-llm-service:sha-7de7813"
        in captured.err
    )
    assert "build truealpha-app-web: main did not publish" in captured.err and "#731" in captured.err


def test_the_cli_is_red_on_a_registry_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = load_tool("verified_sha_images")
    monkeypatch.setattr(tool, "urllib_transport", _registry({"truealpha-data-engine": [(403, {})]}, []))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([PLAN[2]])))

    assert tool.main(["--owner", "wangzitian0", "--sha", SHA]) == 1

    captured = capsys.readouterr()
    assert captured.out == "", "a failed split must not print a plan the workflow could parse as a decision"
    assert captured.err.startswith("::error::")


def test_a_short_sha_on_the_command_line_is_refused_before_any_registry_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = load_tool("verified_sha_images")
    seen: list[tuple[str, str, dict[str, str]]] = []
    monkeypatch.setattr(tool, "urllib_transport", _registry({}, seen))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(PLAN)))

    assert tool.main(["--owner", "wangzitian0", "--sha", "7de7813"]) == 1
    assert seen == []
    assert "full 40-hex commit sha" in capsys.readouterr().err
