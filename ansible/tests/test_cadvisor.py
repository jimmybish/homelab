"""cAdvisor source guards and real Ansible flow tests with simulated Docker.

The simulation runs the unmodified role tasks/conditions, template validation,
check mode and rescue flow locally. It is not a Docker runtime parity test.
"""

import getpass
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

import yaml
from jinja2 import Environment, StrictUndefined


ANSIBLE = Path(__file__).resolve().parents[1]
ROLE = ANSIBLE / "roles/docker"
VARIABLES = yaml.safe_load((ROLE / "vars/main.yaml").read_text())
BINDS = [
    "/:/rootfs:ro", "/var/run:/var/run:ro", "/sys:/sys:ro",
    "/var/lib/docker/:/var/lib/docker:ro", "/dev/disk/:/dev/disk:ro",
]


class CadvisorSourceTests(unittest.TestCase):
    def test_single_deployment_and_scrape_scope(self):
        inventory = yaml.safe_load((ANSIBLE / "inventory.yaml").read_text())
        self.assertEqual(set(inventory["cadvisor_hosts"]["hosts"]),
                         {"docker-1", "docker-2", "proxy", "hermes", "sparky", "nova"})
        self.assertNotIn("docker_vm_hosts", inventory)
        scrape = (ANSIBLE / "roles/grafana/templates/prometheus.yml.j2").read_text()
        self.assertIn("groups.get('cadvisor_hosts', [])", scrape)
        tasks = yaml.safe_load((ROLE / "tasks/main.yaml").read_text())
        selected = [t for t in tasks if "cadvisor" in t.get("tags", [])]
        self.assertEqual(len(selected), 2)
        for task in selected:
            self.assertIn("inventory_hostname in groups.get('cadvisor_hosts', [])",
                          task["when"])

    def test_rendered_six_scrapes_preserve_labels_and_self_ip(self):
        inventory = yaml.safe_load((ANSIBLE / "inventory.yaml").read_text())
        hosts = list(inventory["cadvisor_hosts"]["hosts"])
        template = Environment(undefined=StrictUndefined).from_string(
            (ANSIBLE / "roles/grafana/templates/prometheus.yml.j2").read_text())
        for self_host in ("docker-1", "nova"):
            config = yaml.safe_load(template.render(
                groups={"cadvisor_hosts": hosts},
                inventory_hostname=self_host,
                ansible_default_ipv4={"address": "192.0.2.10"},
                internal_domain="example.internal"))
            job = next(j for j in config["scrape_configs"] if j["job_name"] == "cadvisor")
            targets = job["static_configs"]
            self.assertEqual(len(targets), 6)
            for host, entry in zip(hosts, targets):
                fqdn = f"{host}.example.internal:8089"
                if host == self_host:
                    self.assertEqual(entry, {"targets": ["192.0.2.10:8089"],
                                             "labels": {"instance": fqdn}})
                else:
                    self.assertEqual(entry, {"targets": [fqdn]})
            self.assertEqual(config["global"]["scrape_interval"], "15s")

    def test_runtime_and_updater_policy(self):
        self.assertEqual(VARIABLES["docker_cadvisor_port"], 8089)
        self.assertEqual(VARIABLES["docker_cadvisor_image"],
                         "gcr.io/cadvisor/cadvisor@sha256:"
                         "3de2bd5203120b866d74a9b283b2ffb8ec382fbf9dc321814700c6ea6f44ec57")
        # Render only the two simple placeholders; actual Ansible rendering is
        # exercised below, including validate and the exact destination filename.
        text = (ROLE / "templates/cadvisor-compose.yaml.j2").read_text()
        text = text.replace("{{ docker_cadvisor_image }}", VARIABLES["docker_cadvisor_image"])
        text = text.replace("{{ docker_cadvisor_port }}", "8089")
        manifest = yaml.safe_load(text)
        self.assertEqual(manifest["name"], "cadvisor")
        service = manifest["services"]["cadvisor"]
        self.assertEqual(service["volumes"], BINDS)
        self.assertEqual(service["ports"], ["0.0.0.0:8089:8080"])
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertEqual(service["network_mode"], "bridge")
        self.assertEqual(service["container_name"], "cadvisor")
        self.assertEqual(service["image"], VARIABLES["docker_cadvisor_image"])
        for forbidden in ("user", "environment", "privileged", "build", "pull_policy", "runtime"):
            self.assertNotIn(forbidden, service)
        for path in (ROLE / "tasks").glob("cadvisor*.yaml"):
            self.assertNotIn("check_mode: false", path.read_text())
        tasks = yaml.safe_load((ROLE / "tasks/cadvisor.yaml").read_text())
        cleanup = tasks[-1]["community.docker.docker_container"]
        self.assertEqual(cleanup["state"], "absent")
        self.assertEqual(cleanup["name"], "{{ docker_cadvisor_before.container.Id }}")
        self.assertNotIn("state: started", (ROLE / "tasks/cadvisor.yaml").read_text())


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b'cadvisor_version_info{cadvisorVersion="v0.55.1"} 1\n'
                         b'container_last_seen{name="cadvisor"} 123\n')

    def log_message(self, *args):
        pass


class CadvisorFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cadvisor-flow-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "storage/cadvisor"
        self.state_path = self.root / "state.json"
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.bin = self.root / "bin"
        self.bin.mkdir()
        shutil.copy(ANSIBLE / "tests/fixtures/cadvisor_fake_docker.py", self.bin / "docker")
        (self.bin / "docker").chmod(0o755)
        collection = self.root / "collections/ansible_collections/community/docker/plugins/modules"
        collection.mkdir(parents=True)
        source = (ANSIBLE / "tests/fixtures/cadvisor_fake_module.py").read_text()
        for name in ("docker_container_info", "docker_container", "docker_image",
                     "docker_image_info", "docker_compose_v2", "docker_compose_v2_pull"):
            (collection / f"{name}.py").write_text(f"KIND = {name!r}\n" + source)
        play = [{
            "hosts": "localhost", "gather_facts": False,
            "vars": {**VARIABLES, "docker_storage_folder": str(self.root / "storage"),
                     "docker_cadvisor_port": self.port, "docker_user": getpass.getuser()},
            "tasks": [{"ansible.builtin.include_role": {
                "name": str(ROLE), "tasks_from": "cadvisor.yaml"}}],
        }]
        (self.root / "play.yaml").write_text(yaml.safe_dump(play))
        (self.root / "inventory.yaml").write_text("localhost ansible_connection=local\n")

    def legacy(self, labels=None):
        return {
            "Id": "a" * 64, "Name": "/cadvisor", "Image": "sha256:cached",
            "Config": {"Image": VARIABLES["docker_cadvisor_image"], "Labels": labels or {}},
            "HostConfig": {
                "Binds": BINDS, "NetworkMode": "bridge", "Privileged": False,
                "RestartPolicy": {"Name": "unless-stopped"},
                "PortBindings": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(self.port)}]},
            },
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }

    def seed(self, container=None, **extra):
        state = {"containers": {"cadvisor": container} if container else {},
                 "events": [], "cached": True, **extra}
        self.state_path.write_text(json.dumps(state))
        return state

    def state(self):
        return json.loads(self.state_path.read_text())

    def run_play(self, check=False):
        result = subprocess.run(
            ["ansible-playbook", "-i", "inventory.yaml", "play.yaml",
             "-e", json.dumps({"docker_cadvisor_port": self.port})]
            + (["--check"] if check else []),
            cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                 "CADVISOR_TEST_STATE": str(self.state_path),
                 "ANSIBLE_COLLECTIONS_PATH": str(self.root / "collections"),
                 "ANSIBLE_NOCOLOR": "1"}, timeout=120,
        )
        return result

    def test_legacy_migration_and_repeat_idempotency(self):
        self.seed(self.legacy())
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout)
        state = self.state()
        self.assertEqual(list(state["containers"]), ["cadvisor"])
        self.assertEqual(state["events"], ["stop", "rename", "up", "remove"])
        labels = state["containers"]["cadvisor"]["Config"]["Labels"]
        self.assertEqual(labels["com.docker.compose.project.config_files"],
                         str(self.folder / "docker-compose.yaml"))
        self.assertEqual((self.folder / "docker-compose.yaml").stat().st_mode & 0o777, 0o644)
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r"changed=0\s+")
        self.assertEqual(self.state(), state)

    def test_fresh_host_and_missing_image(self):
        self.seed(cached=False)
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.state()["events"], ["pull_missing", "up"])

    def test_check_mode_legacy_and_fresh_do_not_mutate(self):
        for container in (None, self.legacy()):
            with self.subTest(legacy=bool(container)):
                before = self.seed(container, cached=bool(container))
                result = self.run_play(check=True)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.state(), before)
                self.assertFalse(self.folder.exists())

    def test_managed_check_mode_does_not_mutate(self):
        self.seed()
        self.assertEqual(self.run_play().returncode, 0)
        before = self.state()
        result = self.run_play(check=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r"changed=0\s+")
        self.assertEqual(self.state(), before)

    def test_foreign_project_and_unrecognized_legacy_refused(self):
        for container in (self.legacy({"com.docker.compose.project": "foreign"}),
                          {**self.legacy(), "Config": {"Image": "foreign", "Labels": {}}}):
            before = self.seed(container)
            result = self.run_play()
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("refusing migration or takeover", result.stdout)
            self.assertEqual(self.state(), before)
            self.assertFalse(self.folder.exists())

    def test_failed_up_restores_exact_legacy(self):
        legacy = self.legacy()
        self.seed(legacy, fail_up=True)
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.state()["containers"], {"cadvisor": legacy})
        self.assertEqual(self.state()["events"], ["stop", "rename", "up", "remove", "rename", "start"])
        self.assertIn("Verify recovered legacy metrics", result.stdout)

    def test_invalid_manifest_fails_before_interruption(self):
        before = self.seed(self.legacy(), invalid_config=True)
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.state(), before)

    def test_exact_updater_discovers_and_recovers_missing_pinned_service(self):
        self.seed()
        deployed = self.run_play()
        self.assertEqual(deployed.returncode, 0, deployed.stdout)
        # A simulated missing container, not a destructive live recovery test.
        state = self.state()
        state["containers"] = {}
        state["events"] = []
        self.state_path.write_text(json.dumps(state))
        play = [{
            "hosts": "localhost", "gather_facts": False,
            "vars": {"docker_storage_folder": str(self.root / "storage"),
                     "docker_update_results": []},
            "tasks": [
                {"ansible.builtin.include_tasks":
                 str(ANSIBLE / "tasks/discover_managed_compose.yaml")},
                {"ansible.builtin.assert": {"that": [
                    "valid_compose_projects == [docker_storage_folder ~ '/cadvisor']"]}},
                {"ansible.builtin.include_tasks":
                 str(ANSIBLE / "tasks/update_compose_project.yaml"),
                 "loop": "{{ valid_compose_projects }}",
                 "loop_control": {"loop_var": "compose_project"}},
                {"ansible.builtin.assert": {"that": [
                    "docker_update_results[0].status == 'ok'",
                    "docker_update_results[0].registry_services == ['cadvisor']",
                    "docker_update_results[0].local_services == []"]}},
            ],
        }]
        (self.root / "play.yaml").write_text(yaml.safe_dump(play))
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.state()["events"], ["registry_pull", "up"])
        self.assertEqual(list(self.state()["containers"]), ["cadvisor"])
        self.assertIn(VARIABLES["docker_cadvisor_image"],
                      (self.folder / "docker-compose.yaml").read_text())


if __name__ == "__main__":
    unittest.main()
