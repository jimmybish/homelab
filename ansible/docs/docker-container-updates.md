# Docker container updater

`update_docker_containers.yaml` targets `ubuntu`. It discovers only regular,
non-symlink `{{ docker_storage_folder }}/*/docker-compose.yaml` files in immediate
real child directories. It does not recurse, follow directory/file symlinks,
select `.yml`, or merge default/override Compose files. Both pull and up explicitly
use `files: [docker-compose.yaml]`.

An empty storage directory or missing candidate manifest yields no projects.
A missing/non-directory/symlink storage root, failed stat, or skipped discovery
path is an error, not a silently empty result.

Run from `ansible/`:

```bash
ansible-playbook -i inventory.yaml update_docker_containers.yaml --vault-password-file ~/ansible_key --limit ubuntu --check
```

Check mode skips both Grafana silence includes. There is no global pruning in
either execution mode.
Compose modules use their supported check mode (Compose `--dry-run`); no task
forces execution with `check_mode: false`. Pull predictions are not proof that
images changed. Never use `--diff` on secret-bearing manifests.

Normal execution still creates/removes a Grafana silence, pulls images, and
converges projects with project-scoped orphan removal. This change
does not authorize running that live path. Despite its historic task name,
the former prune was host-global. It was removed on 2026-11-11 after it
deleted stopped non-Compose cAdvisor exporters. No replacement image, volume, or
container garbage collection was added. Intentionally stopped standalone
containers must survive the updater. See [the recovery runbook](known-issues.md#cadvisor-missing-after-maintenance).

## cAdvisor Compose migration (2026-11-11)

Source: `roles/docker/tasks/cadvisor.yaml`,
`roles/docker/templates/cadvisor-compose.yaml.j2`, and the single digest pin in
`roles/docker/vars/main.yaml`. Only `cadvisor_hosts` is managed (docker-1,
docker-2, proxy, Hermes, Sparky, Nova). Following approved scope unification,
this same group supplies Prometheus's six cAdvisor targets; there is no separate
scrape-only group. Self-host IP targeting and FQDN instance labels are unchanged.
LXC hosts remain excluded.

The managed manifest is **`/etc/docker-storage/cadvisor/docker-compose.yaml`**
(`0644`, `docker_user` owner/group, directory `0755`). Project `cadvisor` is
explicit in both the manifest and role. Container name `cadvisor`, IPv4 port
`8089:8080`, five read-only binds, `unless-stopped`, default bridge networking
and image-provided user/environment/healthcheck are preserved. No privileged
mode, PUID/PGID, custom command or runtime override is added; the daemon's runtime
default is retained (including Sparky's NVIDIA default).

Nova also uses the NVIDIA daemon default on ARM64. The unchanged pinned
multi-platform digest includes linux/arm64; do not substitute a newer image or
force `runc` to expand monitoring scope.

Run from `ansible/`, never an untagged OS deployment:

```bash
ansible-playbook -i inventory.yaml os_setup.yaml --vault-password-file ~/ansible_key --tags cadvisor --limit cadvisor_hosts --list-tasks
ansible-playbook -i inventory.yaml os_setup.yaml --vault-password-file ~/ansible_key --tags cadvisor --limit cadvisor_hosts --check
ansible-playbook -i inventory.yaml os_setup.yaml --vault-password-file ~/ansible_key --tags cadvisor --limit cadvisor_hosts
```

### Prometheus configuration only

After changing `cadvisor_hosts`, run from `ansible/`:

```bash
ansible-playbook -i inventory.yaml deploy_grafana.yaml --vault-password-file ~/ansible_key --tags prometheus_config --limit grafana_host --list-tasks
ansible-playbook -i inventory.yaml deploy_grafana.yaml --vault-password-file ~/ansible_key --tags prometheus_config --limit grafana_host --check --diff
ansible-playbook -i inventory.yaml deploy_grafana.yaml --vault-password-file ~/ansible_key --tags prometheus_config --limit grafana_host
```

This tag selects only configuration inspection, staging, running-version
`promtool check config`, and publication with a backup. On a running stack the
change handler refreshes the old single-file bind inode from the validated
directory-mounted source, then sends SIGHUP and checks readiness. It does not
silence alerts, deploy dashboards/provisioning, change credentials, pull images,
or restart/recreate containers. Re-running unchanged configuration does not
reload it. Check mode previews files but does not execute validation or reload.
Initial stack bootstrap retains the normal full role path (no running promtool
to validate against); this tag is for an existing stack, not bootstrap.

Verify six `up{job="cadvisor"}` series are 1, each exporter has named-container
metrics, and `prometheus_config_last_reload_successful` is 1 with a fresh
`prometheus_config_last_reload_success_timestamp_seconds`. Allow at least two
15-second scrapes plus Grafana's evaluation interval before checking the
`cadvisor-down-1` rule. Newly monitored workloads can legitimately raise
Container Down alerts; report those without suppressing or repairing them here.

Rollback: restore the previous inventory scope (five exporters and the explicit
three-host scrape loop), deploy with `prometheus_config` to validate/reload,
then remove only Nova's `cadvisor` Compose project if rolling back the new
exporter. Rename Nova's manifest outside the updater's exact
`docker-compose.yaml` discovery name after stopping that project so a later
updater cannot recreate it. Do not remove any pre-existing exporter or run the
blanket updater.

The first live run refuses foreign Compose ownership or an unrecognized legacy
container, validates the manifest, caches only a missing pinned image, and checks
cached/running image identity **before** interruption. A confirmed legacy
container is stopped and renamed by ID to `cadvisor-legacy-<12-char-ID>`.
Compose starts the replacement with explicit `files: [docker-compose.yaml]`,
`build: never`, `pull: never` and no orphan removal. Only after Docker health and
version plus **named-container** metrics pass is that exact stopped legacy ID
removed (volumes retained). No restart handler or permanent standalone
start/controller remains.

If migration fails, rescue removes only a positively identified replacement
from this project/manifest, renames the original back, restores its previous
running state and checks recovered metrics. The play still fails so rollback is
visible. Already Compose-managed instances are never removed by migration
cleanup. Fresh/missing instances converge directly. Check mode never stops,
renames, removes, pulls or starts anything: an existing managed manifest uses
Compose dry-run; a fresh/legacy host reports staged work without trying to use
files that dry-run did not create.

**Recovery:** rerun the tagged role for a missing/stopped Compose container.
The updater's unchanged discovery also selects this immediate-child `.yaml`
manifest; no `build` or `pull_policy: never` excludes cAdvisor. Its registry pull
targets the **same digest**, followed by no-build/no-pull up, so recovery does
not float the version. This is compatibility evidence, **not authorization to
run the updater live**. Host-global container pruning remains prohibited.

**Operator rollback / interrupted controller:** before touching a name holder,
inspect its ID and all Compose project/service/working-dir/config-file labels.
If a `cadvisor-legacy-*` rollback copy survives interruption, retain it until
the replacement is verified. For failed replacement rollback, remove only the
confirmed replacement (never `down -v`), rename the known legacy ID to
`cadvisor`, start it if previously running, and recheck collector-path metrics.
If the legacy copy was already discarded after success, use the same validated
Compose manifest/digest to recover; reverting source alone does not undo remote
Compose labels. A deliberate return to standalone management requires a
controlled, approved Compose-to-standalone transition, not two controllers.
Preserve unrelated worktree changes when reverting only migration hunks.

Local regression coverage:

```bash
python3 -m unittest discover -s ansible/tests -p test_cadvisor.py -v
```

These execute real Ansible flow with simulated Docker: legacy/fresh/managed,
idempotency, dry-run non-mutation, foreign ownership rejection, preflight failure
and rollback after partial creation. The actual updater discovery/project tasks
also recover a simulated missing digest-pinned cAdvisor as a registry service.
Actual image IDs, mounts, security/runtime,
restart stability and Prometheus-network metrics require live verification.
Grafana's Prometheus-compatible rules API reports the aggregate healthy state as
`inactive` while its three per-instance states are `Normal`; do not reject that
healthy response merely because the aggregate is not spelled `normal`.

## Registry-only policy (2026-11-10)

`tasks/update_compose_project.yaml` reads each exact manifest privately with
`slurp`/`from_yaml` (including YAML anchors), using existing Ansible builtins:

- A service with `build` is excluded from pulls, even if it also has `image`.
  The pull module additionally sets `ignore_buildable: true`.
- An image-only local service **must** declare `pull_policy: never` in its
  deployment role's Compose source. The updater explicitly removes it from the
  pull service list; it does not depend on CLI policy precedence or image names.
  An unmarked image-only service is a registry target, even if named `local/...`.
- All remaining service names are passed explicitly to the pull module, with
  `policy: always`, `include_deps: false`. Mixed projects still pull their registry
  dependencies, without pulling excluded dependencies. An empty registry list
  skips pull entirely (an empty `services` argument would mean all services).
  Registry services must omit `pull_policy` or set it to `always`: some installed
  module versions omit `--policy always`, so competing manifest policies can
  silently skip pulls. Image-only services declaring `missing`, `if_not_present`,
  `build`, `daily`, or `weekly` fail policy validation before any project pull/up.
  Build services remain excluded and retain their supported static policies;
  image-only `never` services remain local exclusions.
- Only a successful pull phase (or no registry targets) permits project-wide
  convergence, with `build: never`, `pull: never`. Missing images fail; there is
  no automatic build or fallback pull. Local-only projects may converge using
  installed images, but are **not upgraded**. Local application upgrades belong
  to their deployment roles, not this updater.
- A failed pull skips that project's up; later projects continue. Policy/pull/up
  failures are collected, Grafana cleanup is attempted, then the final play fails
  the host/run. Discovery failures are also recorded for deferred failure.
  Unreachable hosts/controller interruption are not recoverable by this flow.

Policy selection deliberately supports self-contained static service maps only.
Includes, extends, profiles, interpolated/unsupported pull policies and malformed
manifests fail closed before pull/up, rather than guessing effective policy.
Compose still validates/interpolates the runtime configuration itself. No
configuration, project names, image tags/digests or runtime policies are rewritten.
If future manifests need these advanced policy forms, extend selection/tests
before enabling updates for them.

The summary contains only project/service names, registry `updated`/`unchanged`
(or `not_applicable`, `not_attempted`, `failed`), local `not_upgraded`, convergence
change status and a failure stage/return code. In check mode these are predictions.
Registry change uses the installed module's changed detection, not a guarantee of
a new digest. Secret-bearing manifests and raw module results/errors are `no_log`
and never dumped. A failed up can follow a successful registry pull; that pull's
outcome remains visible.

Read-only inspection of all ten Ubuntu hosts on 2026-11-10 confirmed:

| Local application | Explicit exclusion |
|---|---|
| MCP Radarr, Sonarr, Tracearr | `build` |
| Tronbyt Thinking, Kanban UI, ComfyUI | `build` |
| Firecrawl API, Playwright, nuq-postgres | `build`; Redis/RabbitMQ still registry targets |
| Sparky Immich ML | Image-only, `pull_policy: never` already in source and deployed manifest |

No new markers or deployments are required for these inspected manifests.
Future local-only image services need their explicit source policy deployed
before the updater can recognize it; a template edit alone does not affect a
live check. No local-image name heuristics or duplicate project allowlists exist.

## Tracearr migration (2026-11-10)

Tracearr's managed manifest is now
`docker-2:/etc/docker-storage/tracearr-stack/docker-compose.yaml`, so exact-depth
discovery includes it. Its explicit top-level `name: tracearr` preserves the
existing project and `tracearr_tracearr_net` network for both deployment and updater.
App backups, database and Redis retain their original absolute bind paths.
The old nested manifest was removed once on the host, not by a role cleanup task.
For a relocation-only deployment, pass `-e tracearr_compose_pull=never` to
`deploy_tracearr.yaml` to retain local images; the default remains Compose policy.
The role uses image-only services with `build: never`.

## Known exclusions (read-only inventory, 2026-11-10)

- Source/examples: `hermes:/etc/docker-storage/firecrawl/source/docker-compose.yaml`
  and `source/apps/go-html-to-md-service/docker-compose.yml` are excluded.
  Only `/etc/docker-storage/firecrawl/docker-compose.yaml` is managed.
- No other `.yml` Compose locations were found under storage on the ten Ubuntu hosts.

## Firecrawl

`deploy_firecrawl.yaml` retains the pinned release and loopback-only API. Its
managed manifest is intentionally `0644` for updater read access; it contains the
PostgreSQL credential, so local users able to traverse the `0750` service directory
can read it. Credential files, source, directory permissions and data are unchanged.
Do not run source Compose examples: they share the `firecrawl` project and network
but use different containers/storage and may expose conflicting ports.
Role convergence can replace same-project stale containers/remove orphans; it
must not remove named or anonymous volumes (`down -v` and volume pruning are unsafe).

## Local regression tests

```bash
python3 -m unittest discover -s ansible/tests -v
```

Requires PyYAML and `ansible-playbook`. Fixtures exercise real Ansible find/stat
in normal/check mode. The full updater check test substitutes Docker modules that
validate exact service selection and the no-build/no-pull up contract, and
record calls without Docker/API side effects. Failure fixtures cover skipped up,
continued processing, cleanup-before-failure, missing local images, YAML anchors,
secret suppression and fail-closed unsupported/malformed policy. Registry-policy
regressions verify rejection before pull/up in both normal and check mode, while
default/explicit `always`, local `never` and excluded build policies still work.
Installed-module unit tests also need Ansible importable by the test interpreter.
With this controller's pipx installation, use the existing runner environment
instead of installing packages into system Python:

```bash
/home/jimmy/.local/pipx/venvs/ansible/bin/python -m unittest discover -s ansible/tests -v
```

For an authorized live-inventory **check only** verification, run
`python3 ansible/tests/verify_updater_check.py` from the repository root.
It snapshots every Ubuntu host before/after the real updater `--check`, compares
container IDs/state/restarts/image IDs, image tags/digests/creation/size, and volume
names in memory, and reports prediction failures or incomplete summaries as a
nonzero exit. Raw module results are intentionally unavailable; dry-run command
construction is covered by local installed-module tests, while the verifier
requires check-mode summary evidence and unchanged before/after Docker metadata.
