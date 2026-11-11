"""Local fixture for the real Prometheus configuration task/handler flow."""
import json
import os
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule


module = AnsibleModule(argument_spec={
    "name": {"type": "str"},
    "container": {"type": "str"},
    "argv": {"type": "list", "elements": "str"},
}, supports_check_mode=KIND == "docker_container_info")
state_path = Path(os.environ["PROMETHEUS_TEST_STATE"])
state = json.loads(state_path.read_text())
result = {"changed": False}
if KIND == "docker_container_info":
    result.update(exists=True, container={"State": {"Running": True}})
else:
    argv = module.params["argv"]
    result.update(rc=0, stdout="", stderr="")
    if argv[0] == "/bin/promtool":
        state["events"].append("validate:" + argv[-1])
        if state.get("invalid"):
            result.update(rc=1, stderr="simulated invalid configuration")
    elif argv[0] == "/bin/cmp":
        final = Path(state["folder"]) / "prometheus.yml"
        result["rc"] = int(final.read_bytes() != Path(state["bind"]).read_bytes())
    elif argv[0] == "/bin/sh":
        # Model the exact same-file guard and truncating write (not cp/unlink).
        assert "cmp -s " in argv[-1] and "dd if=" in argv[-1]
        final = Path(state["folder"]) / "prometheus.yml"
        bind = Path(state["bind"])
        if final.read_bytes() != bind.read_bytes():
            bind.write_bytes(final.read_bytes())
            state["events"].append("refresh")
    elif argv == ["/bin/kill", "-HUP", "1"]:
        state["events"].append("reload")
    else:
        module.fail_json(msg="unexpected command", argv=argv)
    state_path.write_text(json.dumps(state))
    if result["rc"] and argv[0] != "/bin/cmp":
        module.fail_json(msg="validation failed", **result)
module.exit_json(**result)
