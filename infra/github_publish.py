#!/usr/bin/env python3
"""Create the 3 private GitHub repositories and publish each standalone service repo.

Uses the GitHub REST & Git Data APIs (https://api.github.com) directly so that
publishing succeeds even when corporate proxies (e.g. Zscaler) block
`git-receive-pack` over https://github.com/*.git (HTTP 403).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

SERVICES = [
    {
        "name": "salescreate-api",
        "description": "Cloud Ops Agent Demo: Order creation microservice with injectable ECS fault scenarios",
        "commit_message": "Initial commit: salescreate-api microservice with 5 ECS fault scenarios",
    },
    {
        "name": "inventory-worker",
        "description": "Cloud Ops Agent Demo: Inventory reservation worker with injectable ECS fault scenarios",
        "commit_message": "Initial commit: inventory-worker microservice with 5 ECS fault scenarios",
    },
    {
        "name": "notifier",
        "description": "Cloud Ops Agent Demo: Multi-channel notification dispatcher with injectable ECS fault scenarios",
        "commit_message": "Initial commit: notifier microservice with 5 ECS fault scenarios",
    },
]


def get_token_from_gcm() -> str | None:
    """Retrieve the GitHub token stored in Git Credential Manager."""
    try:
        proc = subprocess.run(
            ["git", "credential-manager", "get"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def github_request(
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
) -> tuple[int, dict]:
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "cloud-ops-agent-publisher",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"message": body}
        return exc.code, parsed


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def ensure_repo_bootstrapped(login: str, repo_name: str, token: str, repo_dir: Path) -> str:
    """Ensure the repository has at least one commit on 'main' so the Git Data API works."""
    status, ref_data = github_request("GET", f"/repos/{login}/{repo_name}/git/ref/heads/main", token)
    if status == 200 and "object" in ref_data:
        return ref_data["object"]["sha"]

    # Empty repository: bootstrap via the Contents API using .gitignore
    gitignore_path = repo_dir / ".gitignore"
    content_bytes = gitignore_path.read_bytes() if gitignore_path.exists() else b"__pycache__/\n"
    put_status, put_resp = github_request(
        "PUT",
        f"/repos/{login}/{repo_name}/contents/.gitignore",
        token,
        payload={
            "message": "Bootstrap repository",
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": "main",
        },
    )
    if put_status not in (200, 201):
        raise RuntimeError(f"Failed to bootstrap {repo_name} (HTTP {put_status}): {put_resp}")
    return put_resp["commit"]["sha"]


def publish_tree_via_api(
    login: str,
    repo_name: str,
    token: str,
    repo_dir: Path,
    commit_message: str,
) -> tuple[str, list[str]]:
    """Publish all tracked files in `repo_dir` as a single commit on `main` via Git Data API."""
    base_sha = ensure_repo_bootstrapped(login, repo_name, token, repo_dir)

    tracked = run_git(["ls-files"], cwd=repo_dir).stdout.splitlines()
    tracked = [rel.strip() for rel in tracked if rel.strip()]

    tree_entries = []
    for rel_path in tracked:
        file_path = repo_dir / rel_path
        content_str = file_path.read_text(encoding="utf-8")
        tree_entries.append(
            {
                "path": rel_path.replace("\\", "/"),
                "mode": "100644",
                "type": "blob",
                "content": content_str,
            }
        )

    tree_status, tree_resp = github_request(
        "POST",
        f"/repos/{login}/{repo_name}/git/trees",
        token,
        payload={"tree": tree_entries},
    )
    if tree_status not in (200, 201):
        raise RuntimeError(f"Failed to create tree for {repo_name} (HTTP {tree_status}): {tree_resp}")

    new_tree_sha = tree_resp["sha"]

    # Create a root commit with the complete tree
    commit_status, commit_resp = github_request(
        "POST",
        f"/repos/{login}/{repo_name}/git/commits",
        token,
        payload={
            "message": commit_message,
            "tree": new_tree_sha,
            "parents": [],
        },
    )
    if commit_status not in (200, 201):
        raise RuntimeError(
            f"Failed to create commit for {repo_name} (HTTP {commit_status}): {commit_resp}"
        )

    new_commit_sha = commit_resp["sha"]

    # Point refs/heads/main to the new commit
    ref_status, ref_resp = github_request(
        "PATCH",
        f"/repos/{login}/{repo_name}/git/refs/heads/main",
        token,
        payload={"sha": new_commit_sha, "force": True},
    )
    if ref_status != 200:
        raise RuntimeError(
            f"Failed to update refs/heads/main for {repo_name} (HTTP {ref_status}): {ref_resp}"
        )

    return new_commit_sha, tracked


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create private GitHub repos and publish the 3 Cloud Ops Agent demo services."
    )
    parser.add_argument("--token", help="GitHub Personal Access Token (defaults to $GITHUB_TOKEN or GCM)")
    parser.add_argument("--owner", help="GitHub username or org override")
    parser.add_argument("--git-name", help="Repo-level git user.name override")
    parser.add_argument("--git-email", help="Repo-level git user.email override")
    parser.add_argument("--public", action="store_true", help="Create public repos instead of private")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without calling GitHub API")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    services_dir = root_dir / "services"

    if args.dry_run:
        owner = args.owner or "californiaAlmonds"
        print(f"[DRY-RUN] Target GitHub owner: {owner}")
        for svc in SERVICES:
            repo_dir = services_dir / svc["name"]
            print(f"[DRY-RUN] Would create private repo: https://github.com/{owner}/{svc['name']}.git")
            print(f"[DRY-RUN] Would publish local repo: {repo_dir} -> origin/main")
        return 0

    token = args.token or os.environ.get("GITHUB_TOKEN") or get_token_from_gcm()
    if not token:
        print("ERROR: No GitHub token found.", file=sys.stderr)
        return 1

    status, user_info = github_request("GET", "/user", token)
    if status != 200:
        print(
            f"ERROR: GitHub authentication failed (HTTP {status}): {user_info.get('message')}",
            file=sys.stderr,
        )
        return 1

    login = args.owner or user_info["login"]
    git_name = args.git_name or user_info.get("name") or login
    git_email = (
        args.git_email
        or user_info.get("email")
        or f"{user_info.get('id', '0')}+{login}@users.noreply.github.com"
    )
    print(f"Authenticated with GitHub as: {login} ({git_name} <{git_email}>)")

    for svc in SERVICES:
        name = svc["name"]
        repo_dir = services_dir / name
        if not (repo_dir / ".git").exists():
            print(f"ERROR: Local git repository not found at {repo_dir}", file=sys.stderr)
            return 1

        # 1. Create private repository if it doesn't already exist
        check_status, repo_meta = github_request("GET", f"/repos/{login}/{name}", token)
        if check_status == 200:
            visibility = "private" if repo_meta.get("private") else "public"
            print(f"Repo {login}/{name} exists ({visibility}): https://github.com/{login}/{name}")
        else:
            create_status, create_body = github_request(
                "POST",
                "/user/repos",
                token,
                payload={
                    "name": name,
                    "description": svc["description"],
                    "private": not args.public,
                    "auto_init": False,
                },
            )
            if create_status not in (200, 201):
                print(
                    f"ERROR creating repo {name} (HTTP {create_status}): {create_body}",
                    file=sys.stderr,
                )
                return 1
            visibility = "public" if args.public else "private"
            print(f"Created {visibility} repository: https://github.com/{login}/{name}")

        # 2. Set repo-level git identity and clean origin remote URL locally
        run_git(["config", "user.name", git_name], cwd=repo_dir)
        run_git(["config", "user.email", git_email], cwd=repo_dir)
        run_git(["commit", "--amend", "--reset-author", "--no-edit"], cwd=repo_dir)

        clean_url = f"https://github.com/{login}/{name}.git"
        remotes = run_git(["remote"], cwd=repo_dir).stdout.splitlines()
        if "origin" in remotes:
            run_git(["remote", "set-url", "origin", clean_url], cwd=repo_dir)
        else:
            run_git(["remote", "add", "origin", clean_url], cwd=repo_dir)

        # 3. Publish complete file tree via GitHub Git Data REST API
        commit_sha, files = publish_tree_via_api(
            login=login,
            repo_name=name,
            token=token,
            repo_dir=repo_dir,
            commit_message=svc["commit_message"],
        )
        print(
            f"Published {name} ({len(files)} files) -> https://github.com/{login}/{name} "
            f"(branch: main, commit: {commit_sha[:7]})"
        )

    print("\nAll 3 private repositories are created and published on GitHub!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
