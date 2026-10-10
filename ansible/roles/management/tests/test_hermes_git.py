#!/usr/bin/env python3
"""Offline functional coverage for the hermes-git helper's task-id transport.

Run from the ansible directory:
    python3 -m unittest roles/management/tests/test_hermes_git.py

Every case renders roles/management/templates/hermes-git.py.j2 against a
THROWAWAY repository and a THROWAWAY worktree root inside a temp directory, so
the real homelab repository and /home/virtuajimmy/homelab.worktrees are never
touched. The contract under test (round-7 review finding): a model-derived
task id must never travel in shell command text. The helper therefore accepts
it only from HERMES_KANBAN_TASK or from exactly one STDIN line, and refuses
every argv form outright.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "templates" / "hermes-git.py.j2"

GIT_ID = [
    "git",
    "-c", "user.name=hermes-git-tests",
    "-c", "user.email=hermes-git-tests@invalid",
    "-c", "commit.gpgsign=false",
]


def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


class HermesGitWorktreeTransportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = pathlib.Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.origin = self.root / "origin.git"
        self.worktrees = self.root / "worktrees"

        # Bare origin + clone so origin/main exists and is not behind main.
        self.assertEqual(sh(["git", "init", "--bare", "-b", "main",
                             str(self.origin)]).returncode, 0, )
        self.assertEqual(sh(["git", "clone", str(self.origin), str(self.repo)]).returncode, 0)
        seeded = self.repo / "README.md"
        seeded.write_text("seed\n", encoding="utf-8")
        self.assertEqual(sh([*GIT_ID, "add", "README.md"], cwd=str(self.repo)).returncode, 0)
        self.assertEqual(sh([*GIT_ID, "commit", "-m", "seed"], cwd=str(self.repo)).returncode, 0)
        self.assertEqual(sh(["git", "push", "origin", "main"], cwd=str(self.repo)).returncode, 0)

        helper_path = self.root / "hermes-git"
        source = TEMPLATE.read_text(encoding="utf-8")
        for token, value in (
            ("{{ management_repo_dir }}", str(self.repo)),
            ("{{ management_worktree_root }}", str(self.worktrees)),
        ):
            self.assertIn(token, source)
            source = source.replace(token, value)
        self.assertNotIn("{{", source)
        helper_path.write_text(source, encoding="utf-8")
        helper_path.chmod(0o755)
        self.helper = helper_path

    def run_helper(self, args, stdin="", env=None):
        environ = {k: v for k, v in os.environ.items() if k != "HERMES_KANBAN_TASK"}
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, str(self.helper), *args],
            input=stdin, cwd=str(self.repo), env=environ,
            text=True, capture_output=True, timeout=60, check=False,
        )

    def worktree_dirs(self):
        return sorted(p.name for p in self.worktrees.iterdir()) if self.worktrees.exists() else []

    def branches(self):
        proc = sh(["git", "branch", "--list"], cwd=str(self.repo))
        return proc.stdout

    # ------------------------------------------------------------------ pass

    def test_env_unset_stdin_line_creates_only_validated_worktree(self):
        proc = self.run_helper(["worktree"], stdin="t_abcd1234\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("branch: wt/t_abcd1234", proc.stdout)
        self.assertEqual(self.worktree_dirs(), ["t_abcd1234"])
        head = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                  cwd=str(self.worktrees / "t_abcd1234"))
        self.assertEqual(head.stdout.strip(), "wt/t_abcd1234")

    def test_stdin_transport_is_reusable_and_idempotent(self):
        first = self.run_helper(["worktree"], stdin="t_abcd1234\n")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_helper(["worktree"], stdin="t_abcd1234\n")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("worktree already exists", second.stdout)
        self.assertEqual(self.worktree_dirs(), ["t_abcd1234"])

    def test_env_present_bare_subcommand_needs_no_stdin(self):
        proc = self.run_helper(["worktree"], stdin="", env={"HERMES_KANBAN_TASK": "t_envcard"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.worktree_dirs(), ["t_envcard"])

    def test_env_and_matching_stdin_line_agree(self):
        proc = self.run_helper(["worktree"], stdin="t_envcard\n",
                               env={"HERMES_KANBAN_TASK": "t_envcard"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.worktree_dirs(), ["t_envcard"])

    # ----------------------------------------------------------------- fail

    def test_env_and_stdin_mismatch_is_refused_without_worktree(self):
        proc = self.run_helper(["worktree"], stdin="t_forged\n",
                               env={"HERMES_KANBAN_TASK": "t_realcard"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not match HERMES_KANBAN_TASK", proc.stderr)
        self.assertEqual(self.worktree_dirs(), [])
        self.assertNotIn("wt/t_forged", self.branches())

    def test_missing_id_fails_closed(self):
        for stdin in ("", "\n"):
            proc = self.run_helper(["worktree"], stdin=stdin)
            self.assertNotEqual(proc.returncode, 0, stdin)
            self.assertIn("no task id line", proc.stderr)
        # Whitespace-only stdin is no longer normalized away; it is a raw
        # line that fails the byte-exact task-id match.
        proc = self.run_helper(["worktree"], stdin="   \n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid task id", proc.stderr)
        self.assertEqual(self.worktree_dirs(), [])

    def test_malformed_ids_fail_closed_and_create_nothing(self):
        hostile = [
            "t_", "t_UPPER123", "t_too_long_identifier_value_x", "notatask",
            "t_abcd;rm -rf /", "t_../escape", "t_ab cd", "-t_abcd1234",
            "$(id)", "t_ab'cd",
        ]
        for value in hostile:
            proc = self.run_helper(["worktree"], stdin=f"{value}\n")
            self.assertNotEqual(proc.returncode, 0, repr(value))
            self.assertIn("invalid task id", proc.stderr)
        self.assertEqual(self.worktree_dirs(), [])
        self.assertNotIn("wt/", self.branches())

    def test_multiple_stdin_lines_are_refused(self):
        proc = self.run_helper(["worktree"], stdin="t_abcd1234\nt_abcd9999\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing multi-line input", proc.stderr)
        self.assertEqual(self.worktree_dirs(), [])
        self.assertNotIn("wt/", self.branches())

    def test_extra_blank_line_is_refused_without_worktree(self):
        # Round-8 finding: the old normalizer dropped empty lines, so a valid
        # id plus a trailing blank line silently passed. The raw contract
        # allows exactly ONE transport newline.
        for stdin in ("t_abcd1234\n\n", "t_abcd1234\n \n", "t_abcd1234\n\n\n"):
            proc = self.run_helper(["worktree"], stdin=stdin)
            self.assertNotEqual(proc.returncode, 0, repr(stdin))
            self.assertIn("refusing multi-line input", proc.stderr)
        self.assertEqual(self.worktree_dirs(), [])
        self.assertNotIn("wt/", self.branches())

    def test_whitespace_padded_ids_are_refused_without_worktree(self):
        # Round-8 finding: validation used to run after .strip(), accepting
        # padded values. The raw line content itself must match TASK_ID_RE.
        padded = [
            " t_abcd9999\n", "t_abcd7777 \n", "\tt_abcd7777\n",
            "t_abcd7777\t\n", " t_abcd9999 \n",
        ]
        for stdin in padded:
            proc = self.run_helper(["worktree"], stdin=stdin)
            self.assertNotEqual(proc.returncode, 0, repr(stdin))
            self.assertIn("invalid task id", proc.stderr)
        # Padding must not smuggle a match past the env-agreement check either.
        proc = self.run_helper(["worktree"], stdin=" t_envcard\n",
                               env={"HERMES_KANBAN_TASK": "t_envcard"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid task id", proc.stderr)
        self.assertEqual(self.worktree_dirs(), [])
        self.assertNotIn("wt/", self.branches())

    def test_missing_or_extra_line_terminator_is_refused(self):
        for stdin in ("t_abcd1234", "t_abcd1234\r\n", "t_abcd1234\n\r"):
            proc = self.run_helper(["worktree"], stdin=stdin)
            self.assertNotEqual(proc.returncode, 0, repr(stdin))
            self.assertIn("refusing multi-line input", proc.stderr)
        self.assertEqual(self.worktree_dirs(), [])
        self.assertNotIn("wt/", self.branches())

    def test_every_argv_task_id_form_is_refused(self):
        forms = [
            ["worktree", "--task", "t_abcd1234"],
            ["worktree", "t_abcd1234"],
            ["worktree", "--task"],
            ["worktree", "--task=t_abcd1234"],
            ["worktree", "-t", "t_abcd1234"],
        ]
        for args in forms:
            proc = self.run_helper(args, stdin="t_abcd1234\n")
            self.assertNotEqual(proc.returncode, 0, args)
            self.assertIn("takes no arguments", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
        # The refused argv forms must not have created anything either.
        self.assertEqual(self.worktree_dirs(), [])
        self.assertNotIn("wt/", self.branches())

    def test_source_has_no_argv_task_id_transport(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        code = source.split("def cmd_worktree", 1)[1].split("def cmd_base", 1)[0]
        self.assertNotIn("arg_id", code)
        self.assertNotIn("argv[1]", code)


if __name__ == "__main__":
    unittest.main()
