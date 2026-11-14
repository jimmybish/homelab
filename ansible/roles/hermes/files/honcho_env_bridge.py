#!/usr/bin/env python3
"""Bridge the honcho SDK into the dependency environment the RUNNING gateway
already activated (fix card t_0d641077, AC6).

Hermes activates a plugin dependency generation at process boot
(hermes_cli/plugins_loader.py: a gateway running when `hermes plugins enable`
committed a new generation keeps the old one). The honcho provider imports
`honcho` lazily per turn, so a gateway that booted before `hermes plugins
install --yes-deps` logged honcho-ai can never import it, and every hass/
discord turn writes zero messages to Honcho. Restarting the gateway from the
deploy is forbidden here (its cgroup hosts the kanban dispatcher and the
worker pool, per the scope guard in honcho_wiring.yaml).

honcho-ai is a pure-python wheel, so copying the package into the
site-packages directory the live process has ALREADY activated makes the
lazy import succeed on the next turn without touching the process. This is
additive-only and self-limiting: it no-ops once the live env carries the SDK
(any restart activates the committed generation, which always has it).

Usage: honcho_env_bridge.py <hermes_home>
argv-safe: one validated path argument, no shell. stdout is a single JSON
record with no secrets. Exit 0 on skip/noop/bridged; 1 when the committed
generation itself lacks the SDK (install genuinely broken).
"""
from __future__ import annotations

import glob
import grp
import json
import os
import pwd
import re
import shutil
import sys

ENV_RE = re.compile(r"(/[^\s]*environments/[0-9a-f]+/venv/lib/(python3[^/ ]*)/site-packages)")


def emit(action: str, **fields) -> None:
    print(json.dumps({"action": action, **fields}))


def live_site(hermes_home: str):
    try:
        raw = open(os.path.join(hermes_home, "gateway.pid")).read().strip()
        pid = raw
        if raw.startswith("{"):  # modern gateway.pid is a JSON record
            pid = str(json.loads(raw).get("pid", ""))
        if not pid.isdigit():
            return None, "bad-pid"
        maps_path = "/proc/%s/maps" % pid
        if not os.path.exists(maps_path):
            return None, "no-gateway-process"
        with open(maps_path, errors="replace") as fh:
            for line in fh:
                m = ENV_RE.search(line)
                if m:
                    return m.group(0), None
    except OSError:
        return None, "no-pid-file"
    return None, "live-env-not-in-maps"


def selected_site(hermes_home: str):
    """site-packages of a committed generation that carries the honcho SDK."""
    candidates = []
    for facts in glob.glob(os.path.join(hermes_home, "installs", "*", "facts.json")):
        try:
            env = json.load(open(facts)).get("packages", {}).get("venv", {}).get("environment")
        except (OSError, ValueError):
            continue
        if not env:
            continue
        # facts.json may store the env path with or without the trailing
        # /venv (selected_venv() returns .../<id>/venv), so try both.
        for base in (env, os.path.join(env, "venv")):
            for site in glob.glob(os.path.join(base, "lib", "python3*", "site-packages")):
                candidates.append(site)
    for site in candidates:
        if os.path.isdir(os.path.join(site, "honcho")):
            return site, None
    if candidates:
        return None, "selected-env-missing-sdk"
    return None, "no-committed-generation"


def main(argv):
    if len(argv) != 2 or not os.path.isabs(argv[1]):
        emit("error", reason="usage: honcho_env_bridge.py <absolute hermes_home>")
        return 1
    hermes_home = argv[1].rstrip("/")
    live, err = live_site(hermes_home)
    if live is None:
        emit("skip", reason=err)
        return 0
    sel, err = selected_site(hermes_home)
    if sel is None:
        if err == "selected-env-missing-sdk":
            emit("error", reason=err)
            return 1
        emit("skip", reason=err)
        return 0
    live_dir = os.path.join(hermes_home, live)
    if os.path.isdir(os.path.join(live_dir, "honcho")):
        emit("noop", live=live)
        return 0
    st = os.stat(live_dir)
    copied = []
    for name in sorted(os.listdir(sel)):
        if name == "honcho" or name.startswith("honcho_ai-"):
            src = os.path.join(sel, name)
            dst = os.path.join(live_dir, name)
            shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
            os.chown(dst, st.st_uid, st.st_gid)
            for root, dirs, files in os.walk(dst):
                for d in list(dirs) + files:
                    os.chown(os.path.join(root, d), st.st_uid, st.st_gid)
            copied.append(name)
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        owner, group = str(st.st_uid), str(st.st_gid)
    emit("bridged", live=live, selected=sel, copied=copied, owner=owner, group=group)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
