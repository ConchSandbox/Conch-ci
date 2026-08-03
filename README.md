# Conch CI

This repository owns the GitHub-side CI workflows for validating Conch source
refs, and it also mirrors AtomGit `openeuler/Conch` branches and pull requests
into GitHub `ConchSandbox/Conch`.

## CI workflows

The main CI workflows resolve the requested Conch ref once to a full commit
before any self-hosted job starts:

- `build-and-check.yml`: build, static checks, Go tests, Go vet, and Python SDK import
  checks.
- `conch-init-smoke.yml`: boots `conch-init` as PID 1 in a real
  cloud-hypervisor VM, then verifies vsock readiness and SDK health.
- `e2b-template-weekly.yml`: builds or reuses the kernel and rootfs, publishes a
  content-addressed E2B Conch Template to the runner-local OCI registry, and can
  optionally mirror both images to GHCR on manual runs.
- `e2b-workload-smoke.yml`: consumes the same Template producer from the local
  registry and optionally runs E2B SDK operations.
- `prepare-self-hosted-runner.yml`: verifies or installs the locked runner
  environment, and applies reviewed environment changes merged to `main`.

Common dispatch inputs:

```text
ci_marker=<external correlation marker>
conch_repository=<Conch source repository URL>
conch_ref=<Conch source ref to validate>
```

Self-hosted jobs target the `taishan2280-oe2403sp3` runner through the
`self-hosted`, `Linux`, `ARM64`, and `Huawei` labels. Persistent dependencies
are declared only in [`runner-env.lock.yaml`](runner-env.lock.yaml) and
installed under `${RUNNER_TOOL_CACHE}/conch-ci`; host OS, KVM, Docker, sudo,
network, and compiler capabilities are checked but never installed by this
repository. Workflow-only dependencies live with their owning workflow under
`scripts/workflows/` and are installed only by that workflow.

## Kernel and Template producers

`build-and-check.yml`, the Template producer, and the E2E consumer all build Conch
commands through the same `build-conch` action. The action reads the exact Go
version from the lock and lets `setup-go` select the runner architecture.

The Conch Init smoke, Template E2E, and weekly Template workflows run
`build-kernel` first. Its build ID contains exactly the locked kernel source
commit, the selected Conch kernel config content digest, and the normalized
platform. A valid Actions cache hit avoids compilation, but every run still
publishes and validates a workflow-local kernel artifact. Consumers never read
`/opt/conch/bzImage`.

The kernel build ID intentionally does not include the host compiler, compiler
flags, or other build-tool versions. Those are treated as runner infrastructure
rather than semantic test inputs, so changing them does not invalidate an
existing kernel cache. When a toolchain change must be exercised, change an
explicit kernel input or evict the relevant Actions cache. This policy favors
stable test reuse over strict byte-for-byte reproducibility across toolchains.

The generic `build-conch-template` action first builds or reuses the rootfs OCI
image. Its rootfs build ID is derived from the platform, exact Conch commit,
selected Dockerfile path, and rootfs build script digest. The action then
combines that immutable rootfs, the kernel artifact, and an initramfs produced
by the shared `build-conch-initramfs` action into a native Conch boot index and
publishes it to a loopback-only OCI registry managed by the runner environment.
The Template build ID contains the rootfs reference, kernel digest, Conch
commit, and Template recipe digest.

Each workflow owns a fixed image profile and passes it to the common action;
the profile is not exposed as a dispatch input. Repository names are derived as:

```text
localhost:5000/conch-ci/conch-<profile>-rootfs
localhost:5000/conch-ci/conch-<profile>-template
```

For the E2B workflows, `<profile>` is `e2b`. Producers and consumers exchange
only immutable `@sha256:<digest>` references. The registry address, plain-HTTP
mode, repository prefix, and lack of authentication are repository policy and
are not user-configurable inputs.

