#!/usr/bin/env python3
"""Docker-activation pull-in regression for the DOCKER-USER oneshot unit.

PR#13 round-8 finding (inline comment 4354363965, MEDIUM, review 5621868186),
verbatim:

  "A separate `systemctl stop docker` followed by `systemctl start docker`
  stops this oneshot through Requires/PartOf, but does not start it again:
  Docker has no dependency pulling it in, and multi-user.target is already
  active. Any rebuilt forwarding rules or changed container address
  therefore remain unconverged until another playbook run. Make Docker
  activation pull in this unit, and ensure existing installations gain that
  dependency when the role reruns."

Reproduced live on the target host (systemd 249) before the fix: with only
After=/Requires=/PartOf=docker.service and WantedBy=multi-user.target, a
stop+start cycle on (a synthetic stand-in for) docker.service left the
oneshot inactive with ExecMainStartTimestamp unset and its marker file
never written. BindsTo= on the oneshot changed nothing either: Requires=,
BindsTo= and PartOf= propagate deactivation DOWN to the dependent, but
systemd never performs reverse activation when the required unit itself is
started. Only a REVERSE edge declared on the daemon side pulled the oneshot
back in: a docker.service.d drop-in with Wants=kanban-ui-docker-user.service
made every daemon start re-fire the oneshot (active(exited), marker
rewritten).

Fixed shape locked by this test (static parse of the two shipped templates
plus tasks/main.yaml -- the templates are credential-free Jinja; rendering
is done here by neutralising {{ ... }} expressions so the assertions read
the real shipped unit topology):

  * the oneshot unit keeps After=/Requires=/PartOf=docker.service, keeps no
    BindsTo=, and keeps WantedBy=multi-user.target (boot path);
  * the drop-in template declares Wants=kanban-ui-docker-user.service in
    [Unit] and carries NO After=/Before= (an ordering edge from the daemon
    onto the oneshot would cycle against After=docker.service);
  * the role installs the drop-in at
    /etc/systemd/system/docker.service.d/60-kanban-ui-docker-user.conf via
    a content-diffing template task (so existing installs upgrade on rerun,
    Copilot's second sentence), daemon-reloads when either unit-side file
    changes, asserts the LOADED graph (systemctl show docker -p Wants) on
    every play, and removes the drop-in on the kanban_ui_state=absent path
    so no orphaned Wants= survives teardown.

    python3 ansible/roles/kanban_ui/tests/test_docker_activation_pullin.py

Exit 0 = all pass. Stdlib only; no root, systemd, Docker or netfilter.
"""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROLE = HERE.parent
UNIT_TEMPLATE = ROLE / "templates" / "kanban_ui_docker_user.service.j2"
DROPIN_TEMPLATE = ROLE / "templates" / "docker_service_kanban_ui_pullin.conf.j2"
TASKS = ROLE / "tasks" / "main.yaml"

DROPIN_PATH = "/etc/systemd/system/docker.service.d/60-kanban-ui-docker-user.conf"
UNIT_NAME = "kanban-ui-docker-user.service"


def render(template_text):
    """Credential-free 'render': replace every Jinja expression with a fixed
    sentinel so key=value topology lines become plain text."""
    return re.sub(r"\{\{.*?\}\}", "SENTINEL", template_text, flags=re.S)


def parse_unit(text):
    """INI-ish parse of a unit file: {section: {directive: [values...]}}."""
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            current = m.group(1)
            sections.setdefault(current, {})
            continue
        m = re.match(r"^([A-Za-z]+)=(.*)$", line)
        if m and current is not None:
            sections[current].setdefault(m.group(1), []).append(m.group(2).strip())
    return sections


def unit_directives(sections, section, directive):
    """Flatten (possibly multi-line) directive values in a section."""
    out = []
    for value in sections.get(section, {}).get(directive, []):
        out.extend(v for v in value.split() if v)
    return out


def tasks_text():
    return TASKS.read_text(encoding="utf-8")


def task_block(name):
    """The text of one task, from '- name: <name>' to the next '- name:'."""
    text = tasks_text()
    start = text.index("- name: " + name)
    end = text.find("\n- name: ", start + 1)
    return text[start:] if end == -1 else text[start:end]


