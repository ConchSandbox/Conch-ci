# Conch CI

This repository owns the GitHub-side CI workflows for validating Conch source
refs, and it also mirrors AtomGit `openeuler/Conch` branches and pull requests
into GitHub `ConchSandbox/Conch`.

## CI workflows

The main CI workflows are manually dispatched and accept a Conch source
repository/ref pair:

- `build.yml`: build, static checks, Go tests, Go vet, and Python SDK import
  checks.
- `conch-agent-init-e2e.yml`: boots `conch-agent --init` in a real
  cloud-hypervisor VM on a self-hosted runner.
- `e2e-ci.yml`: runs the Conch end-to-end CI entrypoint. It currently builds
  and converts the E2B image, then runs the E2B SDK E2E job when `run_sdk_e2e`
  is not disabled.

Common dispatch inputs:

```text
ci_marker=<external correlation marker>
conch_repository=<Conch source repository URL>
conch_ref=<Conch source ref to validate>
```

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
currently runs the existing image build job and, by default, its dependent SDK
E2E job in the same workflow run.

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
