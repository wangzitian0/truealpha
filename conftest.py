"""Local/CI object-store coordinates, supplied to tests rather than shipped in Settings.

`RuntimeSettings` used to default `s3_endpoint` to `http://localhost:9000` with the
credentials `minio` / `minio_local_secret` baked into tracked source. Those defaults made
the whole suite pass without configuring anything — and made PRODUCTION dial its own
loopback with placeholder credentials when no `S3_*` variables reached the container,
which is #531.

The values themselves are not secret; where they belong is the point. A local-dev
convenience that is also the production FALLBACK is not a convenience, it is an
unconfigured environment that looks configured. So they live here, in the test harness,
where they cannot be reached by a deployed process. CI sets the same three explicitly in
`ci-python.yml` and `ci-runtime.yml`; this file is what keeps `uv run pytest` working on a
developer's machine without re-introducing a shipped default.
"""

import os

_LOCAL_OBJECT_STORE = {
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_ACCESS_KEY": "minio",
    "S3_SECRET_KEY": "minio_local_secret",
}

for _name, _value in _LOCAL_OBJECT_STORE.items():
    os.environ.setdefault(_name, _value)
