from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_manual_image_release_is_explicit_and_waits_for_required_jobs():
    required = (ROOT / ".github" / "workflows" / "ci-required.yml").read_text(encoding="utf-8")
    _before_release, release_marker, after_release = required.partition("  images_release:\n")
    image_release, required_marker, _after_required = after_release.partition("\n  required:\n")

    assert release_marker, "images_release job is missing"
    assert required_marker, "required job boundary is missing after images_release"
    assert "workflow_dispatch:\n    inputs:\n      force_images:" in required
    assert "description: Publish all current-ref images after required checks." in required
    assert "type: boolean\n        default: false" in required
    assert "github.event_name == 'workflow_dispatch' &&" in image_release
    assert "github.ref == 'refs/heads/main' &&" in image_release
    assert "inputs.force_images" in image_release
    assert "github.event_name == 'push'" in image_release
    assert "needs.changes.result == 'success'" in image_release
    assert "needs.security.result == 'success'" in image_release
    for dependency in ("db", "python", "qlib", "runtime", "web"):
        assert f"needs.{dependency}.result == 'success' || needs.{dependency}.result == 'skipped'" in image_release
    assert "publish: true" in image_release
    assert "app_web: true" in image_release
    assert "llm_service: true" in image_release
    assert "data_engine: true" in image_release
