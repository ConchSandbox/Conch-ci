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
- `e2b-template-weekly.yml`: builds or reuses the kernel and rootfs, then
  publishes a content-addressed E2B Conch Template to GHCR.
- `e2b-workload-smoke.yml`: consumes the same Template producer, pulls its immutable
  published output, and optionally runs E2B SDK operations.
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

The generic `build-conch-template` action first builds or reuses the rootfs OCI image. Its
rootfs build ID is derived from the platform, exact Conch commit, selected
Dockerfile path, and rootfs build script digest. The action then combines that
immutable rootfs, the kernel artifact, and an initramfs produced by the shared
`build-conch-initramfs` action into a native Conch boot index and publishes it
to GHCR. The Template build ID contains the rootfs reference, kernel digest,
Conch commit, and Template recipe digest. Dockerfile and rootfs/Template
repositories are workflow inputs to the action; the common action contains no
E2B-specific paths or repository names. Consumers receive only an immutable reference:

```text
ghcr.io/conchsandbox/conch-e2b-template@sha256:<digest>
```

The weekly and E2E workflows call the same Template action. A matching remote
tag avoids rebuilding and republishing the Template; E2E pulls the published
boot index instead of running `conch template create`. There is no runner-local
registry, `build_rootfs` switch, or rootfs image override. Conch CNI
configuration is copied from the exact Conch checkout into the job directory;
only the locked CNI plugin binaries persist in the runner tool cache.

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
run_conch_init_smoke=<checked by default>
run_e2b_workload_smoke=<checked by default>
```

`atomgit_pr_number` accepts exactly one PR number. Leave it empty to sync
branches and mirrored pull requests without running CI.

The CI workflow checkboxes default to selected. Uncheck a workflow to skip it
for that manual dispatch. If `atomgit_pr_number` is set, at least one CI
workflow must be selected.

Manual CI dispatch always starts a new GitHub Actions run for the selected
AtomGit PR head. Existing completed runs are not reused.

When CI is enabled, the workflow:

1. Mirrors the AtomGit PR head to `ConchSandbox/Conch` as `atomgit/pr-<number>`.
2. Dispatches the selected workflows in this repository. By default this is
   `build-and-check.yml`, `conch-init-smoke.yml`, and `e2b-workload-smoke.yml`.
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

The GitHub App installation must grant these repository permissions for
`ConchSandbox/Conch`:

- `Contents: Read and write`
- `Pull requests: Read and write`

When `atomgit_pr_number` is set, the sync workflow uses this repository's
`GITHUB_TOKEN` with `Actions: write` permission to dispatch and watch the local
CI workflows.
