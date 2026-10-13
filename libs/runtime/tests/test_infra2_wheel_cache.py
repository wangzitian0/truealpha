"""Tests for tools/infra2_wheel_cache.py (Issue #1248).

Acceptance criteria:
• A CI job that cannot reach the release URL still installs the wheel from the cache when the sha256 matches.
• A wrong sha256 still fails the job.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import infra2_wheel_cache  # noqa: E402


def test_extract_infra2_wheel_spec_reads_lockfile() -> None:
    lock_path = REPO_ROOT / "uv.lock"
    filename, url, sha256 = infra2_wheel_cache.extract_infra2_wheel_spec(lock_path)
    assert filename.startswith("infra2_sdk-") and filename.endswith(".whl")
    assert url.startswith("https://github.com/wangzitian0/infra2-sdk/releases/download/")
    assert len(sha256) == 64 and sha256.islower()
    assert int(sha256, 16) > 0


def test_cache_hit_with_matching_sha256_makes_zero_network_calls(tmp_path: Path) -> None:
    """Acceptance (1): cached wheel with matching sha256 succeeds without network access."""
    lock_path = REPO_ROOT / "uv.lock"
    filename, url, expected_hash = infra2_wheel_cache.extract_infra2_wheel_spec(lock_path)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    wheel_file = cache_dir / filename

    content = b"fake-wheel-matching-bytes-content"
    actual_hash = hashlib.sha256(content).hexdigest()
    wheel_file.write_bytes(content)

    # Patch lock spec to expect actual_hash
    with patch.object(
        infra2_wheel_cache,
        "extract_infra2_wheel_spec",
        return_value=(filename, url, actual_hash),
    ):
        with patch("urllib.request.urlopen") as mock_urlopen:
            code = infra2_wheel_cache.ensure_wheel(
                lock_path=lock_path,
                cache_dir=cache_dir,
                offline=True,
            )
            assert code == 0
            mock_urlopen.assert_not_called()


def test_corrupted_cached_wheel_fails_the_job(tmp_path: Path) -> None:
    """Acceptance (2): a wrong sha256 in cache fails the job."""
    lock_path = REPO_ROOT / "uv.lock"
    filename, url, expected_hash = infra2_wheel_cache.extract_infra2_wheel_spec(lock_path)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    wheel_file = cache_dir / filename
    wheel_file.write_bytes(b"corrupted-invalid-bytes")

    code = infra2_wheel_cache.ensure_wheel(
        lock_path=lock_path,
        cache_dir=cache_dir,
        offline=True,
    )
    assert code == 1, "Wrong sha256 must fail the job"


def test_cache_miss_offline_fails_without_crashing(tmp_path: Path) -> None:
    lock_path = REPO_ROOT / "uv.lock"
    cache_dir = tmp_path / "empty_cache"

    code = infra2_wheel_cache.ensure_wheel(
        lock_path=lock_path,
        cache_dir=cache_dir,
        offline=True,
    )
    assert code == 1


def test_download_retries_on_504_gateway_timeout_and_succeeds(tmp_path: Path) -> None:
    """Proves survival of transient 504 Gateway Timeout via exponential retry budget."""
    lock_path = REPO_ROOT / "uv.lock"
    filename, url, _ = infra2_wheel_cache.extract_infra2_wheel_spec(lock_path)

    valid_content = b"valid-downloaded-wheel-bytes"
    valid_hash = hashlib.sha256(valid_content).hexdigest()
    cache_dir = tmp_path / "cache"

    response_mock = MagicMock()
    response_mock.read.return_value = valid_content
    response_mock.__enter__.return_value = response_mock

    # First attempt returns 504 Gateway Timeout, second attempt succeeds
    error_504 = urllib.error.HTTPError(url, 504, "Gateway Timeout", {}, None)  # type: ignore[arg-type]

    attempts = [error_504, response_mock]

    def fake_urlopen(*args, **kwargs):
        res = attempts.pop(0)
        if isinstance(res, Exception):
            raise res
        return res

    with patch.object(
        infra2_wheel_cache,
        "extract_infra2_wheel_spec",
        return_value=(filename, url, valid_hash),
    ):
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("time.sleep") as mock_sleep:
                code = infra2_wheel_cache.ensure_wheel(
                    lock_path=lock_path,
                    cache_dir=cache_dir,
                    max_retries=3,
                    initial_backoff=0.01,
                )
                assert code == 0
                mock_sleep.assert_called_once_with(0.01)

    cached_wheel = cache_dir / filename
    assert cached_wheel.exists()
    assert cached_wheel.read_bytes() == valid_content


def test_download_with_mismatched_sha256_refuses_to_cache(tmp_path: Path) -> None:
    """If downloaded content does not match expected sha256, it must fail and not save to cache."""
    lock_path = REPO_ROOT / "uv.lock"
    filename, url, _ = infra2_wheel_cache.extract_infra2_wheel_spec(lock_path)

    downloaded_content = b"spoofed-content"
    expected_hash = "0" * 64
    cache_dir = tmp_path / "cache"

    response_mock = MagicMock()
    response_mock.read.return_value = downloaded_content
    response_mock.__enter__.return_value = response_mock

    with patch.object(
        infra2_wheel_cache,
        "extract_infra2_wheel_spec",
        return_value=(filename, url, expected_hash),
    ):
        with patch("urllib.request.urlopen", return_value=response_mock):
            code = infra2_wheel_cache.ensure_wheel(
                lock_path=lock_path,
                cache_dir=cache_dir,
                max_retries=1,
            )
            assert code == 1

    cached_wheel = cache_dir / filename
    assert not cached_wheel.exists(), "Corrupted download must not be left in cache"
