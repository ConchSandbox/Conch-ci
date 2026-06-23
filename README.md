# Conch CI

This repository owns the GitHub-side CI workflows for validating Conch source
refs, and it also mirrors AtomGit `openeuler/Conch` branches and pull requests
into GitHub `ConchSandbox/Conch`.

## CI workflows

The main CI workflows are manually dispatched and accept a Conch source
repository/ref pair:

- `build.yml`: build, static checks, Go tests, Go vet, and Python SDK import
  checks.
- `conch-agent-init-e2e.yml`: boots `conch-init` in a real
  cloud-hypervisor VM on a self-hosted runner.
- `e2b-rootfs-weekly.yml`: builds the E2B rootfs image into the self-hosted
  runner's local registry and updates the matching repository variable with
  the pushed digest.
- `e2e-ci.yml`: runs the Conch end-to-end CI entrypoint. It converts a
  prebuilt weekly E2B rootfs image by default, then runs the E2B SDK E2E job
  when `run_sdk_e2e` is not disabled.
- `startup-performance.yml`: manually benchmarks sandbox startup latency on a
  self-hosted runner using the weekly E2B rootfs image and a cached
  `conch convert --snapshot` image.

Common dispatch inputs:

```text
ci_marker=<external correlation marker>
conch_repository=<Conch source repository URL>
conch_ref=<Conch source ref to validate>
```

## Weekly E2B rootfs images

The `Weekly E2B Rootfs Image` workflow runs every Monday at 03:00 UTC. It
builds `examples/e2b-rootfs/Dockerfile` from the selected Conch source ref and
pushes it to the configured local registry:

```text
localhost:5000/conch/e2b-rootfs:weekly-<yyyymmdd>-<arch>
localhost:5000/conch/e2b-rootfs:weekly-latest-<arch>
```

After pushing, the workflow resolves the manifest digest and updates one of
these repository variables:

```text
CONCH_E2B_ROOTFS_IMAGE_AMD64=localhost:5000/conch/e2b-rootfs@sha256:<digest>
CONCH_E2B_ROOTFS_IMAGE_ARM64=localhost:5000/conch/e2b-rootfs@sha256:<digest>
```

`e2e-ci.yml` uses the repository variable matching the requested
`rootfs_platform`. A manual dispatch can override this with `rootfs_image`, or
can set `build_rootfs=true` to rebuild the rootfs image inside that E2E run.
The weekly source ref can be configured independently from the general CI ref
with `CONCH_E2B_ROOTFS_REPOSITORY` and `CONCH_E2B_ROOTFS_REF`.

The local registry is runner-local: `localhost:5000` in a workflow means the
self-hosted runner executing that job. Weekly rootfs builds and E2E runs must
therefore land on the same runner, or on runners that share the same registry
endpoint. The local registry action persists new registry containers under
`/opt/conch/registry` by default so digest variables survive registry container
restarts.

The end-to-end workflow also installs Conch's default CNI config from the
selected source ref into `/etc/conch/cni/net.d` and ensures the required CNI
plugins exist under `/opt/cni/bin`. The default plugin set is `bridge`,
`host-local`, and `loopback`.

## Startup performance benchmark

The `Conch Startup Performance CI` workflow is manually dispatched and runs on
a self-hosted Linux runner. It exposes one input:

```text
concurrency=100
```

`concurrency` controls only the snapshot concurrent startup scenario. It must
be a positive integer no greater than `1000`; single-start scenarios always run
5 iterations.

The workflow uses the weekly rootfs image configured in
`CONCH_E2B_ROOTFS_IMAGE_<ARCH>`, builds the Conch agent initramfs for the
selected Conch ref, and caches a snapshot image in the runner-local registry.
The snapshot cache key includes the Conch commit, rootfs manifest digest,
kernel digest, initrd digest, runner architecture, and snapshot format version.
Cache misses convert the weekly rootfs into a local Conch boot image, create a
preparation sandbox through the public SDK, export it as a snapshot image with
`conch snapshot export`, and push that image into the runner-local registry.
Cache hits skip that preparation. Every run still pulls/unpacks the selected
snapshot image into the isolated benchmark namespace before measuring.

Latency metrics include only Python-side timing around `Sandbox.create(...)`.
Rootfs build, image conversion, image pull/unpack, conchd startup, validation,
and cleanup are excluded. The first version reports slow latency without
threshold gates. Create failures, validation failures, cleanup failures,
preparation failures, pull/unpack failures, and invalid parameters fail the
job.

The workflow writes a GitHub job summary and uploads JSON, JSONL, preparation
metadata, SDK/conchd config copies, and conchd logs as
`conch-startup-benchmark-results` artifacts retained for 14 days.

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
run_agent_init_e2e=<checked by default>
run_e2e_ci=<checked by default>
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
   `build.yml`, `conch-agent-init-e2e.yml`, and `e2e-ci.yml`.
3. Waits for the GitHub Actions run to finish.
4. Updates the GitHub mirror pull request body with the CI result and run link.

`e2e-ci.yml` is named `Conch End-to-End CI` in the GitHub Actions UI. It
currently converts the configured weekly rootfs image and, by default, its
dependent SDK E2E job runs in the same workflow run.

The CI section is kept only for the same AtomGit head SHA. If the AtomGit PR is
updated, the next sync clears the old CI section until CI is run again for the
new head.

## Required secrets and permissions

`CONCH_SYNC_APP_PRIVATE_KEY` is required for normal mirroring and weekly rootfs
variable updates. The GitHub App must be installed on `ConchSandbox/Conch` and
`ConchSandbox/Conch-ci`.

The GitHub App installation must grant these repository permissions for
`ConchSandbox/Conch`:

- `Contents: Read and write`
- `Pull requests: Read and write`

The GitHub App installation must grant this repository permission for
`ConchSandbox/Conch-ci`:

- `Variables: Read and write`

When `atomgit_pr_number` is set, the sync workflow uses this repository's
`GITHUB_TOKEN` with `Actions: write` permission to dispatch and watch the local
CI workflows.

The weekly rootfs workflow generates a GitHub App installation token from
`CONCH_SYNC_APP_ID` and `CONCH_SYNC_APP_PRIVATE_KEY` to update
`CONCH_E2B_ROOTFS_IMAGE_<ARCH>` after a successful local registry push.
