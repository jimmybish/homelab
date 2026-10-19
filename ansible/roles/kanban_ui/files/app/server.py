#!/usr/bin/env python3
"""Kanban WebUI server (deployed as the kanban-ui container).

Mounts the *real* Hermes kanban dashboard plugin router
(plugins/kanban/dashboard/plugin_api.py) so every read/write uses the exact
same hermes_cli.kanban_db code paths as the CLI and the official dashboard.
The front-end is a single-page app served from ./static.

Auth model (no shared bearer token is ever delivered to a client):

- The static shell (/, /app.js, /app.css) is unauthenticated and contains
  NO credential material.
- The server holds one bootstrap secret (KANBAN_UI_TOKEN_FILE). The ONLY
  way to consume it is POST /login with the exact plaintext in the JSON
  body, AND the request must carry the proxy marker: a shared secret the
  SWAG vhost injects via proxy_set_header (private 0600 include file on
  the proxy host, never the 0664 vhost; round-5 finding 1), AND the REAL
  TCP peer must be the proxy host itself (KANBAN_UI_TRUSTED_PROXY_IP;
  loopback for local development). On success
  the client receives a random HttpOnly session cookie
  (KANBAN_UI_SESSION); neither secret ever appears in a response,
  HTML, JS, URL, or log line.
- Every /api/ request must present a valid session cookie AND the custom
  request header that the JS sets (double-submit CSRF guard against
  cross-site form/JSON POSTs — cookie alone is not enough).
- The /events WebSocket is authenticated by cookie AND Origin (PR review
  comment 4244239544): SameSite=strict is SITE-scoped, so JS on a
  compromised sibling subdomain of the internal domain still gets the
  cookie on its WS upgrade. The gate therefore ALSO requires the upgrade
  Origin header to equal the exact configured Kanban origin
  (KANBAN_UI_WS_ALLOWED_ORIGIN; deny-by-default when unset, same
  philosophy as the proxy marker, with the loopback-dev opt-out the only
  exception). There is NO query-parameter token path at all: no
  credential ever rides in a URL.

Board: the UI defaults to DEFAULT_BOARD and always sends ?board=...
explicitly, so nothing depends on the global current-board pointer.

NOTE: no `from __future__ import annotations` in this module on purpose --
FastAPI resolves route-handler annotations from module globals, and the
fastapi imports below are deliberately deferred into build_app(); string
annotations would make the Request parameter an unresolvable ForwardRef
and FastAPI would silently treat it as a required query param (422).

Bind: loopback unless --lan (or $KANBAN_UI_LAN=1). Public bind is safe to
expose to the LAN: everything privileged is cookie-gated.
"""
import argparse
import asyncio
import importlib.util
import os
import re
import secrets
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Location of the hermes-agent checkout and the Hermes home (board SQLite DBs)
# are environment-configurable so the same code runs on a developer laptop,
# a host process, or the deployed Docker container. Ansible sets these via
# compose environment (KANBAN_UI_HERMES_REPO / HERMES_HOME).
REPO = Path(os.environ.get("KANBAN_UI_HERMES_REPO", str(Path.home() / ".hermes" / "hermes-agent")))
PLUGIN = REPO / "plugins" / "kanban" / "dashboard"
STATIC = Path(os.environ.get("KANBAN_UI_STATIC", str(HERE / "static")))
TOKEN_FILE = Path(os.environ.get("KANBAN_UI_TOKEN_FILE", str(HERE / "token.txt")))
# Proxy marker file (round-4 finding 1): the shared secret SWAG stamps on
# every proxied request. Provisioned by Ansible (vault -> 0400 file,
# bind-mounted read-only). Absent by default -> marker enforcement is
# deny-by-default (see login_proxy_marked); the loopback-dev opt-out is
# the only way to run without it.
MARKER_FILE = Path(os.environ.get("KANBAN_UI_PROXY_MARKER_FILE", str(HERE / "proxy_marker.txt")))
# Trusted peer address (round-5 finding 1): the SWAG proxy host's real
# TCP source address (post-masquerade on its bridge, post-DNAT here).
# The marker is honored only when the actual peer is this host (or
# loopback, for the app host's own verifier). Empty => unset =>
# developer/laptop run with no peer check; the deployed compose ALWAYS
# sets it from the resolved inventory fact.
TRUSTED_PROXY_IP = os.environ.get("KANBAN_UI_TRUSTED_PROXY_IP", "")
DEFAULT_BOARD = os.environ.get("KANBAN_UI_DEFAULT_BOARD", "homelab")
# PR-review finding (comment 4244239544): the ONLY origin allowed to open
# the /events WebSocket. The deployment compose always sets this to the
# exact SWAG-served URL (https://kanban.<internal_domain>); an unset value
# denies every WS upgrade (deny-by-default, same philosophy as the proxy
# marker) unless the loopback-dev opt-out is active.
WS_ALLOWED_ORIGIN = os.environ.get("KANBAN_UI_WS_ALLOWED_ORIGIN", "")

