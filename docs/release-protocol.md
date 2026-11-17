# Release protocol

Release orchestration, staging auto-release, surface-walk gating, daily delivery
reporting, and scheduler-liveness checks run centrally in wangzitian0/infra2
(#1588). This repository publishes its container images on `main` pushes via
 `.github/workflows/ci-required.yml` and `.github/workflows/release-images.yml`,
 and declares its runtime verification hooks in `runtime-hooks.manifest.json`.

## Central release train and `runtime-hooks.manifest.json`

`runtime-hooks.manifest.json` (`schema_version: 2`, `service: "truealpha/app"`)
is the single contract between `truealpha` and the `infra2` release train.
`infra2_sdk.manifests` (`python -m infra2_sdk.manifests check`) validates it alongside
`env.manifest.json` on every CI run.

It declares five `Pre -> Deploy -> Post` verification slots that `infra2` invokes. Each slot lists
its checks in the manifest; the SDK runner runs them in order and writes `checks.jsonl`.

The `infra2` release train enforces its own daily automatic-release cap
(`DEFAULT_DAILY_CAP` in the `infra2` release controller) and staging soak rules.

## Production needs the owner

Owner instruction, 2026-10-06:

> 只有 prod 环境部署需要我批准和在场。其他的事情 agent 都可以自己干，但是需要满足每个 stage 的 checklist（部分是文字描述、部分是 skill、部分是门禁）。

Production promotion needs two things from the owner: approval of the exact
release SHA, and the owner's presence while the promotion runs in `infra2`.
Every other action is the agent's task when the checklist of its stage passes.
This covers merging any PR, including a CI or governance change, and staging
verification. `.github/CODEOWNERS` asks the owner for a review of CI and
governance paths. It is not a merge condition.
