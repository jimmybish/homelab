#!/usr/bin/env python3
"""Simulate only config validation and ID-scoped legacy transition commands."""
import json
import os
import sys

path = os.environ["CADVISOR_TEST_STATE"]
with open(path) as stream:
    state = json.load(stream)
args = sys.argv[1:]
if args[0] == "compose":
    assert args[-2:] == ["config", "--quiet"]
    sys.exit(1 if state.get("invalid_config") else 0)
action, container_id = args[:2]
assert action in ("stop", "rename", "start")
name, container = next((name, value) for name, value in state["containers"].items()
                       if value["Id"] == container_id)
if action == "rename":
    del state["containers"][name]
    container["Name"] = "/" + args[2]
    state["containers"][args[2]] = container
else:
    container["State"]["Running"] = action == "start"
state["events"].append(action)
with open(path, "w") as stream:
    json.dump(state, stream)