The rootfs build ID likewise intentionally excludes the BuildKit version and
its host-side wrapper. It includes the source, Dockerfile, platform, and rootfs
recipe inputs that define the semantic test image. Builder changes therefore do
not automatically invalidate an existing rootfs tag; explicitly change a
semantic input or evict the cache when a builder change must be validated.

The locked `distribution/distribution` binary, its configuration, and its data
directory live below `${RUNNER_TOOL_CACHE}/conch-ci`. The preparation workflow
installs and enables its systemd service on `127.0.0.1:5000`; normal CI jobs only
verify that the declared version, configuration, service state, and `/v2/`
health endpoint are ready. A matching local tag avoids rebuilding and
republishing the Template, and E2E pulls the resulting boot index instead of
running `conch template create` again.

Manual runs of `e2b-template-weekly.yml` expose a `publish_to_ghcr` checkbox,
which defaults to false. When selected, a permission-scoped job also copies the
immutable rootfs and Template to these fixed repositories:

```text
ghcr.io/conchsandbox/conch-e2b-rootfs
ghcr.io/conchsandbox/conch-e2b-template
```

Scheduled runs remain local-only. There is no registry-address, authentication,
`build_rootfs`, or rootfs-image override. Conch CNI configuration is copied
from the exact Conch checkout into the job directory; only managed environment
data, including the registry cache and locked CNI plugin binaries, persists in
the runner tool cache.

## AtomGit mirror sync

The scheduled `Conch Sync` workflow synchronizes AtomGit branches and mirrored
pull requests. Scheduled runs do not trigger CI.

The schedule currently runs at:

```text
0 2,8,14 * * *
```

Manual `workflow_dispatch` runs behave the same way unless `atomgit_pr_number`
is set.

## Manual AtomGit PR CI

To sync one AtomGit PR and run GitHub CI, manually run the `Conch Sync`
workflow with:

```text
atomgit_pr_number=<AtomGit PR number>
run_build=<checked by default>
run_conch_init_smoke=<unchecked by default>
run_e2b_workload_smoke=<unchecked by default>
```

`atomgit_pr_number` accepts exactly one PR number. Leave it empty to sync
branches and mirrored pull requests without running CI.

The build workflow checkbox defaults to selected, while both smoke workflow
checkboxes default to unselected. Select either smoke workflow to include it in
that manual dispatch. If `atomgit_pr_number` is set, at least one CI workflow
must be selected.

Manual CI dispatch always starts a new GitHub Actions run for the selected
AtomGit PR head. Existing completed runs are not reused.

When CI is enabled, the workflow:

1. Mirrors the AtomGit PR head to `ConchSandbox/Conch` as `atomgit/pr-<number>`.
2. Dispatches the selected workflows in this repository. By default this is
   only `build-and-check.yml`; `conch-init-smoke.yml` and
   `e2b-workload-smoke.yml` are opt-in.
3. Waits for the GitHub Actions run to finish.
4. Updates the GitHub mirror pull request body with the CI result and run link.

`e2b-workload-smoke.yml` is named `E2B Workload Smoke` in the GitHub Actions UI. It
pulls the immutable Template published by its producer job and, by default,
runs the dependent SDK E2E job in the same workflow run.

The CI section is kept only for the same AtomGit head SHA. If the AtomGit PR is
updated, the next sync clears the old CI section until CI is run again for the
new head.

## Required secrets and permissions

`CONCH_SYNC_APP_PRIVATE_KEY` is required for normal mirroring. The GitHub App
must be installed on `ConchSandbox/Conch`.

The optional GHCR publication job uses its run-scoped `GITHUB_TOKEN` with
`packages: write`; the local registry path needs no registry credentials. Other
Template producer and consumer jobs do not request package permissions.

The GitHub App installation must grant these repository permissions for
`ConchSandbox/Conch`:

- `Contents: Read and write`
- `Pull requests: Read and write`

When `atomgit_pr_number` is set, the sync workflow uses this repository's
`GITHUB_TOKEN` with `Actions: write` permission to dispatch and watch the local
CI workflows.