SESSION_COOKIE = "kanban_ui_session"
SESSION_HEADER = "X-Kanban-UI"
# Sessions are in-process, so they only need to outlive a browsing visit;
# the container restarting simply logs everyone out (re-login is cheap).
SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 512
LOGIN_FAIL_LIMIT = 20
LOGIN_FAIL_WINDOW = 60

# PR#13 review finding (comment 4301554389): worker-control deny-list.
# This container is not the worker's host, so its reclaim path clears the
# claim WITHOUT ever signalling the real process (the pinned Hermes
# _terminate_reclaimed_worker only signals when the claim lock carries
# the LOCAL host prefix -- inside the container it never does). A UI-side
# reclaim would leave the host worker running while the dispatcher is
# free to spawn a duplicate. Pure predicates (stdlib-only, unit-tested in
# tests/test_worker_control_guard.py); build_app wires them into a
# middleware + a route dependency and is the only place fastapi is needed.
_WORKER_CONTROL_DENIED: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("POST", re.compile(r"^/api/kanban/tasks/[^/]+/reclaim$")),
    ("POST", re.compile(r"^/api/kanban/runs/[^/]+/terminate$")),
)
WORKER_CONTROL_REJECT_BODY = {
    "detail": "worker control operations are disabled in the Kanban WebUI: "
    "a container-side reclaim releases the claim without signalling the "
    "host worker, which can lead to a duplicate dispatch. Run "
    "`hermes kanban reclaim <task_id>` (or `reassign --reclaim`) on the "
    "Hermes host instead."
}


def worker_control_denied(method: str, path: str) -> bool:
    """True when (method, path) is a worker-control op this container must
    never execute: task reclaim or run terminate. Reclaim-first reassign
    is body-driven and checked by worker_control_reassign_denied."""
    m = (method or "").strip().upper()
    p = path or ""
    return any(m == dm and pattern.match(p) for dm, pattern in _WORKER_CONTROL_DENIED)


def worker_control_reassign_denied(body: object) -> bool:
    """True when a (validated or raw) reassign payload requests
    reclaim_first -- the reclaim half of the reassign op. Accepts the
    endpoint's Pydantic model or a plain dict so it is testable without
    fastapi; anything unrecognised FAILS CLOSED (denied)."""
    if isinstance(body, dict):
        value = body.get("reclaim_first", False)
    elif body is not None and hasattr(body, "reclaim_first"):
        value = body.reclaim_first
    else:
        return True
    return bool(value)


# PR#13 review finding (comment 4319617397): status-driven reclaim.
# The deny-list above only refuses the EXPLICIT reclaim endpoints, but the
# pinned plugin's PATCH /tasks/{id} and POST /tasks/bulk both dispatch
# status="ready" through _drag_to -> _set_status_direct, which for a
# RUNNING task NULLs claim_lock/claim_expires/worker_pid, ends the run as
# 'reclaimed', and calls _terminate_reclaimed_worker -- which no-ops unless
# the claim lock carries the LOCAL host prefix (never true in this
# container; the dispatcher claims on the Hermes host). Dragging a running
# card to Ready therefore released the claim without ever signalling the
# real worker, and recompute_ready let the dispatcher spawn a duplicate
# beside it. build_app() wraps plugin._set_status_direct with a guard built
# from this predicate; both update paths share that single chokepoint.
def worker_requeue_denied(current_status: object, requested_status: object) -> bool:
    """True when moving a task to ``requested_status`` would release a live
    worker claim through the direct status write: a running task requeued
    to 'ready'. Anything else (non-string inputs, absent tasks via None,
    other transitions) is not this op. Deliberately narrow: only the exact
    running->ready pair is refused, matching the finding's scope."""
    return current_status == "running" and requested_status == "ready"


WORKER_REQUEUE_REJECT_MSG = (
    "cannot requeue a running task to 'ready' from the Kanban WebUI: the "
    "direct status write releases the worker claim without signalling the "
    "host worker, which can lead to a duplicate dispatch. Run "
    "`hermes kanban reclaim <task_id>` on the Hermes host instead."
)

# Access-log credential hygiene. Stale browser tabs from the old
# bearer-token build still reconnect with ?token=<secret> in the query
# string; uvicorn's access logger echoes the full request line, so the
# credential would land in json-file container logs even though the app
# itself never accepts a URL credential. This filter scrubs it from every
# uvicorn.access record before the handler writes it: any token=/pass=
# style query parameter and any literal occurrence of the bootstrap
# secret. New tabs carry only the HttpOnly cookie.
_TOKEN_PARAM_RE = re.compile(r"([?&](?:token|access_token|pass|password|key|secret)=)[^&\s]*", re.IGNORECASE)
_AUTH_HEADER_RE = re.compile(r"(authorization:\s*).*$", re.IGNORECASE | re.MULTILINE)


