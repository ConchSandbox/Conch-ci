# Conch CI

This repository owns the GitHub-side CI workflows for validating Conch source
refs, and it also mirrors AtomGit `openeuler/Conch` branches and pull requests
into GitHub `ConchSandbox/Conch`.

## CI workflows

The main CI workflows resolve the requested Conch ref once to a full commit
before any self-hosted job starts:

- `build-and-check.yml`: build, static checks, Go tests, Go vet, and Python SDK
  import checks.
- `network-pool-integration.yml`: privileged network-pool integration tests on
  the ARM64 self-hosted runner.
- `conch-init-smoke.yml`: boots `conch-init` as PID 1 in a real
  cloud-hypervisor VM, then verifies vsock readiness and SDK health.
- `e2b-template-weekly.yml`: builds or reuses the kernel and rootfs, publishes a
  content-addressed E2B Conch Template to the runner-local OCI registry, and can
  optionally mirror both images to GHCR on manual runs.
- `e2b-workload-smoke.yml`: consumes the same Template producer from the local
  registry, then separates E2B SDK/connectivity checks from sandbox
  network-policy and slot-reuse regressions.
- `prepare-self-hosted-runner.yml`: verifies or installs the locked runner
  environment, and applies reviewed environment changes merged to `main`.

The directly dispatched PR validation workflows accept:

```text
conch_repository=<Conch source repository URL>
conch_pr_number=<pull request number in the selected repository>
conch_ref=<Conch source ref to validate>
```

`conch_pr_number` is optional. When it is set, the workflow resolves the pull
request head from the selected repository and ignores `conch_ref`. GitHub uses
`refs/pull/<number>/head`, while AtomGit uses
`refs/merge-requests/<number>/head`.

`conch_repository` defaults to the trusted AtomGit upstream; the GitHub mirror
remains available as an explicit alternative:

```text
https://atomgit.com/openeuler/Conch.git
https://github.com/ConchSandbox/Conch.git
```

The selected repository is used both to resolve `conch_ref` and for every
subsequent immutable checkout. For example, an E2B run can fetch Conch entirely
from AtomGit with:

```bash
gh workflow run e2b-workload-smoke.yml --ref main \
  --field conch_repository=https://atomgit.com/openeuler/Conch.git \
  --field conch_pr_number=104 \
  --field run_sdk_smoke=true
```

Self-hosted jobs target the `taishan2280-oe2403sp3` runner through the
`self-hosted`, `Linux`, `ARM64`, and `Huawei` labels. Persistent dependencies
are declared only in [`runner-env.lock.yaml`](runner-env.lock.yaml) and
installed under `${RUNNER_TOOL_CACHE}/conch-ci`; host OS, KVM, Docker, sudo,
network, and compiler capabilities are checked but never installed by this
repository. Workflow-only dependencies live with their owning workflow under
`scripts/workflows/` and are installed only by that workflow.

The network-pool integration workflow builds and exercises `conchd` as a black
box without adding Go tests or build tags to Conch. A workflow-local script
runs isolated initial-prefill, continuous-refill, retry, cancellation, and
concurrent-close scenarios. It mounts job-local tmpfs instances over
`/run/conch` and `/var/lib/cni` inside a private mount and network namespace. A
workflow-local CNI wrapper injects deterministic ADD failures and blocking
operations while delegating successful operations to the locked bridge plugin.
The runner must provide `mount`, `umount`, and `unshare` in addition to the
existing sudo, `ip`, and `iptables` prerequisites.

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
selected source repository, Dockerfile path, and rootfs build script digest.
The source repository is also recorded in the OCI manifest annotations. The
action then combines that immutable rootfs, the kernel artifact, and an
initramfs produced by the shared `build-conch-initramfs` action into a native
Conch boot index and publishes it to a loopback-only OCI registry managed by the
runner environment. The Template build ID contains the rootfs reference, kernel
digest, Conch commit, and Template recipe digest.

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

The `Conch Sync` workflow synchronizes AtomGit branches and mirrored pull
requests. Scheduled and manual runs only perform synchronization; they do not
dispatch CI workflows.

The schedule currently runs at:

```text
0 2,8,14 * * *
```

## Running pull request CI

Run `build-and-check.yml`, `network-pool-integration.yml`,
`conch-init-smoke.yml`, or `e2b-workload-smoke.yml` directly from GitHub
Actions. Select the trusted GitHub or AtomGit repository and enter that
repository's pull request number. Each workflow resolves the PR head once to a
full commit before downstream self-hosted jobs start.

`e2b-workload-smoke.yml` is named `E2B SDK and Network Policy` in the GitHub
Actions UI. It pulls the immutable Template published by its producer job and
uses two explicitly named consumer jobs. `E2B SDK and Guest Connectivity` adds
the fixed nameserver `223.5.5.5` to its job-local Conch CNI configuration, then
accesses `https://example.com/` from the guest to validate DNS and Internet
connectivity together. The guest also opens a TCP connection to
`223.5.5.5:443` to validate the default route, bridge forwarding, and outbound
NAT independently of DNS. Finally, the runner connects to a one-shot TCP
listener inside the sandbox to validate runner-to-sandbox inbound connectivity.
This last check does not expose the sandbox for inbound connections from the
public Internet.

`Network Policy` runs after the SDK/connectivity job and owns the additional
privileged test setup. It assigns two
benchmark-network addresses to the runner loopback interface and verifies
creation-time policies, live replacement, inbound allow and deny rules,
disabling Internet access, and policy restoration after suspend/resume. It then
pauses background warm-pool
refill so a second sandbox deterministically reuses the first sandbox's network
slot. A bidirectional UDP flow with a fixed source port seeds conntrack before
the first sandbox is deleted; the replacement repeats the same tuple under a
matching `denyOut` rule to catch stale conntrack state bypassing the new policy.
On runners with active firewalld, the job temporarily assigns `cni-conch0` to
the runtime `trusted` zone so sandbox requests can reach the loopback test
server, then restores the interface's previous zone assignment. This job also
requires the host's `nsenter` command and conntrack procfs interface. Its
always-run cleanup releases the blocked refill, restores the firewall, removes
both sandbox IDs and loopback addresses, and tears down the isolated runtime.
Setting `run_sdk_smoke=false` skips sandbox operations in the SDK job and skips
the network-policy job.

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
