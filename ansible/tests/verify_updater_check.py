"""Run ONLY updater --check and compare read-only Docker snapshots on ubuntu.

Run from the repository root: python3 ansible/tests/verify_updater_check.py
Requires the normal inventory/vault access and ansible.posix.json callback.
No snapshot contains container environment variables or interpolated manifests.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile

import yaml


ANSIBLE = Path(__file__).resolve().parents[1]
SNAPSHOT = """
import json, subprocess
def docker(*args):
    return subprocess.check_output(['docker', *args], text=True)
ids = docker('ps', '-aq', '--no-trunc').split()
containers = json.loads(docker('inspect', *ids)) if ids else []
images = sorted(set(docker('image', 'ls', '-aq', '--no-trunc').split()))
images = json.loads(docker('image', 'inspect', *images)) if images else []
state_keys = ('Status', 'Running', 'Paused', 'Restarting', 'Dead', 'ExitCode',
              'StartedAt', 'FinishedAt')
print(json.dumps({
    'containers': sorted([{
        'id': c['Id'], 'name': c['Name'], 'image': c['Image'],
        'configured_image': c['Config']['Image'],
        'state': {k: c['State'][k] for k in state_keys},
        'restart_count': c['RestartCount'],
    } for c in containers], key=lambda c: c['id']),
    'images': sorted([{k: i.get(k) for k in
                     ('Id', 'RepoTags', 'RepoDigests', 'Created', 'Size')}
                     for i in images], key=lambda i: i['Id']),
    'volumes': sorted(docker('volume', 'ls', '-q').split()),
}, sort_keys=True))
"""


def run(playbook, extra=()):
    command = [
        "ansible-playbook", "-i", "inventory.yaml", str(playbook),
        "--vault-password-file", str(Path("~/ansible_key").expanduser()),
        "--limit", "ubuntu", *extra,
    ]
    print("TIME:", datetime.now(timezone.utc).isoformat(), flush=True)
    print("COMMAND:", " ".join(command), flush=True)
    result = subprocess.run(
        command, cwd=ANSIBLE,
        env={**os.environ, "ANSIBLE_STDOUT_CALLBACK": "ansible.posix.json",
             "ANSIBLE_NOCOLOR": "1"},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=1200, check=False,
    )
    if result.stderr:
        print(result.stderr.strip(), flush=True)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        # Do not dump arbitrary output that might contain credentials.
        raise RuntimeError(
            f"Expected JSON callback output; exit={result.returncode}"
        ) from error
    print("RECAP:", json.dumps(data["stats"], sort_keys=True), flush=True)
    return result.returncode, data


def snapshots(playbook):
    code, data = run(playbook)
    if code or any(s["failures"] or s["unreachable"]
                   for s in data["stats"].values()):
        raise RuntimeError("Snapshot incomplete; cannot claim nonmutation")
    values = {}
    for play in data["plays"]:
        for task in play["tasks"]:
            for host, result in task["hosts"].items():
                if "stdout" in result:
                    values[host] = json.loads(result["stdout"])
    if set(values) != set(data["stats"]):
        raise RuntimeError("Missing host snapshots")
    return values


def main():
    play = [{
        "name": "Read-only Docker metadata snapshot",
        "hosts": "ubuntu", "gather_facts": False, "become": True,
        "tasks": [{
            "name": "Inspect containers images and volume names without mutation",
            "ansible.builtin.command": {"argv": ["python3", "-c", SNAPSHOT]},
            "changed_when": False,
        }],
    }]
    with tempfile.TemporaryDirectory(prefix="updater-check-") as tmp:
        snapshot_play = Path(tmp) / "snapshot.yaml"
        snapshot_play.write_text(yaml.safe_dump(play))
        before = snapshots(snapshot_play)
        code, data = run(ANSIBLE / "update_docker_containers.yaml", ("--check",))
        # Always collect the after snapshot, even when predictions fail.
        after = snapshots(snapshot_play)
    predictions_failed = 0
    summaries = {}
    discovered = {}
    for play in data["plays"]:
        for task in play["tasks"]:
            name = task["task"]["name"]
            for host, result in task["hosts"].items():
                projects = result.get("ansible_facts", {}).get(
                    "valid_compose_projects"
                )
                if projects is not None:
                    discovered[host] = projects
                    print("PROJECTS:", host, json.dumps(projects))
                if name in ("Create Grafana silence", "Remove Grafana silence",
                            "Remove stopped containers for each project"):
                    if not result.get("skipped"):
                        raise RuntimeError(f"Unsafe task was not skipped: {name}")
                    print("GUARDED:", host, name, "skipped")
                if name != "Show sanitized updater summary":
                    continue
                summaries[host] = result.get("docker_update_results", [])
                for prediction in summaries[host]:
                    failed = prediction["status"] == "failed"
                    predictions_failed += bool(failed)
                    # The updater deliberately hides raw module output/manifests.
                    # Print ONLY its allowlisted summary fields, never msg/stderr.
                    allowed = {
                        k: prediction[k] for k in (
                            "project", "status", "check_mode", "registry_services",
                            "registry", "local_services", "local", "error", "rc",
                            "up", "convergence_changed",
                        ) if k in prediction
                    }
                    print("PREDICTION:", host, json.dumps(allowed, sort_keys=True))
                    if not prediction.get("check_mode"):
                        raise RuntimeError("Project result lacked check-mode evidence")
    complete = set(summaries) == set(before) == set(discovered)
    for host in summaries:
        complete &= {p["project"] for p in summaries[host]} == set(discovered.get(host, []))
    identical = before == after
    for host in sorted(set(before) | set(after)):
        if before.get(host) == after.get(host):
            state = before[host]
            print("UNCHANGED:", host, f"{len(state['containers'])} containers,",
                  f"{len(state['images'])} images,",
                  f"{len(state['volumes'])} volumes")
        else:
            print("DIFFERENT:", host)
            for key in ("containers", "images", "volumes"):
                if before.get(host, {}).get(key) != after.get(host, {}).get(key):
                    print("DIFFERENT CATEGORY:", host, key)
            old = {c["id"]: c for c in before.get(host, {}).get("containers", [])}
            new = {c["id"]: c for c in after.get(host, {}).get("containers", [])}
            print("CONTAINER IDS UNCHANGED:", host, old.keys() == new.keys())
            print("IMAGE METADATA UNCHANGED:", host,
                  before[host]["images"] == after[host]["images"])
            print("VOLUME NAMES UNCHANGED:", host,
                  before[host]["volumes"] == after[host]["volumes"])
            for container_id in sorted(old.keys() | new.keys()):
                a, b = old.get(container_id, {}), new.get(container_id, {})
                if a != b:
                    fields = {k: {"before": a.get(k), "after": b.get(k)}
                              for k in a.keys() | b.keys()
                              if a.get(k) != b.get(k)}
                    print("CONTAINER DRIFT:", host, a.get("name", b.get("name")),
                          container_id, json.dumps(fields, sort_keys=True))
    print("RESULT:", f"updater_exit={code}",
          f"prediction_failures={predictions_failed}",
          f"summary_complete={complete}",
          f"docker_metadata_unchanged={identical}", flush=True)
    if code or not identical or predictions_failed or not complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
