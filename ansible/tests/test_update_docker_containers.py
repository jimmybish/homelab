"""Local-only updater regressions; no Docker daemon or remote inventory required."""

import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import yaml


ANSIBLE = Path(__file__).resolve().parents[1]
DISCOVERY = ANSIBLE / "tasks/discover_managed_compose.yaml"
UPDATER = ANSIBLE / "update_docker_containers.yaml"
PROJECT_TASKS = ANSIBLE / "tasks/update_compose_project.yaml"


class ComposeUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = self.root / "storage"
        self.storage.mkdir()
        inventory = {
            "ubuntu": {
                "hosts": {"localhost": {"ansible_connection": "local"}},
                "vars": {"docker_storage_folder": str(self.storage)},
            }
        }
        (self.root / "inventory.yaml").write_text(yaml.safe_dump(inventory))

    def run_playbook(self, path, check=False, env=None):
        command = ["ansible-playbook", "-i", "inventory.yaml", str(path)]
        if check:
            command.append("--check")
        return subprocess.run(
            command,
            cwd=self.root,
            env={**os.environ, "ANSIBLE_NOCOLOR": "1", **(env or {})},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )

    def discover(self, expected, check=False):
        play = [{
            "name": "Test actual managed Compose discovery",
            "hosts": "ubuntu",
            "gather_facts": False,
            "tasks": [
                {"ansible.builtin.include_tasks": str(DISCOVERY)},
                {"ansible.builtin.assert": {
                    "that": ["valid_compose_projects == expected_projects"],
                }},
            ],
            "vars": {"expected_projects": [str(p) for p in expected]},
        }]
        path = self.root / "discover.yaml"
        path.write_text(yaml.safe_dump(play))
        return self.run_playbook(path, check)

    def manifest(self, relative):
        path = self.storage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("services: {}\n")
        return path

    def test_exact_depth_filename_and_regular_files(self):
        self.manifest("managed/docker-compose.yaml")
        self.manifest("managed/docker-compose.yml")
        self.manifest("managed/compose.yaml")
        self.manifest("managed/docker-compose.override.yaml")
        self.manifest("firecrawl/docker-compose.yaml")
        self.manifest("firecrawl/source/docker-compose.yaml")
        self.manifest("firecrawl/source/apps/html/docker-compose.yml")
        self.manifest("tracearr-stack/docker-compose.yaml")
        self.manifest("tracearr-stack/tracearr/docker-compose.yaml")
        self.manifest("yml-only/docker-compose.yml")
        self.manifest("default-only/compose.yaml")
        self.manifest("docker-compose.yaml")  # root is not a project
        (self.storage / "no-manifest").mkdir()
        (self.storage / "directory/docker-compose.yaml").mkdir(parents=True)
        (self.storage / "fifo").mkdir()
        os.mkfifo(self.storage / "fifo/docker-compose.yaml")
        (self.storage / "linked-directory").symlink_to(
            self.storage / "managed", target_is_directory=True
        )
        (self.storage / "linked-file").mkdir()
        (self.storage / "linked-file/docker-compose.yaml").symlink_to(
            self.storage / "managed/docker-compose.yaml"
        )
        for check in (False, True):
            with self.subTest(check=check):
                result = self.discover(
                    [self.storage / "firecrawl", self.storage / "managed",
                     self.storage / "tracearr-stack"], check
                )
                self.assertEqual(result.returncode, 0, result.stdout)

    def test_empty_storage(self):
        for check in (False, True):
            with self.subTest(check=check):
                result = self.discover([], check)
                self.assertEqual(result.returncode, 0, result.stdout)

    def test_missing_or_symlink_storage_is_explicit_error(self):
        self.storage.rmdir()
        result = self.discover([])
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("existing, non-symlink directory", result.stdout)
        target = self.root / "target"
        target.mkdir()
        self.storage.symlink_to(target, target_is_directory=True)
        result = self.discover([], check=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("existing, non-symlink directory", result.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root can traverse unreadable fixtures")
    def test_unreadable_storage_is_not_silently_empty(self):
        self.storage.chmod(0)
        self.addCleanup(self.storage.chmod, 0o700)
        result = self.discover([], check=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Cannot inspect Docker storage", result.stdout)

    def test_check_guards_and_explicit_files(self):
        plays = yaml.safe_load(UPDATER.read_text())
        project_tasks = yaml.safe_load(PROJECT_TASKS.read_text())[1]["block"]
        guarded = [
            plays[0]["tasks"][0],
            plays[2]["tasks"][0],
        ]
        for task in guarded:
            self.assertEqual(task["when"], "not ansible_check_mode")
        for module in ("docker_compose_v2", "docker_compose_v2_pull"):
            task = next(t for t in project_tasks if
                        "community.docker." + module in t)
            self.assertEqual(task["community.docker." + module]["files"],
                             ["docker-compose.yaml"])
        for path in (UPDATER, DISCOVERY, PROJECT_TASKS,
                     ANSIBLE / "tasks/grafana_create_silence.yaml",
                     ANSIBLE / "tasks/grafana_remove_silence.yaml"):
            self.assertNotRegex(path.read_text(),
                                r"check_mode:\s*(false|no|off|0)\b")

    def test_updater_has_no_host_global_garbage_collection(self):
        # Compose cannot recreate standalone cAdvisor or stopped runtime jobs.
        # Guard orchestration and included task sources, not just check mode.
        for path in (UPDATER, DISCOVERY, PROJECT_TASKS):
            source = path.read_text()
            self.assertNotIn("community.docker.docker_prune", source)
            self.assertNotRegex(source, r"docker\s+(?:container|image|volume|system)\s+prune")

    def updater_fixture(self):
        shutil.copy(UPDATER, self.root / UPDATER.name)
        tasks = self.root / "tasks"
        tasks.mkdir()
        shutil.copy(DISCOVERY, tasks / DISCOVERY.name)
        shutil.copy(PROJECT_TASKS, tasks / PROJECT_TASKS.name)
        for action in ("create", "remove"):
            (tasks / f"grafana_{action}_silence.yaml").write_text(
                "- ansible.builtin.debug:\n"
                f"    msg: TEST_GRAFANA_{action.upper()}\n"
            )
        collections = self.root / "collections"
        modules = (collections / "ansible_collections/community/docker"
                   "/plugins/modules")
        modules.mkdir(parents=True)
        compose_mock = """
import json
from pathlib import Path
from ansible.module_utils.basic import AnsibleModule
m = AnsibleModule(argument_spec={
    'project_src': {'type': 'path', 'required': True},
    'files': {'type': 'list', 'elements': 'str', 'required': True},
    'state': {'type': 'str'}, 'remove_orphans': {'type': 'bool'},
    'build': {'type': 'str'}, 'pull': {'type': 'str'},
    'services': {'type': 'list', 'elements': 'str'},
    'policy': {'type': 'str'}, 'ignore_buildable': {'type': 'bool'},
    'include_deps': {'type': 'bool'}
}, supports_check_mode=True)
project = Path(m.params['project_src'])
outcome = json.loads((project / 'outcome.json').read_text())
kind = 'up' if m.params['state'] else 'pull'
with (project.parent / 'calls.jsonl').open('a') as log:
    log.write(json.dumps({'project': project.name, 'kind': kind,
                         'check': m.check_mode, 'params': m.params}) + '\\n')
if m.params['files'] != ['docker-compose.yaml']:
    m.fail_json(msg='UNSAFE_COMPOSE_EXECUTION')
if kind == 'pull':
    if (m.params['services'] != outcome['registry']
        or not m.params['services']
        or not m.params['ignore_buildable']
        or m.params['include_deps']
        or m.params['policy'] != 'always'):
        m.fail_json(msg='UNSAFE_SERVICE_SELECTION')
else:
    if m.params['build'] != 'never' or m.params['pull'] != 'never':
        m.fail_json(msg='UNSAFE_UP_EXECUTION')
if outcome.get('fail_' + kind):
    m.fail_json(msg='SECRET_MODULE_ERROR', stderr='SECRET_MODULE_STDERR', rc=17)
m.exit_json(changed=outcome.get('changed_' + kind, False),
            stdout='SECRET_MODULE_OUTPUT')
"""
        for module in ("docker_compose_v2", "docker_compose_v2_pull"):
            (modules / f"{module}.py").write_text(compose_mock)
        (modules / "docker_prune.py").write_text("""
from ansible.module_utils.basic import AnsibleModule
m = AnsibleModule(argument_spec={'containers': {'type': 'bool'}},
                  supports_check_mode=True)
m.fail_json(msg='PRUNE_MUST_NOT_EXECUTE')
""")
        return {"ANSIBLE_COLLECTIONS_PATH": str(collections)}

    def project(self, name, services, registry, **outcome):
        manifest = self.manifest(f"{name}/docker-compose.yaml")
        manifest.write_text(yaml.safe_dump({"services": services}))
        (manifest.parent / "outcome.json").write_text(
            json.dumps({"registry": registry, **outcome})
        )

    def update(self, check=False):
        result = self.run_playbook(
            self.root / UPDATER.name, check=check, env=self.updater_fixture()
        )
        for marker in ("SECRET_", "UNSAFE_", "PRUNE_MUST_NOT_EXECUTE",
                       "...ignoring"):
            self.assertNotIn(marker, result.stdout)
        calls = self.storage / "calls.jsonl"
        return result, ([json.loads(line) for line in calls.read_text().splitlines()]
                        if calls.exists() else [])

    def test_mixed_registry_build_local_only_and_check_guards(self):
        # A registry-looking local image is excluded only by explicit policy;
        # a local-looking registry image is NOT guessed to be local.
        self.project("mixed", {
            "app": {"image": "example/app:1", "build": ".",
                    "environment": {"TOKEN": "SECRET_MANIFEST_VALUE"}},
            "local": {"image": "ghcr.io/example/local:1", "pull_policy": "never"},
            "db": {"image": "local-looking:1", "depends_on": ["app", "local"]},
        }, ["db"], changed_pull=True)
        self.project("registry", {"web": {"image": "example/web:1"}}, ["web"])
        self.project("build-only", {"app": {"build": "."}}, [])
        self.project("local-only", {
            "app": {"image": "example/app:1", "pull_policy": "never"},
        }, [])
        self.manifest("mixed/source/docker-compose.yaml")
        result, calls = self.update(check=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("TEST_GRAFANA_", result.stdout)
        self.assertTrue(all(c["check"] for c in calls))
        self.assertEqual([(c["project"], c["kind"]) for c in calls], [
            ("build-only", "up"), ("local-only", "up"),
            ("mixed", "pull"), ("mixed", "up"),
            ("registry", "pull"), ("registry", "up"),
        ])
        for category in ("updated", "unchanged", "not_upgraded", "not_applicable"):
            self.assertIn(f'"{category}"', result.stdout)

    def test_failed_pull_skips_up_continues_and_fails_after_cleanup(self):
        self.project("a-bad", {"app": {"image": "example/app:1"}},
                     ["app"], fail_pull=True)
        self.project("z-good", {"app": {"image": "example/app:1"}}, ["app"])
        result, calls = self.update()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual([(c["project"], c["kind"]) for c in calls], [
            ("a-bad", "pull"), ("z-good", "pull"), ("z-good", "up"),
        ])
        self.assertIn('"error": "pull_failed"', result.stdout)
        self.assertIn('"up": "skipped"', result.stdout)
        self.assertLess(result.stdout.index("TEST_GRAFANA_REMOVE"),
                        result.stdout.index("Fail when any project failed"))

    def test_missing_local_image_fails_up_without_auto_build_or_pull(self):
        self.project("missing", {
            "app": {"image": "example/local:1", "pull_policy": "never"},
        }, [], fail_up=True)
        result, calls = self.update(check=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual([c["kind"] for c in calls], ["up"])
        self.assertIn('"error": "up_failed"', result.stdout)
        self.assertIn('"rc": 17', result.stdout)
        self.assertIn('"local": "not_upgraded"', result.stdout)

    def test_discovery_failure_still_reaches_cleanup_and_final_failure(self):
        self.storage.rmdir()
        result, calls = self.update()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(calls, [])
        self.assertIn('"error": "discovery_or_orchestration_failed"', result.stdout)
        self.assertLess(result.stdout.index("TEST_GRAFANA_REMOVE"),
                        result.stdout.index("Fail when any project failed"))

    def test_unsupported_policy_is_fail_closed_and_secret_safe(self):
        self.project("bad", {"app": {
            "image": "example/app:1", "pull_policy": "${SECRET_POLICY}",
        }}, ["app"])
        result, calls = self.update(check=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(calls, [])
        self.assertIn('"error": "policy_failed"', result.stdout)

    def test_non_always_registry_policies_fail_before_pull_or_up(self):
        policies = ("missing", "if_not_present", "build", "daily", "weekly")
        for policy in policies:
            self.project(policy, {"app": {
                "image": "example/app:1", "pull_policy": policy,
                "environment": {"TOKEN": "SECRET_MANIFEST_VALUE"},
            }}, ["app"])
        env = self.updater_fixture()
        for check in (False, True):
            with self.subTest(check=check):
                result = self.run_playbook(self.root / UPDATER.name, check, env)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((self.storage / "calls.jsonl").exists(),
                                 result.stdout)
                self.assertEqual(result.stdout.count('"error": "policy_failed"'),
                                 len(policies), result.stdout)
                self.assertNotIn("SECRET_", result.stdout)
                if not check:
                    self.assertLess(
                        result.stdout.index("TEST_GRAFANA_REMOVE"),
                        result.stdout.index("Fail when any project failed"),
                    )

    def test_registry_defaults_always_and_local_build_policies_are_preserved(self):
        services = {
            "explicit": {"image": "example/app:1", "pull_policy": "always"},
            "implicit": {"image": "example/dependency:1"},
            "local": {"image": "example/local:1", "pull_policy": "never"},
        }
        for policy in ("missing", "if_not_present", "build", "daily", "weekly"):
            services["build-" + policy] = {
                "image": "example/local:1", "build": ".",
                "pull_policy": policy,
            }
        self.project("supported", services, ["explicit", "implicit"])
        result, calls = self.update(check=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([c["kind"] for c in calls], ["pull", "up"])
        self.assertEqual(calls[0]["params"]["services"], ["explicit", "implicit"])
        self.assertIn('"local": "not_upgraded"', result.stdout)

    def test_yaml_anchors_and_malformed_manifests(self):
        self.project("anchor", {}, ["redis"])
        (self.storage / "anchor/docker-compose.yaml").write_text(
            "x-app: &app\n  build: .\n  image: example/app:1\n"
            "services:\n  app:\n    <<: *app\n"
            "  redis:\n    image: redis:7\n"
        )
        self.manifest("broken/docker-compose.yaml").write_text(
            "services: [SECRET_BROKEN\n"
        )
        result, calls = self.update(check=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual([(c["project"], c["kind"]) for c in calls],
                         [("anchor", "pull"), ("anchor", "up")])
        self.assertIn('"error": "policy_failed"', result.stdout)


class InstalledComposeModuleTests(unittest.TestCase):
    """Exercise installed command builders/failure handling, never a daemon."""

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["ansible-doc", "-j", "community.docker.docker_compose_v2_pull"],
            text=True, capture_output=True, check=True,
        )
        path = Path(json.loads(result.stdout)[
            "community.docker.docker_compose_v2_pull"]["doc"]["filename"])
        sys.path.insert(0, str(path.parents[5]))

    def manager(self, module, class_name, **attributes):
        installed = importlib.import_module(
            "ansible_collections.community.docker.plugins.modules." + module)
        manager = object.__new__(getattr(installed, class_name))
        manager.get_base_args = lambda: [
            "compose", "--project-directory", "/test",
            "--file", "docker-compose.yaml",
        ]
        for key, value in attributes.items():
            setattr(manager, key, value)
        return manager

    def test_installed_pull_selects_only_registry_and_never_dependencies(self):
        manager = self.manager(
            "docker_compose_v2_pull", "PullManager", policy="always",
            ignore_buildable=True, include_deps=False, services=["redis"],
        )
        for check in (False, True):
            command = manager.get_pull_cmd(check)
            self.assertIn("--ignore-buildable", command)
            self.assertNotIn("--include-deps", command)
            self.assertNotIn("--ignore-pull-failures", command)
            self.assertEqual(command[command.index("--") + 1:], ["redis"])
            self.assertEqual("--dry-run" in command, check)
            # Some versions omit --policy always. Manifest validation rejects
            # competing registry policies; local services are excluded explicitly.
            if "--policy" in command:
                self.assertEqual(command[command.index("--policy") + 1], "always")

    def test_installed_up_missing_image_failure_has_no_build_or_pull_fallback(self):
        manager = self.manager(
            "docker_compose_v2", "ServicesManager", pull="never", build="never",
            remove_orphans=True, recreate="auto", renew_anon_volumes=False,
            dependencies=True, timeout=None, scale={}, wait=False, yes=False,
            services=[], project_src="/test", ignore_build_events=True,
        )
        manager.parse_events = Mock(return_value=[])
        manager.emit_warnings = Mock()
        manager.client = SimpleNamespace(
            call_cli=Mock(return_value=(1, "", "No such image: example/local:1")),
            get_cli=lambda: "docker",
        )
        for check in (False, True):
            manager.check_mode = check
            manager.client.call_cli.reset_mock()
            result = manager.cmd_up()
            self.assertTrue(result["failed"])
            self.assertEqual(result["rc"], 1)
            manager.client.call_cli.assert_called_once()
            command = manager.client.call_cli.call_args.args
            self.assertIn("--no-build", command)
            self.assertNotIn("--build", command)
            self.assertEqual(command[command.index("--pull") + 1], "never")
            self.assertEqual("--dry-run" in command, check)

        # An unchanged engine result remains unchanged on repeated convergence.
        manager.check_mode = False
        manager.client.call_cli.return_value = (0, "", "")
        for _ in range(2):
            result = manager.cmd_up()
            self.assertFalse(result.get("failed", False))
            self.assertFalse(result["changed"])


if __name__ == "__main__":
    unittest.main()
