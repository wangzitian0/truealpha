from datetime import UTC, datetime

from botocore.exceptions import ClientError
from truealpha_contracts import DataSource, RawCapture
from truealpha_runtime import (
    DEPENDENCY_MANIFEST,
    DeploymentSettings,
    EnvironmentTier,
    RuntimeSettings,
    S3RawObjectStore,
)
from truealpha_runtime.tiers import resolve_environment_tier


class FakeS3:
    def __init__(self):
        self.bucket_exists = False
        self.objects = {}
        self.put_count = 0

    def head_bucket(self, *, Bucket):
        if not self.bucket_exists:
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    def create_bucket(self, **kwargs):
        self.bucket_exists = True

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.put_count += 1
        self.objects[Key] = Body


def test_manifest_declares_database_graph_and_object_storage_for_every_tier():
    for tier in EnvironmentTier:
        assert DEPENDENCY_MANIFEST.required_for(tier) == {"database", "graph_store", "object_storage"}


def test_environment_resolution_is_explicit():
    assert resolve_environment_tier("dev") is EnvironmentTier.LOCAL_DEV
    assert resolve_environment_tier("ci", github_actions=True) is EnvironmentTier.GITHUB_CI


def test_runtime_dependency_environment_keys_do_not_drift():
    declared = {env_var for dependency in DEPENDENCY_MANIFEST for env_var in dependency.env_vars}
    settings_keys = {
        field_name.upper()
        for field_name in RuntimeSettings.model_fields
        if field_name not in {"app_env", "git_commit_sha"}
    }
    assert declared == settings_keys


def test_deployment_environment_is_typed_separately_from_app_config():
    settings = DeploymentSettings(_env_file=None, restart_policy="always", docker_log_max_file=2)
    assert settings.compose_project_name == "truealpha"
    assert settings.restart_policy == "always"


def test_raw_storage_is_content_addressed_and_idempotent():
    client = FakeS3()
    settings = RuntimeSettings(_env_file=None, app_env="test")
    store = S3RawObjectStore(settings, client=client)
    capture = RawCapture(
        source=DataSource.SEC,
        source_record_id="0001564590-20-006422",
        body=b'{"entityName":"Datadog, Inc."}',
        content_type="application/json",
        fetched_at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    first = store.store(capture)
    second = store.store(capture)

    assert first.object.key == second.object.key
    assert first.object.uri.startswith("s3://truealpha-raw/raw/sec/")
    assert client.put_count == 1
