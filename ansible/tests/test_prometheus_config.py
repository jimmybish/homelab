"""Exercise the actual narrow configuration tasks and handlers, not a rewrite."""
import getpass
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

import yaml


ANSIBLE = Path(__file__).resolve().parents[1]


class ReadyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Prometheus Server is Ready.")

    def log_message(self, *args):
        pass


class PrometheusConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="prometheus-config-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "prometheus"
        self.folder.mkdir()
        self.final = self.folder / "prometheus.yml"
        self.final.write_text("previous configuration\n")
        self.bind = self.root / "stale-bind.yml"
        self.bind.write_text(self.final.read_text())
        self.state_path = self.root / "state.json"
        self.state_path.write_text(json.dumps({
            "folder": str(self.folder), "bind": str(self.bind), "events": [],
        }))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ReadyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        collection = self.root / "collections/ansible_collections/community/docker/plugins/modules"
        collection.mkdir(parents=True)
        source = (ANSIBLE / "tests/fixtures/prometheus_fake_module.py").read_text()
        # The full handler file must parse, but invoking its unrelated Compose
        # handlers must fail (their arguments are deliberately unsupported).
        for kind in ("docker_container_info", "docker_container_exec", "docker_compose_v2"):
            (collection / f"{kind}.py").write_text(f"KIND = {kind!r}\n" + source)
        play = [{
            "hosts": "grafana_host", "gather_facts": False,
            "vars": {
                "grafana_prometheus_data_folder": str(self.folder),
                "grafana_prometheus_port": server.server_address[1],
                "docker_user": getpass.getuser(), "internal_domain": "example.internal",
                "ansible_default_ipv4": {"address": "192.0.2.10"},
            },
            "tasks": [{"ansible.builtin.include_role": {
                "name": str(ANSIBLE / "roles/grafana"),
                "tasks_from": "prometheus_config.yaml", "vars_from": "empty.yaml",
            }}],
        }]
        # A tiny role copy retains the real tasks/templates/handlers, excluding
        # role vars and unrelated full-stack tasks from this local simulation.
        import shutil
        role = self.root / "role"
        for folder in ("tasks", "templates", "handlers"):
            shutil.copytree(ANSIBLE / "roles/grafana" / folder, role / folder)
        play[0]["tasks"][0]["ansible.builtin.include_role"]["name"] = str(role)
        del play[0]["tasks"][0]["ansible.builtin.include_role"]["vars_from"]
        (self.root / "play.yaml").write_text(yaml.safe_dump(play))
        hosts = ["docker-1", "docker-2", "proxy", "hermes", "sparky", "nova"]
        inventory = {"grafana_host": {"hosts": {"docker-1": {
            "ansible_connection": "local"}}},
            "cadvisor_hosts": {"hosts": {h: {} for h in hosts}}}
        (self.root / "inventory.yaml").write_text(yaml.safe_dump(inventory))

    def state(self):
        return json.loads(self.state_path.read_text())

    def run_play(self, check=False):
        return subprocess.run(
            ["ansible-playbook", "-i", "inventory.yaml", "play.yaml"]
            + (["--check"] if check else []),
            cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PROMETHEUS_TEST_STATE": str(self.state_path),
                 "ANSIBLE_COLLECTIONS_PATH": str(self.root / "collections"),
                 "ANSIBLE_NOCOLOR": "1"}, timeout=120)

    def test_validate_publish_reload_and_repeat_idempotency(self):
        first = self.run_play()
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual(self.final.read_bytes(), self.bind.read_bytes())
        self.assertEqual(self.state()["events"], [
            "validate:/prometheus/prometheus.yml.candidate", "refresh",
            "validate:/etc/prometheus/prometheus.yml", "reload"])
        second = self.run_play()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("changed=0", second.stdout)
        self.assertEqual(self.state()["events"].count("reload"), 1)

    def test_invalid_candidate_never_publishes_or_reloads(self):
        state = self.state()
        state["invalid"] = True
        self.state_path.write_text(json.dumps(state))
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.final.read_text(), "previous configuration\n")
        self.assertEqual(self.bind.read_text(), "previous configuration\n")
        self.assertEqual(self.state()["events"],
                         ["validate:/prometheus/prometheus.yml.candidate"])

    def test_check_mode_does_not_publish_or_reload(self):
        result = self.run_play(check=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.final.read_text(), "previous configuration\n")
        self.assertFalse((self.folder / "prometheus.yml.candidate").exists())
        self.assertEqual(self.state()["events"], [])

    def test_recovers_interrupted_publication_when_final_is_unchanged(self):
        first = self.run_play()
        self.assertEqual(first.returncode, 0, first.stdout)
        self.bind.write_text("stale bind after interrupted publication\n")
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.final.read_bytes(), self.bind.read_bytes())
        self.assertEqual(self.state()["events"].count("reload"), 2)


if __name__ == "__main__":
    unittest.main()