# 3. PEER ADDRESS (round-5 finding 1, server-side half): the marker is
#    honored ONLY when the REAL TCP peer is the SWAG proxy host (or
#    loopback, for this host's own verifier). The published port is
#    additionally UFW-source-restricted to the proxy host (proved live
#    in an earlier deploy round: with the rule absent a LAN client gets
#    Connection refused), so off-proxy peers cannot open a connection
#    at all; this predicate is the app-level backstop that still holds
#    if a firewall rule ever vanishes. request.client.host is the real
#    post-DNAT socket peer (DNAT rewrites the destination only), so
#    unlike X-Forwarded-For it is NOT client-spoofable. An unset
#    expected peer disables the check (developer runs only -- the
#    deployment compose always passes the resolved proxy address).
def login_peer_allowed(peer: str, expected_proxy: str) -> bool:
    """Pure predicate: is the real TCP source address a trusted peer?
    expected_proxy empty => unset (dev/laptop run), allow; the deployed
    compose template always sets it from inventory facts."""
    if not expected_proxy:
        return True
    pres = (peer or "").strip()
    if pres.startswith("127.") or pres == "::1":
        return True
    return pres == expected_proxy.strip()


# Round-3 finding 1 + round-4/5 finding 1: the bootstrap credential is
# accepted ONLY through the trusted HTTPS proxy path, gated by THREE
# independent checks, ALL enforced before the body is parsed or the
# credential is ever compared:
#
# 1. PROXY MARKER: the SWAG vhost injects `proxy_set_header
#    X-Kanban-Proxy-Marker <secret>` where the secret lives in the
#    dedicated include /config/kanban_ui_marker.conf deployed 0600
#    at the /config root (NOT under /config/nginx, which SWAG's
#    init-permissions chmods -R g+w at every start; NEVER rendered
#    into the 0664-normalized proxy-confs vhost -- that world-readable
#    placement was the round-5 blocking finding),
#    mirrored to a 0400 file bind-mounted read-only into this container.
#    nginx proxy_set_header REPLACES any client-supplied header of that
#    name, so the value can only ever arrive from the proxy. Comparison
#    is constant-time.
# 2. TRANSPORT (defense-in-depth, round-3): SWAG sets X-Forwarded-Proto
#    from $scheme, so a request reaching the published port directly has
#    no such header. This one IS client-spoofable on a direct socket --
#    which is why it is never the sole trust signal; it only ever
#    supplements the marker and peer checks.
# 3. PEER ADDRESS: see login_peer_allowed above.
#
# Threat boundary (documented for review, role vars): the marker file
# on the proxy host is 0600 docker_user-owned (SWAG convention;
# lsiown-safe) -- readable by root/docker_user ON the proxy host but by
# NO other host and no unprivileged local account. Forging the full
# (peer + marker + proto) triple therefore requires already
# compromising SWAG itself, which is the estate-wide trust root every
# proxied service shares. UFW additionally restricts the port's source
# to the proxy host (defense-in-depth only; published ports ride the
# Docker forwarding path, which is why the app gate, not the firewall,
# is the enforcement layer). KANBAN_UI_COOKIE_INSECURE is the sole
# opt-out (local loopback development only) and the deployment compose
# template never sets it.
def login_proxy_marked(presented: str, expected: str, allow_insecure_dev: bool) -> bool:
    """Pure predicate: did the trusted proxy stamp this request with the
    shared marker? Deny-by-default: an unconfigured expected marker (empty)
    refuses EVERYTHING except the loopback-dev opt-out."""
    if allow_insecure_dev:
        return True
    if not expected:
        return False
    pres = (presented or "").strip()
    if not pres or len(pres) > 128:
        return False
    try:
        return secrets.compare_digest(pres.encode("latin-1"), expected.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        return False


def login_transport_allowed(forwarded_proto: str, allow_insecure_dev: bool) -> bool:
    """Pure predicate: is this transport trusted enough for a credential?
    X-Forwarded-Proto alone is spoofable on a direct socket, so the caller
    must ALSO pass login_proxy_marked; this predicate never stands alone."""
    if allow_insecure_dev:
        return True
    # X-Forwarded-Proto may be a comma list; the FIRST entry is the
    # client-facing protocol set by the outermost trusted proxy.
    first = (forwarded_proto or "").split(",")[0].strip().lower()
    return first == "https"


def login_token_matches(presented: object, secret: str) -> bool:
    """Pure predicate: does the presented bootstrap token match the secret?

    PR#13 review finding (comment 4301554425): the raw
    ``secrets.compare_digest(presented, secret)`` gate only accepts ASCII
    strings, so a non-ASCII JSON token such as "é" raised TypeError --
    Starlette answered 500 and, worse, the exception skipped the
    ``login_failures`` append on the invalid-credentials branch, leaving
    the brute-force throttle disengaged for exactly the inputs an attacker
    would throw at it. Comparing UTF-8 BYTES keeps the constant-time
    comparison for any Unicode presentation, and a presentation that
    cannot even be UTF-8 encoded (a lone surrogate admitted by
    ``json.loads`` on a ``\\udXXX`` escape) is treated as invalid
    credentials rather than a server error, so every failed attempt flows
    through the 401 + counter path.
    """
    if not isinstance(presented, str) or presented == "":
        return False
    try:
        return secrets.compare_digest(presented.encode("utf-8"), secret.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        return False


# PR-review finding (comment 4244239544): SameSite=strict is SITE-scoped,
# not origin-scoped -- JS on a compromised sibling subdomain of the
# internal domain gets the session cookie on ITS WebSocket upgrade and
# would read the task event stream on a cookie-only gate. Browsers always
# send the non-client-forgeable Origin on a WS upgrade, so the gate
# requires it to equal the exact configured Kanban origin (scheme://host,
# the value the compose renders from the same https URL the UI is served
# from). Deny-by-default: an unset allowed origin refuses every upgrade
# (dev runs that want the loopback path set KANBAN_UI_COOKIE_INSECURE,
# which is already the single sanctioned opt-out for every other gate).
def ws_origin_allowed(origin: str, allowed_origin: str, allow_insecure_dev: bool) -> bool:
    """Pure predicate: does this WS upgrade Origin match the Kanban origin?"""
    if allow_insecure_dev:
        return True
    if not allowed_origin:
        return False
    pres = (origin or "").strip().rstrip("/").lower()
    if not pres:
        # Non-browser clients may omit Origin; every cookie-authenticated
        # WS consumer here IS a browser, so absence is not a pass. A
        # cookie-only upgrade would also re-admit exactly the sibling-
        # subdomain attack this gate exists to close.
        return False
    exp = allowed_origin.strip().rstrip("/").lower()
    try:
        return secrets.compare_digest(pres.encode("latin-1"), exp.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        return False


def redact_credentials(msg: str, secret: str) -> str:
    """Remove credential material from a log line (pure, unit-testable)."""
    safe = _TOKEN_PARAM_RE.sub(r"\1[redacted]", msg)
    safe = _AUTH_HEADER_RE.sub(r"\1[redacted]", safe)
    if secret and secret in safe:
        safe = safe.replace(secret, "[redacted]")
    return safe


# Secret material NEVER goes inside the dictConfig dict: a dictConfig
# failure prints the offending config dict into the traceback (proved live
# by build 2026.10.10h, where a rejected inline filter spec echoed the
# bootstrap token into stdout). The factories below read the secret from
# this module global instead, which main() populates via
# configure_ui_secret() before uvicorn.Config is constructed.
_UI_SECRET = ""


def configure_ui_secret(secret: str) -> None:
    global _UI_SECRET
    _UI_SECRET = secret


class RedactingLogFilter:
    """logging.Filter that scrubs credentials from every record.

    Installed on BOTH dictConfig handlers below (via the top-level
    ``filters`` section -- handler lists only accept filter NAMES).
    Handler-level filters see records emitted by child loggers too --
    uvicorn's websockets protocol logs rejected upgrades (including stale
    tabs replaying an old ?token= URL) through uvicorn.error child
    loggers, which never pass through the uvicorn.access formatter. A
    formatter-only redaction leaked exactly those lines in build
    2026.10.10g.
    """

    def __new__(cls):
        import logging

        f = logging.Filter()

        def _filter(record: "logging.LogRecord") -> bool:
            try:
                msg = record.getMessage()
            except Exception:  # never drop a record over redaction
                return True
            red = redact_credentials(msg, _UI_SECRET)
            if red != msg:
                # PR#13 review comment 4244239564: never blank record.args
                # wholesale -- uvicorn 0.41.0's AccessFormatter.formatMessage
                # unconditionally unpacks the 5-tuple (client_addr, method,
                # full_path, http_version, status_code), and the
                # credential-bearing access records are EXACTLY the ones
                # this filter exists to scrub; clearing args turned each
                # such request into a logging exception, not a redaction.
                # Redact the string fields inside the tuple (arity kept);
                # only records whose format string itself carries the
                # material (DefaultFormatter-style uvicorn.error records)
                # collapse to the single-message form.
                args = record.args
                if isinstance(args, tuple) and args:
                    scrubbed = tuple(
                        redact_credentials(a, _UI_SECRET)
                        if isinstance(a, str) else a
                        for a in args
                    )
                    record.args = scrubbed
                    try:
                        new_msg = record.getMessage()
                    except Exception:
                        record.args = args
                    else:
                        if redact_credentials(new_msg, _UI_SECRET) == new_msg:
                            return True
                        record.args = args
                record.msg = red
                record.args = None
            return True

        f.filter = _filter
        return f


class RedactingAccessFormatter:
    """uvicorn AccessFormatter that scrubs credentials from the final line.

    Referenced from the dictConfig below via the ``()`` factory, so the
    redaction is PART of the logging configuration uvicorn applies -- not a
    logger-level filter that a later dictConfig could reset.
    """

    def __new__(cls, *args, **kwargs):
        from uvicorn.logging import AccessFormatter

        impl = AccessFormatter(*args, **kwargs)
        orig_format = impl.format  # bind BEFORE overriding (else recursion)

        def _format(record):
            # module-level lookups so tests can reconfigure both pieces
            return redact_credentials(orig_format(record), _UI_SECRET)

        impl.format = _format
        return impl


def access_log_config(secret: str) -> dict:
    """dictConfig mirroring uvicorn's defaults + redacted access lines.

    ``secret`` is only used to populate the module global that the filter
    and formatter read at emit time; the dict itself stays credential-free
    so a dictConfig failure can never echo the secret in its traceback.
    """
    configure_ui_secret(secret)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "redact_credentials": {
                "()": f"{__name__}.RedactingLogFilter",
            },
        },
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
            "access": {
                # __name__ is "__main__" when run as a script and the
                # import name under test; both resolve for dictConfig.
                "()": f"{__name__}.RedactingAccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "filters": ["redact_credentials"],
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "filters": ["redact_credentials"],
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }

sys.path.insert(0, str(REPO))
os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))


