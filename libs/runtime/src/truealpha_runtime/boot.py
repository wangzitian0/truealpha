"""Boot-time environment validation (#759): a process refuses to start on a missing name.

`required-env.generated.json` says which variables a service reads and who supplies each.
Until now that file bound only the deployer (templates, policies, the daily
reconciliation); the process itself started on whatever reached it and failed later,
wherever the value was first used. `assert_environment` closes that gap at the
entrypoint: the names the manifest declares and the environment lacks are the whole
error -- names, never values.
"""

from __future__ import annotations

from infra2_sdk.manifests import assert_environment

__all__ = ["assert_environment"]

