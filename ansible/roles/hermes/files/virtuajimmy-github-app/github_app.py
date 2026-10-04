#!/usr/bin/env python3
"""Constrained GitHub App client for VirtuaJimmy's pull-request workflow.

Model-authored prose (PR titles, bodies, comments) is accepted only through
the --title-file/--body-file options ('-' reads stdin), never as command-line
text, so it never appears in process argv.
"""

import argparse
import base64
import errno
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
MODEL_TEXT_LIMIT = 65_536


class AppError(RuntimeError):
    """Safe, credential-free error suitable for stderr."""


class SanitizedArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that never echoes user-supplied values on usage errors.

    argparse's default error() prints the offending arguments verbatim, so a
    rejected inline draft (e.g. '--title SECRET-DRAFT-$(id)') still reaches
    terminal logs even though it is never acted on. Usage failures here print
    only this parser's fixed usage line and keep the exit-code-2 semantics.
    """

    def error(self, message):
        # 'message' is deliberately dropped: it can contain raw argv text.
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: invalid command line (details suppressed "
              "because rejected arguments may contain private text)",
              file=sys.stderr)
        raise SystemExit(2)


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


def read_model_text(value, label):
    """Load model-authored text from stdin or a private file, never argv.

    PR/comment prose is model-derived and must never appear in a command
    line (injection plus process-argument disclosure). Accepted values:
    '-' for stdin, or the path of a private regular file owned by the
    executing user with no group or other access (0600 or stricter).
    Validation is bound to the opened descriptor: os.open() with
    O_NOFOLLOW refuses symlinks at open time, and os.fstat() checks the
    very descriptor that is read, so there is no interval where the
    pathname could be swapped to a symlink after validation.
    O_NONBLOCK keeps the open itself non-blocking: opening a FIFO for
    reading without O_NONBLOCK would stall forever waiting for a writer,
    so FIFOs are refused immediately (ENXIO with no writer, otherwise by
    the fstat regular-file check on the opened descriptor). Stdin is
    likewise bounded to MODEL_TEXT_LIMIT + 1 characters so an oversized
    stream is rejected without buffering it in full.
    """
    if value == "-":
        text = sys.stdin.read(MODEL_TEXT_LIMIT + 1)
    else:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is not None:
            flags |= nofollow
        try:
            descriptor = os.open(value, flags)
        except OSError as exc:
            if getattr(exc, "errno", None) in (errno.ELOOP, errno.ENXIO):
                # ELOOP: O_NOFOLLOW refused a symlink at open time.
                # ENXIO: O_NONBLOCK refused a FIFO with no writer; a FIFO
                # is not a regular file either way.
                raise AppError(f"{label} must be a regular text file or '-'") from None
            raise AppError(
                f"{label} must be '-' for stdin or an existing text file; "
                "inline text on the command line is not accepted"
            ) from None
        try:
            try:
                file_stat = os.fstat(descriptor)
            except OSError as exc:
                raise AppError(f"cannot read {label} file: {exc}") from None
            if not stat.S_ISREG(file_stat.st_mode):
                # Also covers ELOOP symlinks re-checked here and any
                # non-regular descriptor that reached the open.
                raise AppError(f"{label} must be a regular text file or '-'")
            if stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise AppError(
                    f"{label} file must be private: no group or other "
                    "access (0600 or stricter) is required"
                )
            if file_stat.st_uid != os.getuid():
                raise AppError(
                    f"{label} file must be owned by the executing user"
                )
            if nofollow is None:  # exotic platform without O_NOFOLLOW
                try:
                    path_stat = os.lstat(value)
                except OSError as exc:
                    raise AppError(f"cannot read {label} file: {exc}") from None
                if (
                    stat.S_IFMT(path_stat.st_mode) != stat.S_IFREG
                    or (path_stat.st_ino, path_stat.st_dev)
                    != (file_stat.st_ino, file_stat.st_dev)
                ):
                    raise AppError(
                        f"{label} must be a regular text file or '-'"
                    )
            try:
                with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                    descriptor = -1
                    raw = handle.read(MODEL_TEXT_LIMIT + 1)
            except (OSError, UnicodeError) as exc:
                raise AppError(f"cannot read {label} file: {exc}") from None
        finally:
            if descriptor != -1:
                os.close(descriptor)
        text = raw
    if len(text) > MODEL_TEXT_LIMIT:
        raise AppError(f"{label} text exceeds {MODEL_TEXT_LIMIT} characters")
    text = text.rstrip("\r\n")
    if not text.strip():
        raise AppError(f"{label} text must not be empty")
    return text


def push_branch(config, args):
    validate_ref(args.branch, "branch")
    validate_ref(args.source, "source")
    repository = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if repository.returncode != 0:
        raise AppError("current directory is not a trusted Git repository")
    repository_root = repository.stdout.strip()
    if not repository_root or "\n" in repository_root:
        raise AppError("Git returned an invalid repository path")

    token = installation_token(config, args.repo)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    with tempfile.TemporaryDirectory(prefix="vj-github-") as directory:
        git_config = pathlib.Path(directory) / "gitconfig"
        safe_directory = subprocess.run(
            [
                "git", "config", "--file", str(git_config),
                "--add", "safe.directory", repository_root,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if safe_directory.returncode != 0:
            raise AppError("could not create isolated Git configuration")
        git_config.write_text(
            git_config.read_text(encoding="utf-8")
            + "[credential]\n"
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
    title = read_model_text(args.title_file, "title")
    body = read_model_text(args.body_file, "body")
    token = installation_token(config, args.repo)
    response = api_request(
        config,
        "POST",
        f"/repos/{args.repo}/pulls",
        token,
        {
            "title": title,
            "head": args.head,
            "base": args.base,
            "body": body,
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
    body = read_model_text(args.body_file, "body")
    token = installation_token(config, args.repo)
    issue = api_request(
        config, "GET", f"/repos/{args.repo}/issues/{args.pr}", token
    )
    if "pull_request" not in issue:
        raise AppError(f"#{args.pr} is an issue, not a pull request")
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
    root = SanitizedArgumentParser(
        description=__doc__,
        usage="%(prog)s --config FILE {push-branch,open-pr,comment} ...",
        allow_abbrev=False,
    )
    root.add_argument("--config", required=True)
    commands = root.add_subparsers(
        dest="operation", required=True, parser_class=SanitizedArgumentParser)

    push = commands.add_parser(
        "push-branch",
        usage="%(prog)s --repo REPO --branch REF [--source REF]",
        allow_abbrev=False,
    )
    push.add_argument("--repo", required=True)
    push.add_argument("--branch", required=True)
    push.add_argument("--source", default="HEAD")
    push.set_defaults(handler=push_branch)

    pull = commands.add_parser(
        "open-pr",
        usage="%(prog)s --repo REPO --head REF [--base REF] "
              "--title-file FILE --body-file FILE [--draft]",
        allow_abbrev=False,
    )
    pull.add_argument("--repo", required=True)
    pull.add_argument("--head", required=True)
    pull.add_argument("--base", default="main")
    pull.add_argument(
        "--title-file", required=True,
        help="file holding the PR title, or '-' to read stdin; "
             "inline title text on the command line is not accepted",
    )
    pull.add_argument(
        "--body-file", required=True,
        help="file holding the PR body, or '-' to read stdin; "
             "inline body text on the command line is not accepted",
    )
    pull.add_argument("--draft", action="store_true")
    pull.set_defaults(handler=open_pr)

    note = commands.add_parser(
        "comment",
        usage="%(prog)s --repo REPO --pr NUMBER --body-file FILE",
        allow_abbrev=False,
    )
    note.add_argument("--repo", required=True)
    note.add_argument("--pr", type=int, required=True)
    note.add_argument(
        "--body-file", required=True,
        help="file holding the comment body, or '-' to read stdin; "
             "inline comment text on the command line is not accepted",
    )
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