def load_secret() -> str:
    """Read the bootstrap secret; never generate one, never echo it.

    The secret is provisioned by Ansible from Ansible Vault. If it is
    missing the service refuses to start rather than minting a secret no
    one knows -- a broken deploy should be loud, not secret-shaped.
    """
    if not TOKEN_FILE.exists():
        raise SystemExit(
            f"missing bootstrap secret file {TOKEN_FILE}; it is deployed by "
            "Ansible from vault (role kanban_ui). Refusing to start."
        )
    tok = TOKEN_FILE.read_text().strip()
    if len(tok) < 32:
        raise SystemExit(f"bootstrap secret in {TOKEN_FILE} is too short; refusing to start.")
    return tok


def load_proxy_marker() -> str:
    """Read the shared proxy marker; empty string if absent.

    Deployment (role kanban_ui) provisions this file from vault on BOTH
    sides of the trust boundary: the SWAG vhost stamps it, this container
    verifies it. main() refuses to start when it is missing unless the
    loopback-development opt-out (KANBAN_UI_COOKIE_INSECURE) is set --
    a silent empty marker would lock every operator out of /login.
    """
    if not MARKER_FILE.exists():
        return ""
    return MARKER_FILE.read_text().strip()


def build_app(secret: str, proxy_marker: str = "", trusted_proxy: str = "", ws_allowed_origin: str = ""):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response
    # fastapi 0.133.1 (pinned) exports only APIRouter at top level; the
    # route CLASS lives in fastapi.routing (verified against the image).
    from fastapi.routing import APIRoute

    spec = importlib.util.spec_from_file_location("kanban_plugin_api", PLUGIN / "plugin_api.py")
    plugin = importlib.util.module_from_spec(spec)
    sys.modules["kanban_plugin_api"] = plugin
    spec.loader.exec_module(plugin)

    app = FastAPI(title="Hermes Kanban UI", docs_url=None, redoc_url=None)

    # ---- session store (in-process; bounded; sliding expiry) -------------
    sessions: dict[str, float] = {}

    def _prune_sessions() -> None:
        now = time.time()
        for tok in [t for t, exp in sessions.items() if exp < now]:
            sessions.pop(tok, None)
        while len(sessions) >= MAX_SESSIONS:
            sessions.pop(next(iter(sessions)))

    def _session_valid(presented: str) -> bool:
        if not presented:
            return False
        exp = sessions.get(presented)
        if exp is None:
            return False
        if exp < time.time():
            sessions.pop(presented, None)
            return False
        sessions[presented] = time.time() + SESSION_TTL_SECONDS
        return True

    # ---- login brute-force throttle --------------------------------------
    login_failures: list[float] = []

    def _login_throttled() -> bool:
        now = time.time()
        login_failures[:] = [t for t in login_failures if now - t < LOGIN_FAIL_WINDOW]
        return len(login_failures) >= LOGIN_FAIL_LIMIT

    @app.middleware("http")
    async def worker_control_mw(request: Request, call_next):
        # PR#13 review finding 4301554389 (path-deny half; the body-driven
        # reassign guard is wired after include_router below). Stateless
        # reclaim/run-terminate paths are refused here BEFORE routing
        # begins -- no body parse, no DB access. Registered BEFORE
        # auth_mw: Starlette makes the LAST-registered middleware the
        # OUTERMOST, so the session gate runs first and an anonymous
        # probe still gets 401 -- the reject body (which names operator
        # workflows) is only ever visible to a session-holder.
        if worker_control_denied(request.method, request.url.path):
            return JSONResponse(WORKER_CONTROL_REJECT_BODY, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            cookie = request.cookies.get(SESSION_COOKIE, "")
            if not _session_valid(cookie):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            # Defense-in-depth CSRF guard: same-origin JS sets this header on
            # fetch() calls; a cross-site form/JSON post cannot set custom
            # headers. The session cookie is SameSite=strict (never sent on
            # any cross-site request), and GET navigations (attachment
            # downloads) legitimately carry only the cookie.
            if request.method not in ("GET", "HEAD") and request.headers.get(SESSION_HEADER.lower()) != "1":
                return JSONResponse({"detail": "missing session header"}, status_code=403)
        return await call_next(request)

    app.include_router(plugin.router, prefix="/api/kanban")

    # ---- worker-control deny-list (PR#13 review finding 4301554389) ------
    # This container is NOT the worker's host. The pinned Hermes reclaim
    # path clears the claim and returns the task to ready unconditionally
    # (hermes_cli/kanban_db.py reclaim_task), but the SIGTERM/SIGKILL half
    # (_terminate_reclaimed_worker in kanban_db_dispatch.py) bails out
    # with host_local=False whenever the claim lock does not start with
    # the local host prefix. The dispatcher claims on the HERMES host, so
    # a reclaim issued from inside this container releases the claim
    # without ever signalling the real worker: the host worker keeps
    # running while the dispatcher is free to spawn a duplicate beside
    # it. Until there is a host-side worker-control channel, these three
    # operations are refused here BEFORE any DB access, and the UI
    # controls that would call them are disabled (static/app.js). Plain
    # reassign (reclaim_first=false) is unaffected: it never touches
    # reclaim_task. Operators on the Hermes host remain the sanctioned
    # path (`hermes kanban reclaim <id>` / `hermes kanban reassign ...
    # --reclaim`; the dashboard's own terminate/reclaim buttons are the
    # host dashboard, not this container).
    #
    # The path-deny half (reclaim, run terminate) lives in
    # worker_control_mw above. The reassign refusal depends on the PARSED
    # body (reclaim_first), so it runs as a wrapper around the endpoint's
    # own call -- swapped into route.dependant.call, which is exactly
    # what FastAPI invokes per request AFTER it has parsed and validated
    # the body with the upstream ReassignBody model. Upstream validation
    # semantics (422 shapes, defaults) are therefore untouched, and the
    # guard refuses before the handler calls reassign_task/reclaim_task.
    _reassign_path = "/api/kanban/tasks/{task_id}/reassign"
    _reassign_routes = [
        r for r in app.routes
        if isinstance(r, APIRoute) and r.path == _reassign_path
        and "POST" in (r.methods or set())
    ]
    # Fail closed: this wrapper is the only thing stopping a body-driven
    # reclaim through reassign. If the upstream endpoint is ever renamed,
    # removed, or duplicated so the hook cannot attach (or attach twice),
    # refuse to boot rather than serve the op unguarded -- same
    # philosophy as the proxy-marker gate.
    if len(_reassign_routes) != 1 or getattr(_reassign_routes[0], "dependant", None) is None \
            or not callable(_reassign_routes[0].dependant.call):
        raise SystemExit(
            f"worker-control guard could not attach to {_reassign_path}; "
            "refusing to start with the reclaim-first reassign path unguarded "
            "(mounted plugin router drifted from the pinned Hermes checkout)."
        )
    _reassign_route = _reassign_routes[0]
    _reassign_upstream_call = _reassign_route.dependant.call
    # FastAPI captured is_coroutine_callable at route construction and
    # dispatches dependant.call accordingly (sync => threadpool). The
    # wrapper must keep that SAME dispatch shape; a mismatch would hand
    # run_in_threadpool a coroutine function. The pinned upstream is a
    # sync def; both shapes are supported so a future upstream flip to
    # async stays correct, anything else is drift and fails closed.
    _upstream_is_async = bool(
        getattr(_reassign_route.dependant, "is_coroutine_callable", False)
    )
    if not _upstream_is_async and asyncio.iscoroutinefunction(_reassign_upstream_call):
        raise SystemExit(
            f"worker-control guard: {_reassign_path} dispatch shape drifted "
            "(dependant.is_coroutine_callable disagrees with the callable); "
            "refusing to start unguarded."
        )

    if _upstream_is_async:
        async def _reassign_guarded_call(**values):
            if worker_control_reassign_denied(values.get("payload", values.get("body"))):
                return JSONResponse(WORKER_CONTROL_REJECT_BODY, status_code=403)
            return await _reassign_upstream_call(**values)
    else:
        def _reassign_guarded_call(**values):  # sync: FastAPI threadpools it
            if worker_control_reassign_denied(values.get("payload", values.get("body"))):
                return JSONResponse(WORKER_CONTROL_REJECT_BODY, status_code=403)
            return _reassign_upstream_call(**values)

    _reassign_guarded_call.__name__ = getattr(_reassign_upstream_call, "__name__", "reassign")
    _reassign_guarded_call.__qualname__ = "reassign_task_endpoint[worker-control-guarded]"
    _reassign_guarded_call._worker_control_guarded = True
    _reassign_guarded_call._worker_control_upstream = _reassign_upstream_call
    _reassign_route.dependant.call = _reassign_guarded_call

    # ---- status-driven reclaim guard (PR#13 review finding 4319617397) ----
    # The deny-list middleware above refuses the EXPLICIT reclaim endpoints,
    # but a running-card -> ready drag reaches the same claim release
    # through plugin._set_status_direct: that helper NULLs the claim
    # columns, ends the run as 'reclaimed', and its post-commit
    # _terminate_reclaimed_worker no-ops for any claim lock that does not
    # carry the LOCAL host prefix (inside this container: never), so the
    # host worker keeps running while recompute_ready invites a duplicate
    # dispatch. Both update paths -- PATCH /tasks/{id} (_patch_status) and
    # POST /tasks/bulk (_bulk_apply_one) -- funnel through _drag_to ->
    # _set_status_direct for the raw ready write, so wrapping that ONE
    # module-level function guards BOTH paths at the single chokepoint.
    # _drag_to and _STATUS_HANDLERS resolve _set_status_direct as a module
    # global at call time (same mechanism as the WS-gate override below),
    # so the swap is authoritative without touching the pinned checkout.
    # The refuse channel is upstream's own _StatusRejected: _patch_status
    # maps it via _map_errors(400, _StatusRejected, ValueError) -> 400 +
    # actionable detail, and bulk_update's per-id handler records it as
    # {ok: false, error: <message>} without aborting siblings. The check
    # runs BEFORE the write_txn opens -- a refused transition never touches
    # the DB. Fail closed on router drift: if upstream renames the helper
    # or the error class, refuse to boot rather than serve ready-requeue
    # unguarded (same philosophy as the reassign guard and proxy marker).
    _upstream_set_status = getattr(plugin, "_set_status_direct", None)
    _status_rejected_cls = getattr(plugin, "_StatusRejected", None)
    if not callable(_upstream_set_status) or not (
        isinstance(_status_rejected_cls, type) and issubclass(_status_rejected_cls, Exception)
    ):
        raise SystemExit(
            "worker-control guard could not attach to plugin._set_status_direct; "
            "refusing to start with the running->ready status requeue unguarded "
            "(mounted plugin router drifted from the pinned Hermes checkout)."
        )

    def _set_status_direct_guarded(conn, task_id, new_status):
        # Read the CURRENT status through upstream's own accessor; an
        # absent task (None) is not a running claim and passes through to
        # upstream's own not-found handling. Only the exact running->ready
        # pair is refused; every other transition (todo<->ready for idle
        # cards, running->blocked/scheduled/done/review, ...) is untouched.
        current = plugin.kanban_db.get_task(conn, task_id)
        if worker_requeue_denied(getattr(current, "status", None), new_status):
            raise _status_rejected_cls(WORKER_REQUEUE_REJECT_MSG)
        return _upstream_set_status(conn, task_id, new_status)

    _set_status_direct_guarded.__name__ = "_set_status_direct"
    _set_status_direct_guarded.__qualname__ = "_set_status_direct[worker-control-guarded]"
    _set_status_direct_guarded._worker_control_guarded = True
    _set_status_direct_guarded._worker_control_upstream = _upstream_set_status
    plugin._set_status_direct = _set_status_direct_guarded

    # Override the plugin's WS upgrade authorization. The upstream default
    # delegates to web_server._ws_auth_ok, which only understands ?token=
    # query credentials and lazily imports the huge dashboard module (that
    # import fails in this slim image, where the upstream default would
    # fail OPEN -- unacceptable). stream_events resolves this module global
    # at call time, so replacing it here is authoritative. Authorization is
    # the session cookie AND an exact-Origin match (PR review comment
    # 4244239544): SameSite=strict is site- not origin-scoped, so a
    # compromised sibling subdomain's JS would otherwise get the cookie on
    # its WS upgrade; the browser-supplied Origin pins the upgrade to the
    # Kanban origin itself. No credential ever rides in a URL (no browser
    # history, no access logs, no query-param fallback of any kind).
    def _ws_gate(ws):
        if not _session_valid(ws.cookies.get(SESSION_COOKIE, "")):
            return False
        return ws_origin_allowed(
            ws.headers.get("origin", ""),
            ws_allowed_origin,
            bool(os.environ.get("KANBAN_UI_COOKIE_INSECURE")),
        )

    plugin._ws_upgrade_authorized = _ws_gate

    @app.get("/api/health")
    def health():
        return {"ok": True, "default_board": DEFAULT_BOARD}

    @app.post("/login")
    async def login(req: Request):
        # Round-3 finding 1 + round-4 finding 1: reject any request that
        # did not arrive stamped by the trusted proxy BEFORE touching the
        # body or the secret. Two independent gates, both cheap and both
        # ahead of any credential touch:
        #   1. the vaulted proxy marker (nginx proxy_set_header REPLACES
        #      any client-supplied value, so it cannot be forged or
        #      observed by a direct client), and
        #   2. the transport predicate (X-Forwarded-Proto from SWAG's
        #      $scheme), which is defense-in-depth ONLY -- spoofable on
        #      its own, never trusted alone.
        # A direct plaintext client -- with OR without a forged
        # X-Forwarded-Proto: https -- gets 403 here, so the bootstrap
        # credential is never compared and no session is ever issued on
        # an untrusted request.
        if not login_proxy_marked(
            req.headers.get("x-kanban-proxy-marker", ""),
            proxy_marker,
            bool(os.environ.get("KANBAN_UI_COOKIE_INSECURE")),
        ):
            return JSONResponse(
                {"detail": "login requires the HTTPS endpoint"}, status_code=403
            )
        # Round-5 finding 1: the marker alone is a bearer string -- the
        # AND-gate also requires the REAL TCP peer to be the trusted
        # proxy host (loopback allowed for this host's own verifier;
        # the loopback-dev opt-out skips it). req.client is the real
        # post-DNAT socket peer; no header is consulted here.
        if not login_peer_allowed(
            req.client.host if req.client else "",
            "" if os.environ.get("KANBAN_UI_COOKIE_INSECURE") else trusted_proxy,
        ):
            return JSONResponse(
                {"detail": "login requires the HTTPS endpoint"}, status_code=403
            )
        if not login_transport_allowed(
            req.headers.get("x-forwarded-proto", ""),
            bool(os.environ.get("KANBAN_UI_COOKIE_INSECURE")),
        ):
            return JSONResponse(
                {"detail": "login requires the HTTPS endpoint"}, status_code=403
            )
        if _login_throttled():
            return JSONResponse({"detail": "too many failed attempts"}, status_code=429)
        try:
            body = await req.json()
        except Exception:
            body = None
        presented = body.get("token") if isinstance(body, dict) else None
        ok = login_token_matches(presented, secret)
        if not ok:
            login_failures.append(time.time())
            return JSONResponse({"detail": "invalid credentials"}, status_code=401)
        _prune_sessions()
        session = secrets.token_urlsafe(32)
        sessions[session] = time.time() + SESSION_TTL_SECONDS
        resp = JSONResponse({"ok": True, "default_board": DEFAULT_BOARD})
        resp.set_cookie(
            SESSION_COOKIE,
            session,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            # Secure by DEFAULT (round-2 finding 2): the deployed path is
            # HTTPS-only through SWAG and the verifier refuses any
            # http:// base, so a session cookie must never be stored or
            # replayed over plaintext. Local development over loopback
            # is the ONLY sanctioned exception, via an explicit
            # KANBAN_UI_COOKIE_INSECURE=1 opt-in that the deployment
            # compose template must never set.
            secure=not os.environ.get("KANBAN_UI_COOKIE_INSECURE"),
            path="/",
        )
        return resp

    @app.post("/logout")
    async def logout(req: Request):
        cookie = req.cookies.get(SESSION_COOKIE, "")
        sessions.pop(cookie, None)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # Static shell: served anonymously, deliberately contains NO credential.
    # Only the board slug (an operator-controlled value from role vars,
    # never user input) is interpolated, into a data attribute.
    csp = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self' ws: wss:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    )

    @app.get("/")
    def index():
        html = (STATIC / "index.html").read_text()
        return Response(
            content=html.replace("__BOARD__", DEFAULT_BOARD),
            media_type="text/html",
            headers={"Content-Security-Policy": csp},
        )

    @app.get("/app.js")
    def app_js():
        return FileResponse(STATIC / "app.js", media_type="text/javascript", headers={"Content-Security-Policy": csp})

    @app.get("/md.js")
    def md_js():
        return FileResponse(STATIC / "md.js", media_type="text/javascript", headers={"Content-Security-Policy": csp})

    @app.get("/app.css")
    def app_css():
        return FileResponse(STATIC / "app.css", media_type="text/css", headers={"Content-Security-Policy": csp})

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9470)
    ap.add_argument("--host", default=None)
    ap.add_argument("--lan", action="store_true", help="bind 0.0.0.0 (session cookie still required)")
    args = ap.parse_args()

    secret = load_secret()
    host = args.host or ("0.0.0.0" if (args.lan or os.environ.get("KANBAN_UI_LAN") == "1") else "127.0.0.1")

    # Deny-by-default boot gate (round-4): without the proxy marker the
    # ONLY login path would be one an operator cannot produce, so a
    # missing file is a broken deploy, not a degraded mode. Loud, like
    # the bootstrap secret gate above; the loopback-dev opt-out is the
    # single sanctioned exception.
    proxy_marker = load_proxy_marker()
    if not proxy_marker and not os.environ.get("KANBAN_UI_COOKIE_INSECURE"):
        raise SystemExit(
            f"missing proxy marker file {MARKER_FILE}; it is deployed by "
            "Ansible from vault alongside the bootstrap secret (role "
            "kanban_ui). Refusing to start with an unforgeable-login gate "
            "that no request can satisfy."
        )

    app = build_app(secret, proxy_marker, TRUSTED_PROXY_IP, WS_ALLOWED_ORIGIN)

    # Loud-not-secret-shaped again (same philosophy as the marker gate),
    # but WARN rather than refuse: an unset WS origin degrades only the
    # live event tail (every upgrade denied), while login and the board
    # still work -- unlike a missing marker, it must not lock the
    # operator out of the very UI they need to fix the deploy.
    if not WS_ALLOWED_ORIGIN and not os.environ.get("KANBAN_UI_COOKIE_INSECURE"):
        print(
            "WARNING: KANBAN_UI_WS_ALLOWED_ORIGIN is unset; the /events "
            "WebSocket will refuse every upgrade (deny-by-default). The "
            "deployment compose always renders it from the SWAG HTTPS URL.",
            flush=True,
        )

    import uvicorn

    # Access lines may echo stale-tab URL query strings; the redaction is
    # baked INTO the dictConfig we hand uvicorn (RedactingAccessFormatter
    # above), so it cannot be reset by a later logging reconfiguration.
    config = uvicorn.Config(
        app,
        host=host,
        port=args.port,
        log_level="info",
        access_log=True,
        log_config=access_log_config(secret),
    )

    # Never log the secret; sessions/tokens are credentials, not diagnostics.
    print(f"kanban UI      : http://{host}:{args.port} (login required)", flush=True)
    print(f"default board  : {DEFAULT_BOARD}", flush=True)
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
