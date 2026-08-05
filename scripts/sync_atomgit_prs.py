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
    dry_run: bool


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value or ""


def split_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    atomgit_owner = env("ATOMGIT_OWNER", "openeuler")
    atomgit_repo = env("ATOMGIT_REPO", "Conch")
    github_owner = env("GITHUB_TARGET_OWNER", "ConchSandbox")
    github_repo = env("GITHUB_TARGET_REPO_NAME", "Conch")
    dry_run = env_bool("DRY_RUN")
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
        base_branches=split_csv(env("MIRROR_BASE_BRANCHES", "dev")),
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


def build_body(config: Config, pr: dict[str, Any]) -> str:
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
    return source_body + "\n".join(mirror_note)


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
    for gh_pr in list_github_pull_requests(config, state="open"):
        marker = parse_marker(gh_pr.get("body"))
        if not marker:
            continue
        owner, repo, number = marker
        if owner == config.atomgit_owner and repo == config.atomgit_repo:
            mirrors[number] = gh_pr
    return mirrors


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
            "body": build_body(config, pr),
            "base": base,
        }
        if existing:
            gh_number = existing["number"]
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
        log("DRY_RUN: skipping GitHub API mutations and stale mirror closure")
        return 0

    existing_mirrors = find_existing_mirrors(config)
    upsert_github_prs(config, atomgit_prs, pushed_numbers, existing_mirrors)
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
