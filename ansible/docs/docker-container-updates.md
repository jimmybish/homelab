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

Check mode skips both Grafana silence includes and global container pruning.
Compose modules use their supported check mode (Compose `--dry-run`); no task
forces execution with `check_mode: false`. Pull predictions are not proof that
images changed. Never use `--diff` on secret-bearing manifests.

Normal execution still creates/removes a Grafana silence, globally prunes stopped
containers, pulls images, and converges projects with orphan removal. This change
does not authorize running that live path. The prune is global despite its
historic task name; it can delete unrelated stopped containers. It has not been
redesigned or made safe for live use by this policy change.

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
