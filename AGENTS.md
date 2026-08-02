# AGENTS.md

## Scope

These instructions apply to the entire repository. A more deeply nested
`AGENTS.md`, if one is added later, overrides this file for its subtree.

## Repository purpose

This repository owns CI orchestration for Conch; it is not the Conch source
repository. It contains GitHub workflows and composite actions, mirrors
AtomGit branches and pull requests into GitHub, and manages reproducible tool
environments used by self-hosted runners.

Keep these boundaries intact:

- Resolve a requested Conch ref once, in a hosted job, to a full commit ID.
  Downstream jobs must consume that immutable commit rather than resolving a
  branch or tag again.
- Declare persistent runner dependencies only in `runner-env.lock.yaml` and
  install them below `${RUNNER_TOOL_CACHE}/conch-ci`.
- Keep dependencies used by only one workflow beside that workflow under
  `scripts/workflows/`; do not promote them to persistent runner state.
- Treat the self-hosted runner's host OS, KVM, Docker, sudo, network, and
  compiler toolchain as verified prerequisites. Workflows must not install or
  upgrade host packages on that runner.
- Build and consume artifacts by immutable IDs or digests. Do not rely on an
  old workspace, `/opt/conch`, mutable image tags, or other runner-local state.
- Kernel and rootfs build IDs intentionally capture semantic source, config, and
  recipe inputs, but not the host compiler/toolchain or BuildKit implementation.
  Toolchain changes do not invalidate those caches by policy; explicitly change
  a semantic input or evict the cache when a toolchain change must be exercised.

## Repository map

- `.github/workflows/` contains top-level orchestration and permissions.
- `.github/actions/` contains reusable composite actions. Keep common Conch
  build/runtime behavior here rather than copying it between workflows.
- `scripts/runner-env/runner_env.py` implements `ensure`, `verify`, and
  `print-id` for the persistent runner environment.
- `scripts/runner-env/jobs/` contains reproducible checkout and artifact build
  helpers; `scripts/runner-env/runtime/` contains runtime lifecycle helpers.
- `scripts/runner-env/lib/` contains strict lock parsing, safe archive handling,
  and content-addressed ID helpers.
- `runner-env.lock.yaml` is the single source of truth for managed component
  versions, checksums, the Go toolchain, and kernel source inputs.
- `scripts/workflows/` contains workflow-local scripts and locked Python
  dependencies.
- `scripts/sync_atomgit_prs.py` performs externally visible mirror and PR
  updates. Do not run it against live services as a routine local check.

## Reproducibility and security rules

- Pin downloads and tool inputs to exact versions or commits and verify their
  SHA-256 digests before use. Never introduce `latest`, floating version
  ranges, unverified archives, or a second dependency lock.
- When an output can change, include every relevant source, recipe, config,
  platform, and tool input in its build ID. Update producers and consumers
  together when an ID contract changes.
- Preserve the distinction between `verify` (read-only drift detection) and
  `ensure` (installation or repair). Only the dedicated runner preparation
  workflow should invoke `ensure` automatically.
- Keep runner-specific labels and platform assumptions in the workflows that
  target those runners. Do not bake one runner architecture or vendor into
  otherwise reusable actions and scripts.
- Give workflows the minimum `permissions` they require. Do not print secrets,
  embed tokens in diagnostic output, or persist checkout credentials in sync
  jobs.
- Pass GitHub expressions and action inputs into shell code through `env` where
  practical, quote expansions, and write action data through `$GITHUB_OUTPUT`,
  `$GITHUB_PATH`, and `$GITHUB_STEP_SUMMARY`.
- Give jobs explicit timeouts. Cleanup steps for VMs, containers, network
  namespaces, mounts, and temporary files must use `if: always()` where a
  preceding step can fail.

## Editing conventions

- Use two-space indentation in YAML and keep workflow/action names descriptive.
- Composite actions and workflow shell blocks use Bash. Start non-trivial
  blocks with `set -euo pipefail`, quote variables, and use `mktemp` plus a
  cleanup trap for temporary state.
- Python code must support Python 3.10 or newer. Prefer the standard library,
  `pathlib`, type annotations, narrow exceptions, and deterministic ordering.
  The runner manager and sync script intentionally have no third-party Python
  dependencies; workflow-only dependencies belong in the adjacent locked
  requirements file.
- Keep validation strict and fail closed: reject unknown lock fields, unsafe
  archive members, unsupported platforms, missing checksums, and malformed
  action inputs instead of guessing defaults.
- Preserve executable bits on entry-point `.sh` and `.py` files. Do not commit
  caches, downloaded sources, build output, VM images, credentials, or
  `__pycache__` directories.
- Follow the existing concise commit style, such as `ci: ...` or
  `refactor(ci): ...`. Keep unrelated edits out of the same change.

## Coupled changes

Review all affected consumers when changing any of these contracts:

- A `runner-env.lock.yaml` component change normally requires updating its
  checksum and may require updating the matching installer, verifier, runtime
  helper, or build recipe.
- Files under `scripts/runner-env/` and
  `.github/actions/ensure-runner-environment/` contribute to the repository
  environment ID. Do not add a hand-maintained ID constant.
- Changes to composite-action inputs or outputs require updates to every
  calling workflow.
- Workflow filename changes require updates to the allowlist in
  `.github/workflows/sync-conch.yml`, sync defaults, and user-facing README
  instructions.
- Changes to artifact contents or publication recipes that are intended to
  change semantic output require a corresponding build-ID input change so stale
  caches or tags cannot be reused. Host toolchain and BuildKit changes are the
  documented exception and do not change the build ID by themselves.
- Update `README.md` whenever dispatch inputs, schedules, runner prerequisites,
  permissions, secrets, or the producer/consumer flow changes.

## Validation

Run checks from the repository root and choose the checks relevant to the
files changed. The following checks are safe and do not mutate the runner:

```bash
git diff --check
python3 scripts/runner-env/lib/lock.py validate
scripts/runner-env/runner-env.sh print-id
git ls-files -z '*.sh' | xargs -0 -r bash -n
python3 - <<'PY'
import ast
from pathlib import Path

for path in sorted(Path(".").rglob("*.py")):
    if ".git" not in path.parts:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY
```

If ShellCheck is installed, also run:

```bash
git ls-files -z '*.sh' | xargs -0 -r shellcheck
```

If `actionlint` is installed, run it after changing files in
`.github/workflows/`. Review composite-action YAML separately because
`actionlint` targets workflows rather than `action.yml` metadata.

Do not treat `runner-env.sh verify` as a portable local test: it intentionally
requires a supported self-hosted runner platform and an already installed
managed environment. Never run `runner-env.sh ensure` merely for validation
because it changes persistent runner state.

The hosted `Conch Build and Check` workflow is the broad integration check.
Kernel, VM, Template, and E2B validation must run through their corresponding
workflow on the labeled self-hosted runner. Dispatching workflows, publishing
images, or running the AtomGit sync changes external state and requires
explicit authorization.