class UnitTopology(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.unit = parse_unit(render(UNIT_TEMPLATE.read_text(encoding="utf-8")))

    def test_oneshot_is_partof_and_requires_docker(self):
        # Stop-direction semantics preserved (r4 behaviour): daemon stop
        # must still take the oneshot down with it.
        unit = unit_directives(self.unit, "Unit", "PartOf")
        req = unit_directives(self.unit, "Unit", "Requires")
        self.assertIn("docker.service", unit)
        self.assertIn("docker.service", req)

    def test_oneshot_orders_after_docker(self):
        after = unit_directives(self.unit, "Unit", "After")
        self.assertIn("docker.service", after)

    def test_oneshot_has_no_bindsto(self):
        # BindsTo= was tested on the live systemd 249 and does NOT reverse-
        # activate (phase 2 of the synthetic proof). Locking it out keeps a
        # future edit from re-introducing it as a (non-)fix.
        self.assertEqual([], unit_directives(self.unit, "Unit", "BindsTo"))

    def test_oneshot_still_enabled_at_boot(self):
        want = unit_directives(self.unit, "Install", "WantedBy")
        self.assertIn("multi-user.target", want)

    def test_oneshot_is_oneshot_remain_after_exit(self):
        self.assertEqual(["oneshot"], unit_directives(self.unit, "Service", "Type"))
        self.assertEqual(["yes"], unit_directives(self.unit, "Service", "RemainAfterExit"))


class DropInTopology(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dropin = parse_unit(render(DROPIN_TEMPLATE.read_text(encoding="utf-8")))

    def test_dropin_wants_the_oneshot(self):
        wants = unit_directives(self.dropin, "Unit", "Wants")
        self.assertIn(UNIT_NAME, wants)

    def test_dropin_declares_no_ordering_edges(self):
        # A daemon-side After=/Before= onto the oneshot would form a start
        # order cycle against the oneshot's After=docker.service; systemd
        # then breaks it arbitrarily. Ordering lives solely on the oneshot.
        for directive in ("After", "Before", "Requires", "BindsTo", "UpheldBy"):
            self.assertEqual(
                [],
                unit_directives(self.dropin, "Unit", directive),
                f"drop-in must not declare {directive}=",
            )

    def test_dropin_only_has_unit_section(self):
        self.assertEqual(["Unit"], sorted(self.dropin.keys()))


class RoleWiring(unittest.TestCase):
    def test_dropin_installed_by_content_diffing_template_task(self):
        # Copilot's second sentence: existing installations must gain the
        # dependency on role rerun -> template: (content diff), not
        # copy/lineinfile shell hacks, targeting the canonical path.
        block = task_block("Install the docker.service reverse-dependency drop-in")
        self.assertIn("ansible.builtin.template:", block)
        self.assertIn("docker_service_kanban_ui_pullin.conf.j2", block)
        self.assertIn(DROPIN_PATH, block)

    def test_dropin_installed_root_owned_mode_0644(self):
        # The drop-in is read by the systemd daemon at every docker start
        # and its Wants= redirects daemon activation into our oneshot, so
        # it must be root-owned and non-writable by anything else. Lock the
        # exact ownership/mode the install task ships.
        block = task_block("Install the docker.service reverse-dependency drop-in")
        self.assertIn("owner: root", block)
        self.assertIn("group: root", block)
        self.assertIn("mode: '0644'", block)

    def test_dropin_parent_dir_ensured(self):
        # The template module does not create parent dirs; a stock docker
        # install has no docker.service.d, so the role must (first real run
        # failed with "Destination directory ... does not exist").
        block = task_block("Ensure the docker.service.d drop-in directory exists")
        self.assertIn("state: directory", block)
        self.assertIn("/etc/systemd/system/docker.service.d", block)

    def test_daemon_reload_when_unit_or_dropin_changed(self):
        block = task_block("Reload systemd after (re)installing the convergence unit or drop-in")
        self.assertIn("daemon_reload: true", block)
        self.assertIn("kanban_ui_fw_unit is changed", block)
        self.assertIn("kanban_ui_fw_dropin is changed", block)

    def test_loaded_graph_asserted_every_play(self):
        # Stale-file-but-never-reloaded and hand-deleted drop-ins both fail
        # here rather than leaving Docker without the pull-in.
        block = task_block("Verify Docker activation pulls the convergence unit in")
        self.assertIn("docker.service", block)
        self.assertIn("- Wants", block)
        self.assertIn(UNIT_NAME, block)
        self.assertIn("changed_when: false", block)

    def test_teardown_removes_dropin(self):
        block = task_block("Remove the DOCKER-USER kanban-ui enforcement unit and script")
        self.assertIn(DROPIN_PATH, block)
        self.assertIn("/etc/systemd/system/kanban-ui-docker-user.service", block)

    def test_no_lineinfile_or_shell_hack_for_the_dropin(self):
        # The dependency graph changes must be declarative: exactly two
        # managed references to the canonical path (template install task +
        # teardown removal), no lineinfile/shell manipulation of
        # docker.service.d anywhere.
        text = tasks_text()
        self.assertEqual(0, text.count("lineinfile"))
        self.assertEqual(2, text.count(DROPIN_PATH))
        self.assertEqual(0, text.count("command:\n    cmd:") + text.count("shell:"))


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
