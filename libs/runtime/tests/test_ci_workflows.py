from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_manual_image_release_is_explicit_and_waits_for_required_jobs():
    required = (ROOT / ".github" / "workflows" / "ci-required.yml").read_text(encoding="utf-8")
    image_release = required.split("  images_release:\n", 1)[1].split("\n  required:\n", 1)[0]

    assert "workflow_dispatch:\n    inputs:\n      force_images:" in required
    assert "description: Publish all current-ref images after required checks." in required
    assert "type: boolean\n        default: false" in required
    assert "github.event_name == 'workflow_dispatch' && inputs.force_images" in image_release
    assert "github.event_name == 'push'" in image_release
    for dependency in ("security", "db", "python", "qlib", "runtime", "web"):
        assert f"needs.{dependency}.result == 'success' || needs.{dependency}.result == 'skipped'" in image_release
    assert "publish: true" in image_release
    assert "app_web: true" in image_release
    assert "llm_service: true" in image_release
    assert "data_engine: true" in image_release
