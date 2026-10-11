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
- The /events WebSocket is authenticated by cookie only (browsers send
  cookies on same-origin WS upgrades). There is NO query-parameter token
  path at all: no credential ever rides in a URL.

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

SESSION_COOKIE = "kanban_ui_session"
SESSION_HEADER = "X-Kanban-UI"
# Sessions are in-process, so they only need to outlive a browsing visit;
# the container restarting simply logs everyone out (re-login is cheap).
SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 512
LOGIN_FAIL_LIMIT = 20
LOGIN_FAIL_WINDOW = 60

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


def build_app(secret: str, proxy_marker: str = "", trusted_proxy: str = ""):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response

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

    # Override the plugin's WS upgrade authorization. The upstream default
    # delegates to web_server._ws_auth_ok, which only understands ?token=
    # query credentials and lazily imports the huge dashboard module (that
    # import fails in this slim image, where the upstream default would
    # fail OPEN -- unacceptable). stream_events resolves this module global
    # at call time, so replacing it here is authoritative. Authorization is
    # the session cookie ONLY: browsers send it on the same-origin WS
    # upgrade, and no credential ever rides in a URL (no browser history,
    # no access logs, no query-param fallback of any kind).
    def _ws_gate(ws):
        return _session_valid(ws.cookies.get(SESSION_COOKIE, ""))

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
        ok = isinstance(presented, str) and presented != "" and secrets.compare_digest(presented, secret)
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

    app = build_app(secret, proxy_marker, TRUSTED_PROXY_IP)

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
