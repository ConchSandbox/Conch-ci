#!/usr/bin/env python3
"""Mirror open AtomGit pull requests into GitHub pull requests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


ATOMGIT_API_VERSION = "2023-02-21"
MARKER_RE = re.compile(r"<!--\s*atomgit-sync:\s*([^/\s]+)/([^#\s]+)#(\d+)\s*-->")
CI_SECTION_START = "<!-- conch-sync-ci-status:start -->"
CI_SECTION_END = "<!-- conch-sync-ci-status:end -->"
CI_SECTION_RE = re.compile(re.escape(CI_SECTION_START) + r".*?" + re.escape(CI_SECTION_END), re.DOTALL)
CI_HEAD_RE = re.compile(r"<!--\s*conch-sync-ci-head:\s*([0-9a-fA-F]{40})\s*-->")
API_RETRY_STATUS_CODES = {500, 502, 503, 504}
API_RETRY_DELAYS_SECONDS = (2, 4, 8)


@dataclass(frozen=True)
class Config:
    atomgit_api_base: str
    atomgit_owner: str
    atomgit_repo: str
    atomgit_token: str | None
    source_repo: str
    github_api_base: str
    github_owner: str
    github_repo: str
    github_token: str
    target_repo: str
    branch_prefix: str
    base_branches: set[str]
    ci_enabled: bool
    ci_github_owner: str
    ci_github_repo: str
    ci_github_token: str | None
    ci_github_ref: str
    ci_workflows: list[str]
    ci_conch_repository: str
    ci_timeout_seconds: int
    ci_poll_interval_seconds: int
    ci_wait_for_completion: bool
    ci_rerun_existing_statuses: bool
    ci_atomgit_pr_numbers: set[int]
    dry_run: bool


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value or ""


def split_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def split_csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def optional_int_set(name: str, value: str) -> set[int]:
    item = value.strip()
    if not item:
        return set()
    if not re.fullmatch(r"\d+", item):
        raise SystemExit(f"{name} must be one positive integer PR number, got {value!r}")
    number = int(item)
    if number <= 0:
        raise SystemExit(f"{name} must be one positive integer PR number, got {value!r}")
    return {number}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


def load_config() -> Config:
    atomgit_owner = env("ATOMGIT_OWNER", "openeuler")
    atomgit_repo = env("ATOMGIT_REPO", "Conch")
    github_owner = env("GITHUB_TARGET_OWNER", "ConchSandbox")
    github_repo = env("GITHUB_TARGET_REPO_NAME", "Conch")
    ci_github_owner = env("CI_GITHUB_OWNER", "ConchSandbox")
    ci_github_repo = env("CI_GITHUB_REPO", "Conch-ci")
    dry_run = env_bool("DRY_RUN")
    ci_workflows = split_csv_list(env("CI_WORKFLOWS", "build.yml"))
    return Config(
        atomgit_api_base=env("ATOMGIT_API_BASE", "https://api.atomgit.com/api/v5").rstrip("/"),
        atomgit_owner=atomgit_owner,
        atomgit_repo=atomgit_repo,
        atomgit_token=env("ATOMGIT_TOKEN") or None,
        source_repo=env("ATOMGIT_SOURCE_REPO", f"https://atomgit.com/{atomgit_owner}/{atomgit_repo}.git"),
        github_api_base=env("GITHUB_API_BASE", "https://api.github.com").rstrip("/"),
        github_owner=github_owner,
        github_repo=github_repo,
        github_token=env("GITHUB_TOKEN", required=not dry_run),
        target_repo=env("GITHUB_TARGET_REPO", f"github.com/{github_owner}/{github_repo}.git"),
        branch_prefix=env("MIRROR_BRANCH_PREFIX", "atomgit/pr-"),
        base_branches=split_csv(env("MIRROR_BASE_BRANCHES", "dev,dev-cri")),
        ci_enabled=env_bool("CI_ENABLED"),
        ci_github_owner=ci_github_owner,
        ci_github_repo=ci_github_repo,
        ci_github_token=env("CI_GITHUB_TOKEN") or None,
        ci_github_ref=env("CI_GITHUB_REF", "main"),
        ci_workflows=ci_workflows,
        ci_conch_repository=env(
            "CI_CONCH_REPOSITORY",
            f"https://github.com/{github_owner}/{github_repo}.git",
        ),
        ci_timeout_seconds=env_int("CI_TIMEOUT_SECONDS", 3 * 60 * 60),
        ci_poll_interval_seconds=env_int("CI_POLL_INTERVAL_SECONDS", 30),
        ci_wait_for_completion=env_bool("CI_WAIT_FOR_COMPLETION"),
        ci_rerun_existing_statuses=env_bool("CI_RERUN_EXISTING_STATUSES"),
        ci_atomgit_pr_numbers=optional_int_set("CI_ATOMGIT_PR_NUMBER", env("CI_ATOMGIT_PR_NUMBER")),
        dry_run=dry_run,
    )


def log(message: str) -> None:
    print(message, flush=True)


def run_git(args: list[str], cwd: str, redact: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    command_for_error = "<redacted>" if redact else " ".join(args)
    if redact:
        log("$ git <redacted>")
    else:
        log("$ git " + " ".join(args))
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {command_for_error} failed with exit code {proc.returncode}")
    return proc


def api_request(
    method: str,
    url: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    body = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "Conch-ci-sync",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)

    for attempt in range(len(API_RETRY_DELAYS_SECONDS) + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read()
                if not content:
                    return None
                return json.loads(content.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code not in API_RETRY_STATUS_CODES or attempt == len(API_RETRY_DELAYS_SECONDS):
                raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
            delay = API_RETRY_DELAYS_SECONDS[attempt]
            log(f"{method} {url} failed with HTTP {exc.code}; retrying in {delay}s")
            time.sleep(delay)
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt == len(API_RETRY_DELAYS_SECONDS):
                raise RuntimeError(f"{method} {url} failed: {exc}") from exc
            delay = API_RETRY_DELAYS_SECONDS[attempt]
            log(f"{method} {url} failed with transient network error; retrying in {delay}s")
            time.sleep(delay)

    raise AssertionError("unreachable")


def atomgit_request(
    config: Config,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = f"{config.atomgit_api_base}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return api_request(
        method,
        url,
        token=config.atomgit_token,
        payload=payload,
        extra_headers={"X-Api-Version": ATOMGIT_API_VERSION},
    )


def github_request(
    config: Config,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> Any:
    return api_request(
        method,
        f"{config.github_api_base}{path}",
        token=token or config.github_token,
        payload=payload,
        extra_headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def quote_path(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def list_atomgit_open_prs(config: Config) -> list[dict[str, Any]]:
    prs: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    while True:
        chunk = atomgit_request(
            config,
            "GET",
            f"/repos/{config.atomgit_owner}/{config.atomgit_repo}/pulls",
            {"state": "open", "per_page": str(per_page), "page": str(page)},
        )
        if not isinstance(chunk, list):
            raise RuntimeError(f"unexpected AtomGit pull list response: {chunk!r}")
        prs.extend(chunk)
        if len(chunk) < per_page:
            break
        page += 1
    return prs


def list_github_pull_requests(config: Config, state: str = "all") -> list[dict[str, Any]]:
    prs: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    while True:
        query = urllib.parse.urlencode({"state": state, "per_page": per_page, "page": page})
        chunk = github_request(config, "GET", f"/repos/{config.github_owner}/{config.github_repo}/pulls?{query}")
        if not isinstance(chunk, list):
            raise RuntimeError(f"unexpected GitHub pull list response: {chunk!r}")
        prs.extend(chunk)
        if len(chunk) < per_page:
            break
        page += 1
    return prs


def list_github_branches(config: Config) -> list[str]:
    branches: list[str] = []
    page = 1
    per_page = 100
    while True:
        query = urllib.parse.urlencode({"per_page": per_page, "page": page})
        chunk = github_request(config, "GET", f"/repos/{config.github_owner}/{config.github_repo}/branches?{query}")
        if not isinstance(chunk, list):
            raise RuntimeError(f"unexpected GitHub branch list response: {chunk!r}")
        for branch in chunk:
            name = branch.get("name") if isinstance(branch, dict) else None
            if isinstance(name, str):
                branches.append(name)
        if len(chunk) < per_page:
            break
        page += 1
    return branches


def list_github_workflow_runs(config: Config, workflow: str, branch: str | None = None) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    while True:
        params: dict[str, str | int] = {"event": "workflow_dispatch", "per_page": per_page, "page": page}
        if branch:
            params["branch"] = branch
        query = urllib.parse.urlencode(params)
        workflow_id = quote_path(workflow)
        chunk = github_request(
            config,
            "GET",
            f"/repos/{config.ci_github_owner}/{config.ci_github_repo}/actions/workflows/{workflow_id}/runs?{query}",
            token=config.ci_github_token,
        )
        workflow_runs = chunk.get("workflow_runs") if isinstance(chunk, dict) else None
        if not isinstance(workflow_runs, list):
            raise RuntimeError(f"unexpected GitHub workflow run list response: {chunk!r}")
        runs.extend(workflow_runs)
        if len(workflow_runs) < per_page:
            break
        page += 1
        if page > 3:
            break
    return runs


def dispatch_github_workflow(
    config: Config,
    workflow: str,
    marker: str,
    conch_ref: str,
) -> None:
    workflow_id = quote_path(workflow)
    payload = {
        "ref": config.ci_github_ref,
        "inputs": {
            "ci_marker": marker,
            "conch_repository": config.ci_conch_repository,
            "conch_ref": conch_ref,
        },
    }
    github_request(
        config,
        "POST",
        f"/repos/{config.ci_github_owner}/{config.ci_github_repo}/actions/workflows/{workflow_id}/dispatches",
        payload,
        token=config.ci_github_token,
    )


def find_workflow_run(config: Config, workflow: str, marker: str) -> dict[str, Any] | None:
    for run in list_github_workflow_runs(config, workflow, branch=config.ci_github_ref):
        if str(run.get("display_title") or "") == marker:
            return run
    return None


def wait_for_workflow_run(config: Config, workflow: str, marker: str) -> dict[str, Any]:
    deadline = time.time() + config.ci_timeout_seconds
    last_status = "not-created"
    while time.time() < deadline:
        run = find_workflow_run(config, workflow, marker)
        if run:
            status = str(run.get("status") or "")
            conclusion = str(run.get("conclusion") or "")
            html_url = str(run.get("html_url") or "")
            state = f"{status}/{conclusion or 'none'}"
            if state != last_status:
                log(f"GitHub workflow {workflow} marker {marker}: {state} {html_url}")
                last_status = state
            if status == "completed":
                return run
        else:
            if last_status != "not-created":
                log(f"GitHub workflow {workflow} marker {marker}: waiting for run creation")
                last_status = "not-created"
        time.sleep(config.ci_poll_interval_seconds)
    raise TimeoutError(f"timed out waiting for GitHub workflow {workflow} marker {marker}")


def workflow_run_description(run: dict[str, Any]) -> str:
    name = str(run.get("name") or "GitHub Actions")
    conclusion = str(run.get("conclusion") or "unknown")
    return f"{name}: {conclusion}"


def workflow_target_url(config: Config, workflow: str) -> str:
    return f"https://github.com/{config.ci_github_owner}/{config.ci_github_repo}/actions/workflows/{workflow}"


def ci_marker(config: Config, pr: dict[str, Any], workflow: str) -> str:
    number = pr_number(pr)
    head_sha = pr_head_sha(pr)
    safe_workflow = workflow.replace("/", "-")
    return f"atomgit-{config.atomgit_owner}-{config.atomgit_repo}-pr-{number}-{head_sha[:12]}-{safe_workflow}"


def strip_ci_section(body: str) -> str:
    return CI_SECTION_RE.sub("", body).rstrip()


def extract_ci_section(body: str | None, head_sha: str) -> str | None:
    if not body:
        return None
    match = CI_SECTION_RE.search(body)
    if not match:
        return None
    section = match.group(0).strip()
    head_match = CI_HEAD_RE.search(section)
    if not head_match or head_match.group(1).lower() != head_sha.lower():
        return None
    return section


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def build_ci_section(pr: dict[str, Any], results: list[dict[str, Any]]) -> str:
    head_sha = pr_head_sha(pr)
    lines = [
        CI_SECTION_START,
        f"<!-- conch-sync-ci-head: {head_sha} -->",
        "",
        "## GitHub CI",
        "",
        "| Workflow | State | Details |",
        "| --- | --- | --- |",
    ]
    for result in results:
        workflow = markdown_cell(str(result["workflow"]))
        state = markdown_cell(str(result["state"]))
        url = str(result["url"])
        description = markdown_cell(str(result["description"]))
        lines.append(f"| {workflow} | {state} | [{description}]({url}) |")
    lines.extend(["", CI_SECTION_END])
    return "\n".join(lines)


def update_github_pr_ci_status(
    config: Config,
    pr: dict[str, Any],
    gh_pr: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    gh_number = int(gh_pr["number"])
    body = strip_ci_section(str(gh_pr.get("body") or build_body(config, pr)))
    new_body = body + "\n\n" + build_ci_section(pr, results)
    if config.dry_run:
        log(f"DRY_RUN: would update GitHub mirror PR #{gh_number} CI status")
        return
    log(f"Updating GitHub mirror PR #{gh_number} CI status")
    updated = github_request(
        config,
        "PATCH",
        f"/repos/{config.github_owner}/{config.github_repo}/pulls/{gh_number}",
        {"body": new_body},
    )
    if isinstance(updated, dict):
        gh_pr.update(updated)
    else:
        gh_pr["body"] = new_body


def run_ci_for_pr(config: Config, pr: dict[str, Any], gh_pr: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    pending_runs: list[tuple[str, str, dict[str, Any] | None, dict[str, Any]]] = []
    number = pr_number(pr)
    branch = mirror_branch(config, number)
    for workflow in config.ci_workflows:
        marker = ci_marker(config, pr, workflow)
        existing = None if config.dry_run or config.ci_rerun_existing_statuses else find_workflow_run(config, workflow, marker)
        if existing and str(existing.get("status") or "") == "completed":
            log(f"Reusing completed GitHub workflow run for AtomGit PR #{number}: {workflow} {existing.get('html_url')}")
            results.append(
                {
                    "workflow": workflow,
                    "state": str(existing.get("conclusion") or "unknown"),
                    "url": str(existing.get("html_url") or workflow_target_url(config, workflow)),
                    "description": workflow_run_description(existing),
                }
            )
            update_github_pr_ci_status(config, pr, gh_pr, results)
            continue

        target_url = str((existing or {}).get("html_url") or workflow_target_url(config, workflow))
        result = {
            "workflow": workflow,
            "state": "pending",
            "url": target_url,
            "description": f"{workflow} queued on GitHub Actions",
        }
        results.append(result)
        update_github_pr_ci_status(config, pr, gh_pr, results)
        if config.dry_run:
            log(f"DRY_RUN: would dispatch {workflow} for AtomGit PR #{number} using {branch}")
            if config.ci_wait_for_completion:
                log(f"DRY_RUN: would wait for {workflow} completion and update GitHub mirror PR CI status")
            result["description"] = "dry run"
            continue
        if not existing:
            log(f"Dispatching GitHub workflow {workflow} for AtomGit PR #{number} using {branch}")
            dispatch_github_workflow(config, workflow, marker, branch)
            result["url"] = workflow_target_url(config, workflow)
        else:
            log(f"Found existing GitHub workflow run for AtomGit PR #{number}: {workflow} {target_url}")
        if config.ci_wait_for_completion:
            pending_runs.append((workflow, marker, existing, result))
        else:
            result["description"] = f"{workflow} has not completed"

    for workflow, marker, existing, result in pending_runs:
        run = existing or wait_for_workflow_run(config, workflow, marker)
        if str(run.get("status") or "") != "completed":
            run = wait_for_workflow_run(config, workflow, marker)
        result.update(
            {
                "workflow": workflow,
                "state": str(run.get("conclusion") or "unknown"),
                "url": str(run.get("html_url") or workflow_target_url(config, workflow)),
                "description": workflow_run_description(run),
            }
        )
        update_github_pr_ci_status(config, pr, gh_pr, results)
    return results


def run_ci_for_mirrored_prs(
    config: Config,
    atomgit_prs: list[dict[str, Any]],
    pushed_numbers: set[int],
    existing_mirrors: dict[int, dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    if not config.ci_enabled:
        log("CI bridge is disabled; set CI_ENABLED=1 to dispatch GitHub workflows")
        return {}
    if not config.ci_workflows:
        log("CI bridge is enabled but CI_WORKFLOWS is empty")
        return {}
    if not config.ci_github_token and not config.dry_run:
        raise RuntimeError("CI bridge requires CI_GITHUB_TOKEN to dispatch and watch CI workflows")

    results: dict[int, list[dict[str, Any]]] = {}
    for pr in atomgit_prs:
        number = pr_number(pr)
        if number not in pushed_numbers:
            continue
        if config.ci_atomgit_pr_numbers and number not in config.ci_atomgit_pr_numbers:
            log(f"Skipping CI for AtomGit PR #{number}: not selected by CI_ATOMGIT_PR_NUMBER")
            continue
        gh_pr = existing_mirrors.get(number)
        if not gh_pr:
            raise RuntimeError(f"GitHub mirror PR for AtomGit PR #{number} was not found")
        results[number] = run_ci_for_pr(config, pr, gh_pr)

    if config.ci_atomgit_pr_numbers:
        missing = config.ci_atomgit_pr_numbers - results.keys()
        if missing:
            missing_list = ", ".join(map(str, sorted(missing)))
            raise RuntimeError(f"selected AtomGit PR(s) were not mirrored for CI: {missing_list}")
    return results


def atomgit_marker(config: Config, number: int) -> str:
    return f"<!-- atomgit-sync: {config.atomgit_owner}/{config.atomgit_repo}#{number} -->"


def parse_marker(body: str | None) -> tuple[str, str, int] | None:
    if not body:
        return None
    match = MARKER_RE.search(body)
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def mirror_branch(config: Config, number: int) -> str:
    return f"{config.branch_prefix}{number}"


def mirror_branch_number(config: Config, branch: str) -> int | None:
    if not branch.startswith(config.branch_prefix):
        return None
    suffix = branch[len(config.branch_prefix) :]
    if not suffix.isdigit():
        return None
    return int(suffix)


def pr_number(pr: dict[str, Any]) -> int:
    return int(pr["number"])


def pr_base_ref(pr: dict[str, Any]) -> str:
    return str(pr.get("base", {}).get("ref", "")).strip()


def pr_head_sha(pr: dict[str, Any]) -> str:
    return str(pr.get("head", {}).get("sha", "")).strip()


def pr_head_label(pr: dict[str, Any]) -> str:
    return str(pr.get("head", {}).get("label", "")).strip()


def pr_source_url(pr: dict[str, Any]) -> str:
    return str(pr.get("html_url", "")).strip()


def build_body(config: Config, pr: dict[str, Any], existing_body: str | None = None) -> str:
    number = pr_number(pr)
    source_body = str(pr.get("body") or "").rstrip()
    source_url = pr_source_url(pr)
    head_sha = pr_head_sha(pr)
    head_label = pr_head_label(pr)
    base_ref = pr_base_ref(pr)

    mirror_note = [
        "",
        "---",
        f"Mirrored from AtomGit PR [{config.atomgit_owner}/{config.atomgit_repo}#{number}]({source_url}).",
        f"- AtomGit head: `{head_label}` (`{head_sha[:12]}`)",
        f"- AtomGit base: `{base_ref}`",
        "",
        atomgit_marker(config, number),
    ]
    body = source_body + "\n".join(mirror_note)
    ci_section = extract_ci_section(existing_body, head_sha)
    if ci_section:
        body += "\n\n" + ci_section
    return body


def validate_ref_name(ref_name: str) -> bool:
    proc = subprocess.run(
        ["git", "check-ref-format", "--branch", ref_name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode == 0


def mirror_branches(config: Config, atomgit_prs: list[dict[str, Any]]) -> set[int]:
    mirrored: set[int] = set()
    with tempfile.TemporaryDirectory() as workdir:
        repo = os.path.join(workdir, "repo.git")
        run_git(["init", "--bare", "-b", "main", repo], cwd=workdir)
        run_git(["remote", "add", "source", config.source_repo], cwd=repo)
        if not config.dry_run:
            target_url = f"https://x-access-token:{config.github_token}@{config.target_repo}"
            run_git(["remote", "add", "target", target_url], cwd=repo, redact=True)

        for pr in atomgit_prs:
            number = pr_number(pr)
            base = pr_base_ref(pr)
            branch = mirror_branch(config, number)
            if config.base_branches and base not in config.base_branches:
                log(f"Skipping AtomGit PR #{number}: base branch {base!r} is not mirrored")
                continue
            if not validate_ref_name(branch):
                log(f"Skipping AtomGit PR #{number}: invalid mirror branch {branch!r}")
                continue

            log(f"Fetching AtomGit PR #{number} head into {branch}")
            run_git(
                ["fetch", "--no-tags", "source", f"+refs/merge-requests/{number}/head:refs/heads/{branch}"],
                cwd=repo,
            )

            if config.dry_run:
                log(f"DRY_RUN: would push AtomGit PR #{number} head to GitHub branch {branch}")
            else:
                log(f"Pushing AtomGit PR #{number} head to GitHub branch {branch}")
                run_git(["push", "target", f"+refs/heads/{branch}:refs/heads/{branch}"], cwd=repo)
            mirrored.add(number)

        return mirrored


def find_existing_mirrors(config: Config) -> dict[int, dict[str, Any]]:
    mirrors: dict[int, dict[str, Any]] = {}
    for gh_pr in list_github_pull_requests(config, state="all"):
        marker = parse_marker(gh_pr.get("body"))
        if not marker:
            continue
        owner, repo, number = marker
        if owner == config.atomgit_owner and repo == config.atomgit_repo:
            mirrors[number] = gh_pr
    return mirrors


def ensure_github_pr_open(config: Config, gh_pr: dict[str, Any], atomgit_number: int) -> None:
    if gh_pr.get("state") == "open":
        return

    gh_number = gh_pr["number"]
    if gh_pr.get("merged_at"):
        raise RuntimeError(
            f"GitHub mirror PR #{gh_number} for AtomGit PR #{atomgit_number} is already merged; "
            "cannot reopen it for sync"
        )

    log(f"Reopening GitHub mirror PR #{gh_number} for AtomGit PR #{atomgit_number}")
    github_request(
        config,
        "PATCH",
        f"/repos/{config.github_owner}/{config.github_repo}/pulls/{gh_number}",
        {"state": "open"},
    )


def upsert_github_prs(
    config: Config,
    atomgit_prs: list[dict[str, Any]],
    pushed_numbers: set[int],
    existing_mirrors: dict[int, dict[str, Any]],
) -> None:
    for pr in atomgit_prs:
        number = pr_number(pr)
        if number not in pushed_numbers:
            continue

        base = pr_base_ref(pr)
        branch = mirror_branch(config, number)
        existing = existing_mirrors.get(number)
        payload = {
            "title": str(pr.get("title") or f"AtomGit PR #{number}"),
            "body": build_body(config, pr, str((existing or {}).get("body") or "")),
            "base": base,
        }
        if existing:
            gh_number = existing["number"]
            ensure_github_pr_open(config, existing, number)
            log(f"Updating GitHub mirror PR #{gh_number} for AtomGit PR #{number}")
            github_request(
                config,
                "PATCH",
                f"/repos/{config.github_owner}/{config.github_repo}/pulls/{gh_number}",
                payload,
            )
            continue

        payload.update(
            {
                "head": branch,
                "draft": bool(pr.get("draft", False)),
            }
        )
        log(f"Creating GitHub mirror PR for AtomGit PR #{number}")
        github_request(config, "POST", f"/repos/{config.github_owner}/{config.github_repo}/pulls", payload)


def close_stale_github_prs(
    config: Config,
    desired_numbers: set[int],
    existing_mirrors: dict[int, dict[str, Any]],
) -> None:
    for atomgit_number, gh_pr in sorted(existing_mirrors.items()):
        if atomgit_number in desired_numbers or gh_pr.get("state") != "open":
            continue
        gh_number = gh_pr["number"]
        log(f"Closing stale GitHub mirror PR #{gh_number} for AtomGit PR #{atomgit_number}")
        github_request(
            config,
            "PATCH",
            f"/repos/{config.github_owner}/{config.github_repo}/pulls/{gh_number}",
            {"state": "closed"},
        )


def delete_stale_mirror_branches(config: Config, desired_numbers: set[int]) -> None:
    stale_branches = []
    for branch in list_github_branches(config):
        number = mirror_branch_number(config, branch)
        if number is not None and number not in desired_numbers:
            stale_branches.append(branch)

    if not stale_branches:
        return

    with tempfile.TemporaryDirectory() as workdir:
        repo = os.path.join(workdir, "repo.git")
        run_git(["init", "--bare", "-b", "main", repo], cwd=workdir)
        target_url = f"https://x-access-token:{config.github_token}@{config.target_repo}"
        run_git(["remote", "add", "target", target_url], cwd=repo, redact=True)

        for branch in sorted(stale_branches, key=lambda name: mirror_branch_number(config, name) or -1):
            log(f"Deleting stale GitHub mirror branch {branch}")
            run_git(["push", "target", f":refs/heads/{branch}"], cwd=repo)


def main() -> int:
    config = load_config()
    log(f"Listing open AtomGit PRs for {config.atomgit_owner}/{config.atomgit_repo}")
    atomgit_prs = list_atomgit_open_prs(config)
    open_numbers = {pr_number(pr) for pr in atomgit_prs}
    log(f"Found {len(atomgit_prs)} open AtomGit PR(s): {', '.join(map(str, sorted(open_numbers))) or 'none'}")

    pushed_numbers = mirror_branches(config, atomgit_prs)
    if config.dry_run:
        log(f"DRY_RUN: would upsert GitHub mirrors for AtomGit PR(s): {', '.join(map(str, sorted(pushed_numbers))) or 'none'}")
        dry_run_mirrors = {
            pr_number(pr): {"number": pr_number(pr), "body": build_body(config, pr)}
            for pr in atomgit_prs
            if pr_number(pr) in pushed_numbers
        }
        run_ci_for_mirrored_prs(config, atomgit_prs, pushed_numbers, dry_run_mirrors)
        log("DRY_RUN: skipping GitHub API mutations and stale mirror closure")
        return 0

    existing_mirrors = find_existing_mirrors(config)
    upsert_github_prs(config, atomgit_prs, pushed_numbers, existing_mirrors)
    existing_mirrors = find_existing_mirrors(config)
    run_ci_for_mirrored_prs(config, atomgit_prs, pushed_numbers, existing_mirrors)
    close_stale_github_prs(config, pushed_numbers, existing_mirrors)
    delete_stale_mirror_branches(config, pushed_numbers)
    log("AtomGit PR mirror sync complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
