#!/usr/bin/env python3
"""Constrained GitHub App client for VirtuaJimmy's pull-request workflow."""

import argparse
import base64
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

API_VERSION = "2026-03-10"
TOKEN_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
}
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^(?![-/.])(?!.*(?:\.\.|//|@\{|\\))(?!.*[/.]$)[A-Za-z0-9._/-]+$")
COMMENT_MARKER = "<!-- virtuajimmy-github-app -->"


class AppError(RuntimeError):
    """Safe, credential-free error suitable for stderr."""


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def load_config(path):
    config_path = pathlib.Path(path)
    try:
        config_mode = stat.S_IMODE(config_path.stat().st_mode)
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AppError(f"cannot read valid configuration: {exc}") from None
    if config_mode & 0o077:
        raise AppError("configuration file must not be accessible by group or others")

    required = {"api_url", "app_id", "installation_id", "private_key_file",
                "allowed_repositories"}
    if set(config) != required:
        raise AppError("configuration has missing or unsupported keys")
    if not str(config["app_id"]).isdigit() or int(config["app_id"]) < 1:
        raise AppError("app_id must be a positive integer")
    if (not str(config["installation_id"]).isdigit()
            or int(config["installation_id"]) < 1):
        raise AppError("installation_id must be a positive integer")
    allowed = config["allowed_repositories"]
    if not isinstance(allowed, list) or not allowed:
        raise AppError("allowed_repositories must contain unique owner/repository names")
    if (any(not isinstance(repo, str) or not REPOSITORY_RE.fullmatch(repo)
            for repo in allowed)
            or len(set(allowed)) != len(allowed)):
        raise AppError("allowed_repositories must contain unique owner/repository names")
    if config["api_url"] != "https://api.github.com":
        raise AppError("only the public GitHub API endpoint is approved")
    assert_private_file(config["private_key_file"])
    return config


def assert_private_file(path):
    key_path = pathlib.Path(path)
    try:
        file_stat = key_path.stat()
    except OSError as exc:
        raise AppError(f"cannot access private key file: {exc}") from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise AppError("private key path is not a regular file")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise AppError("private key file must not be accessible by group or others")


def mint_jwt(config, now=None):
    issued = int(time.time() if now is None else now)
    header = b64url(json.dumps(
        {"alg": "RS256", "typ": "JWT"}, separators=(",", ":")
    ).encode())
    payload = b64url(json.dumps({
        "iat": issued - 60,
        "exp": issued + 540,
        "iss": str(config["app_id"]),
    }, separators=(",", ":")).encode())
    unsigned = f"{header}.{payload}".encode("ascii")
    try:
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", config["private_key_file"]],
            input=unsigned,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise AppError(f"cannot execute openssl: {exc}") from None
    if signed.returncode != 0:
        raise AppError("OpenSSL could not sign the GitHub App JWT")
    return f"{unsigned.decode('ascii')}.{b64url(signed.stdout)}"


def api_request(config, method, path, bearer, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        config["api_url"] + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "VirtuaJimmy-GitHub-App",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise AppError(f"GitHub API returned HTTP {exc.code} for {method} {path}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise AppError(f"GitHub API request failed for {method} {path}: {exc}") from None


def installation_token(config, repository):
    jwt = mint_jwt(config)
    repo_name = repository.split("/", 1)[1]
    response = api_request(
        config,
        "POST",
        f"/app/installations/{config['installation_id']}/access_tokens",
        jwt,
        {"repositories": [repo_name], "permissions": TOKEN_PERMISSIONS},
    )
    token = response.get("token")
    if not isinstance(token, str) or not token:
        raise AppError("GitHub did not return an installation token")
    return token


def validate_repo(config, repository):
    if not REPOSITORY_RE.fullmatch(repository):
        raise AppError("repository must use owner/repository syntax")
    if repository not in config["allowed_repositories"]:
        raise AppError(f"repository is not allowed: {repository}")


def validate_ref(value, label):
    if not REF_RE.fullmatch(value):
        raise AppError(f"{label} is not a safe Git reference")


def push_branch(config, args):
    validate_ref(args.branch, "branch")
    validate_ref(args.source, "source")
    token = installation_token(config, args.repo)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    with tempfile.TemporaryDirectory(prefix="vj-github-") as directory:
        git_config = pathlib.Path(directory) / "gitconfig"
        git_config.write_text(
            "[credential]\n"
            "\thelper =\n"
            '[http "https://github.com/"]\n'
            f"\textraHeader = Authorization: Basic {basic}\n",
            encoding="utf-8",
        )
        git_config.chmod(0o600)
        env = os.environ.copy()
        env.update({
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        result = subprocess.run(
            [
                "git", "push", "--porcelain",
                f"https://github.com/{args.repo}.git",
                f"{args.source}:refs/heads/{args.branch}",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        raise AppError("git push failed; inspect the local repository and branch")
    return {"operation": "push-branch", "repository": args.repo, "branch": args.branch}


def open_pr(config, args):
    validate_ref(args.head, "head")
    validate_ref(args.base, "base")
    token = installation_token(config, args.repo)
    response = api_request(
        config,
        "POST",
        f"/repos/{args.repo}/pulls",
        token,
        {
            "title": args.title,
            "head": args.head,
            "base": args.base,
            "body": args.body,
            "draft": args.draft,
        },
    )
    return {
        "operation": "open-pr",
        "repository": args.repo,
        "number": response.get("number"),
        "url": response.get("html_url"),
    }


def comment(config, args):
    token = installation_token(config, args.repo)
    issue = api_request(
        config, "GET", f"/repos/{args.repo}/issues/{args.pr}", token
    )
    if "pull_request" not in issue:
        raise AppError(f"#{args.pr} is an issue, not a pull request")
    body = args.body
    if COMMENT_MARKER not in body:
        body = f"{body.rstrip()}\n\n{COMMENT_MARKER}"
    response = api_request(
        config,
        "POST",
        f"/repos/{args.repo}/issues/{args.pr}/comments",
        token,
        {"body": body},
    )
    return {
        "operation": "comment",
        "repository": args.repo,
        "pull_request": args.pr,
        "url": response.get("html_url"),
    }


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--config", required=True)
    commands = root.add_subparsers(dest="operation", required=True)

    push = commands.add_parser("push-branch")
    push.add_argument("--repo", required=True)
    push.add_argument("--branch", required=True)
    push.add_argument("--source", default="HEAD")
    push.set_defaults(handler=push_branch)

    pull = commands.add_parser("open-pr")
    pull.add_argument("--repo", required=True)
    pull.add_argument("--head", required=True)
    pull.add_argument("--base", default="main")
    pull.add_argument("--title", required=True)
    pull.add_argument("--body", required=True)
    pull.add_argument("--draft", action="store_true")
    pull.set_defaults(handler=open_pr)

    note = commands.add_parser("comment")
    note.add_argument("--repo", required=True)
    note.add_argument("--pr", type=int, required=True)
    note.add_argument("--body", required=True)
    note.set_defaults(handler=comment)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        validate_repo(config, args.repo)
        if getattr(args, "pr", 1) < 1:
            raise AppError("pull-request number must be positive")
        result = args.handler(config, args)
        print(json.dumps(result, sort_keys=True))
        return 0
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
