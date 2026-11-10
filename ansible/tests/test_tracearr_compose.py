"""Local Tracearr relocation guards; no Docker or remote access."""

from pathlib import Path
import unittest

import yaml
from jinja2 import Environment, StrictUndefined


ROLE = Path(__file__).resolve().parents[1] / "roles/tracearr"


class TracearrComposeTests(unittest.TestCase):
    def test_database_directory_permissions_are_private_and_nonrecursive(self):
        tasks = yaml.safe_load((ROLE / "tasks/main.yaml").read_text())
        database = next(t["ansible.builtin.file"] for t in tasks
                        if t["name"] == "Setup tracearr database directory")
        self.assertEqual(database, {
            "path": "{{ tracearr_database_folder }}",
            "state": "directory",
            "mode": "0700",
            "owner": "1000",
            "group": "1000",
        })
        for task in tasks:
            config = task.get("ansible.builtin.file")
            if config and task["name"] != "Setup tracearr database directory":
                self.assertEqual(config["mode"], "0755")

    def test_stack_root_and_explicit_file_for_all_compose_calls(self):
        tasks = yaml.safe_load((ROLE / "tasks/main.yaml").read_text())
        handlers = yaml.safe_load((ROLE / "handlers/main.yaml").read_text())
        template = next(t["ansible.builtin.template"] for t in tasks
                        if t.get("ansible.builtin.template", {}).get("src")
                        == "templates/docker_compose.yaml.j2")
        self.assertEqual(template["dest"],
                         "{{ tracearr_parent_folder }}/docker-compose.yaml")
        self.assertEqual(template["mode"], "0644")
        compose = [t["community.docker.docker_compose_v2"]
                   for t in tasks + handlers
                   if "community.docker.docker_compose_v2" in t
                   and t["name"] in ("Run tracearr docker-compose up",
                                     "Start tracearr")]
        self.assertEqual(len(compose), 2)
        for config in compose:
            self.assertEqual(config["project_src"], "{{ tracearr_parent_folder }}")
            self.assertEqual(config["files"], ["docker-compose.yaml"])
            self.assertEqual(config["build"], "never")
            self.assertEqual(config["pull"],
                             "{{ tracearr_compose_pull | default('policy') }}")
        self.assertFalse(any(t.get("ansible.builtin.file", {}).get("state")
                             == "absent" for t in tasks))

    def test_existing_project_images_and_absolute_bind_paths(self):
        env = Environment(undefined=StrictUndefined)
        variables = {"docker_storage_folder": "/etc/docker-storage",
                     "timezone": "UTC", "tracearr_db_password": "test-only",
                     "tracearr_jwt_secret": "test-only",
                     "tracearr_cookie_secret": "test-only",
                     "internal_tracearr_url": "https://tracearr.example.invalid"}
        for key, value in yaml.safe_load((ROLE / "vars/main.yaml").read_text()).items():
            variables[key] = (env.from_string(value).render(variables)
                              if isinstance(value, str) else value)
        compose = yaml.safe_load(env.from_string(
            (ROLE / "templates/docker_compose.yaml.j2").read_text()
        ).render(variables))
        self.assertEqual(compose["name"], "tracearr")
        services = compose["services"]
        expected = {
            "tracearr": ("ghcr.io/connorgallopo/tracearr:latest",
                         "tracearr/backups:/data/backup"),
            "tracearr-db": ("timescale/timescaledb-ha:pg18.1-ts2.25.0",
                            "database:/home/postgres/pgdata/data"),
            "tracearr-redis": ("redis:8-alpine", "redis:/data"),
        }
        self.assertEqual(set(services), set(expected))
        for name, (image, mount) in expected.items():
            self.assertEqual(services[name]["image"], image)
            self.assertEqual(services[name]["volumes"],
                             ["/etc/docker-storage/tracearr-stack/" + mount])
            self.assertEqual(services[name]["networks"], ["tracearr_net"])


if __name__ == "__main__":
    unittest.main()
