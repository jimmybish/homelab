"""Local test fixture; KIND is prepended by test_cadvisor.py."""
import json
import os

from ansible.module_utils.basic import AnsibleModule


spec = {key: {"type": kind} for key, kind in {
    "name": "str", "source": "str", "force_source": "bool", "state": "str",
    "keep_volumes": "bool", "project_src": "str", "project_name": "str",
    "files": "list", "build": "str", "pull": "str", "remove_orphans": "bool",
    "wait": "bool", "wait_timeout": "int",
    "services": "list", "policy": "str", "ignore_buildable": "bool",
    "include_deps": "bool",
}.items()}
module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
path = os.environ["CADVISOR_TEST_STATE"]
with open(path) as stream:
    state = json.load(stream)
params = module.params
containers = state["containers"]
result = {"changed": False}
failed = False

if KIND == "docker_container_info":
    result["exists"] = params["name"] in containers
    result["container"] = containers.get(params["name"])
elif KIND == "docker_image":
    if not state["cached"] and not module.check_mode:
        state["cached"] = True
        state["events"].append("pull_missing")
        result["changed"] = True
elif KIND == "docker_image_info":
    result["images"] = [{"Id": "sha256:cached"}] if state["cached"] else []
elif KIND == "docker_container":
    assert params["state"] == "absent" and params["keep_volumes"]
    for name, container in list(containers.items()):
        if container["Id"] == params["name"]:
            result["changed"] = True
            if not module.check_mode:
                del containers[name]
                state["events"].append("remove")
elif KIND == "docker_compose_v2":
    assert params["files"] == ["docker-compose.yaml"]
    assert params["project_name"] in ("cadvisor", None)
    if params["project_name"] is None:  # updater uses the manifest's name
        with open(params["project_src"] + "/docker-compose.yaml") as stream:
            assert "\nname: cadvisor\n" in stream.read()
    assert params["pull"] == "never" and params["build"] == "never"
    assert bool(params["remove_orphans"]) == (params["project_name"] is None)
    result["changed"] = "cadvisor" not in containers
    if result["changed"] and not module.check_mode:
        state["events"].append("up")
        containers["cadvisor"] = {
            "Id": "b" * 64, "Image": "sha256:cached",
            "Config": {"Labels": {
                "com.docker.compose.project": "cadvisor",
                "com.docker.compose.service": "cadvisor",
                "com.docker.compose.project.working_dir": params["project_src"],
                "com.docker.compose.project.config_files": params["project_src"] + "/docker-compose.yaml",
            }},
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }
        failed = state.get("fail_up", False)
elif KIND == "docker_compose_v2_pull":
    assert params["files"] == ["docker-compose.yaml"]
    assert params["services"] == ["cadvisor"] and params["policy"] == "always"
    assert params["ignore_buildable"] and not params["include_deps"]
    if not module.check_mode:
        state["events"].append("registry_pull")

if not module.check_mode:
    with open(path, "w") as stream:
        json.dump(state, stream)
if failed:
    module.fail_json(msg="simulated Compose creation failure")
module.exit_json(**result)
