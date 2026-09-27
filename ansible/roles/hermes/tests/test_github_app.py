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
            title="Task 9",
            body="Summary",
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
                    "--title", "title", "--body", "body",
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
                        "--pr", "7", "--body", "hello",
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

        args = argparse.Namespace(repo="jimmybish/homelab", pr=7, body="done")
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

    def write_config(self):
        path = pathlib.Path(self.tempdir.name) / "config.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        path.chmod(0o600)
        return str(path)


if __name__ == "__main__":
    unittest.main()
