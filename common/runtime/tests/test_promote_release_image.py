"""The release tag gets the bytes of the main image, never a rebuild (#1878)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from truealpha_runtime.testing import load_tool

_tool = load_tool("promote_release_image")
Response, digest_of, promote, source_tag_for = _tool.Response, _tool.digest_of, _tool.promote, _tool.source_tag_for

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
INDEX = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}'
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"


class FakeRegistry:
    """Store manifests by tag. Can hide the source for the first N reads."""

    def __init__(self, manifests: dict[str, tuple[str, bytes]], *, missing_reads: int = 0, mangle: bool = False):
        self.manifests = dict(manifests)
        self.missing_reads = missing_reads
        self.mangle = mangle
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        tag = url.rsplit("/manifests/", 1)[1]
        self.calls.append((method, tag))
        if method == "PUT":
            assert body is not None
            self.manifests[tag] = (headers["Content-Type"], body + b" " if self.mangle else body)
            return Response(201, {}, b"")
        if tag.startswith("sha-") and self.missing_reads:
            self.missing_reads -= 1
            return Response(404, {}, b"")
        if tag not in self.manifests:
            return Response(404, {}, b"")
        content_type, stored = self.manifests[tag]
        return Response(200, {"content-type": content_type, "docker-content-digest": digest_of(stored)}, stored)


def test_the_release_tag_gets_the_same_bytes_and_digest_as_the_main_image() -> None:
    registry = FakeRegistry({"sha-1cf29c9": (INDEX_TYPE, INDEX)})
    digest = promote("o/img", "sha-1cf29c9", "v0.2.146", "t", registry, sleep=lambda _: None)
    assert registry.manifests["v0.2.146"] == (INDEX_TYPE, INDEX)
    assert digest == digest_of(INDEX)
    assert ("PUT", "v0.2.146") in registry.calls
    assert ("PUT", "sha-1cf29c9") not in registry.calls


def test_a_late_main_image_is_awaited_and_then_promoted() -> None:
    registry = FakeRegistry({"sha-1cf29c9": (INDEX_TYPE, INDEX)}, missing_reads=2)
    waits: list[float] = []
    promote("o/img", "sha-1cf29c9", "v1.0.0", "t", registry, attempts=3, delay_seconds=5, sleep=waits.append)
    assert waits == [5, 5]
    assert registry.manifests["v1.0.0"][1] == INDEX


def test_a_missing_main_image_fails_and_pushes_nothing() -> None:
    registry = FakeRegistry({})
    with pytest.raises(RuntimeError, match="is not published"):
        promote("o/img", "sha-1cf29c9", "v1.0.0", "t", registry, attempts=3, sleep=lambda _: None)
    assert not [call for call in registry.calls if call[0] == "PUT"]


def test_a_release_tag_with_other_bytes_after_the_push_fails() -> None:
    registry = FakeRegistry({"sha-1cf29c9": (INDEX_TYPE, INDEX)}, mangle=True)
    with pytest.raises(RuntimeError, match="does not resolve"):
        promote("o/img", "sha-1cf29c9", "v1.0.0", "t", registry, sleep=lambda _: None)


def test_a_single_manifest_keeps_its_own_content_type() -> None:
    manifest_type = "application/vnd.docker.distribution.manifest.v2+json"
    registry = FakeRegistry({"sha-1cf29c9": (manifest_type, b'{"schemaVersion":2}')})
    promote("o/img", "sha-1cf29c9", "v1.0.0", "t", registry, sleep=lambda _: None)
    assert registry.manifests["v1.0.0"] == (manifest_type, b'{"schemaVersion":2}')


def test_a_body_that_differs_from_the_registry_digest_fails_before_any_push() -> None:
    def transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        assert method == "GET", "no push may happen"
        return Response(200, {"content-type": INDEX_TYPE, "docker-content-digest": "sha256:" + "0" * 64}, INDEX)

    with pytest.raises(RuntimeError, match="differs from the registry header"):
        promote("o/img", "sha-1cf29c9", "v1.0.0", "t", transport, sleep=lambda _: None)


def test_a_transient_registry_fault_is_retried() -> None:
    registry = FakeRegistry({"sha-1cf29c9": (INDEX_TYPE, INDEX)})
    faults = iter([Response(503, {}, b"")])

    def transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        if method == "GET" and url.endswith("sha-1cf29c9"):
            fault = next(faults, None)
            if fault is not None:
                return fault
        return registry(method, url, headers, body)

    promote("o/img", "sha-1cf29c9", "v1.0.0", "t", transport, sleep=lambda _: None)
    assert registry.manifests["v1.0.0"][1] == INDEX


@pytest.mark.parametrize(
    ("commit", "tag"),
    [("1CF29C9e4b0d2a7c", "sha-1cf29c9"), ("1cf29c9", "sha-1cf29c9")],
)
def test_the_source_tag_matches_the_metadata_action_short_sha(commit: str, tag: str) -> None:
    assert source_tag_for(commit) == tag


@pytest.mark.parametrize("commit", ["1cf29c", "v0.2.146", ""])
def test_a_ref_that_is_not_a_commit_sha_is_refused(commit: str) -> None:
    with pytest.raises(ValueError, match="not a hex SHA"):
        source_tag_for(commit)


def test_tag_and_main_runs_of_one_commit_do_not_cancel_each_other() -> None:
    group = yaml.safe_load((WORKFLOWS / "ci-required.yml").read_text(encoding="utf-8"))["concurrency"]["group"]
    assert "github.ref_type" in group


def test_a_tag_push_promotes_and_never_builds() -> None:
    req = yaml.safe_load((WORKFLOWS / "ci-required.yml").read_text(encoding="utf-8"))["jobs"]
    images = yaml.safe_load((WORKFLOWS / "release-images.yml").read_text(encoding="utf-8"))["jobs"]
    for name in ("images_build", "images_release"):
        assert "github.ref_type != 'tag'" in req[name]["if"], name
    promote_job = req["images_promote"]
    assert "github.ref_type == 'tag'" in promote_job["if"]
    assert promote_job["with"]["promote"] is True
    assert "images_promote" in req["required"]["needs"]
    for name in ("build", "publish"):
        assert "!inputs.promote" in images[name]["if"], name
    assert "inputs.promote" in images["promote"]["if"]
    promote_runs = "\n".join(str(step.get("run", "")) for step in images["promote"]["steps"])
    assert "tools/promote_release_image.py" in promote_runs
    assert not [s for s in images["promote"]["steps"] if "build-push-action" in str(s.get("uses", ""))]
    publish_tags = "\n".join(str(step.get("with", {}).get("tags", "")) for step in images["publish"]["steps"])
    assert "type=semver" not in publish_tags
