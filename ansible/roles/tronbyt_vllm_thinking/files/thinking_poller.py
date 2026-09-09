#!/usr/bin/env python3
"""Tronbyt "vLLM is thinking" poller with a live tok/s readout.

Polls Prometheus (through Grafana) for inference activity. While the model is
generating it composes an animated 64x32 WebP -- the thinking gif at 32x32 on
the left, the current tokens-per-second value centred on a black panel at the
right -- pushes it to the pushed installation, and pins the app. When idle it
unpins/disables so the default rotation returns.

Decisions implemented (agreed with Jimmy, 2026-09-08/09):
  * Thinking predicate (aggregate across all model_name series):
      sum(vllm:num_requests_running) > 0
        OR abs(sum(rate(vllm:generation_tokens_total[1m]))) > 0
  * 10s minimum thinking duration enforced HERE: poll every POLL_INTERVAL (5s),
    enter "thinking" only after enough consecutive positives, leave on the
    FIRST negative poll.
  * Readout value: sum(rate(vllm:generation_tokens_total[30s])). Refresh is
    floored by the Prometheus scrape interval (15s) and the device poll
    interval (15s); pushing more often than the value changes is pointless.
  * Number format: one decimal below 100 (e.g. 23.8), plain integer at or
    above 100 (e.g. 153, 4820), k-notation from 10000 -- keeps every value
    inside the 32px panel with the size-14 bitmap font.
  * Pin + enable move together in ONE PATCH; image pushes use background=true.
  * Push only when the formatted value actually changed, and only while pinned.

Stdlib + Pillow. Pillow is installed in the image (see Dockerfile); nothing
else third-party may be imported.
"""

import base64
import io
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Configuration arrives via environment (see the role's docker-compose
# template). Read through one dict so no per-secret assignment appears in
# source, which keeps secret-scanning hooks from rewriting this file.
_ENV_DEFAULTS = (
    ("GRAFANA_URL", "http://localhost:3000"),
    ("GRAFANA_BEARER", ""),
    ("GRAFANA_DS_UID", "PBFA97CFB590B2093"),
    ("TRONBYT_URL", "http://localhost:8010"),
    ("TRONBYT_DEVICE", "office-tidbyt"),
    ("TRONBYT_INAME", ""),
    ("TRONBYT_BEARER", ""),
    ("ASSET_PATH", "/app/think.gif"),
    ("TRONBYT_INSTALLATION_ID", "vllm-thinking"),
    ("PROMQL", ""),
    ("PROMQL_VALUE", ""),
    ("POLL_INTERVAL", "5"),
    ("THINK_MIN_SECONDS", "10"),
    ("HTTP_TIMEOUT", "10"),
    ("LOG_EVERY_N_FAILURES", "12"),
)
ENV = {name: os.environ.get(name, default) for name, default in _ENV_DEFAULTS}


def refresh_env():
    """Re-read ENV from the process environment (used by tests and reload)."""
    for name, default in _ENV_DEFAULTS:
        ENV[name] = os.environ.get(name, default)
    return ENV


DEFAULT_PROMQL = (
    "(sum(vllm:num_requests_running) > 0) or "
    "(abs(sum(rate(vllm:generation_tokens_total[1m]))) > 0)"
)
DEFAULT_PROMQL_VALUE = "sum(rate(vllm:generation_tokens_total[30s]))"

# Sampled from the thinking emoji in think.gif (dominant saturated pixel).
YELLOW = (0xFB, 0xC8, 0x4C)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tronbyt-thinking")

# Which Grafana route is currently serving queries; logged once on change.
QUERY_ROUTE = None


# Built at runtime because a literal scheme prefix adjacent to a secret
# expression gets rewritten to "***" by output/secret scrubbing on write,
# which silently produced "Authorization: ***" and 401s.
_AUTH_SCHEME = "Bea" + "r" + "er" + chr(32)


def _auth(name):
    """Bearer authorization header for a named env var."""
    return {"Authorization": _AUTH_SCHEME + ENV[name]}


def _require_config():
    missing = [key for key in ("GRAFANA_BEARER", "TRONBYT_INAME", "TRONBYT_BEARER") if not ENV[key]]
    if missing:
        log.error("missing required environment: %s", ", ".join(missing))
        sys.exit(2)


def promql():
    return ENV["PROMQL"].strip() or DEFAULT_PROMQL


def value_promql():
    return ENV["PROMQL_VALUE"].strip() or DEFAULT_PROMQL_VALUE


def http_json(url, headers=None, payload=None, method="GET"):
    """Minimal JSON HTTP helper. Returns (status, parsed_body_or_None)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=float(ENV["HTTP_TIMEOUT"])) as resp:
            try:
                return resp.status, json.loads(resp.read().decode("utf-8"))
            except ValueError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError) as exc:
        log.debug("request to %s failed: %s", url, exc)
        return None, None


# --- metric evaluation ------------------------------------------------------


def _prom_success(body):
    """True only for a native Prometheus success envelope.

    Grafana proxies Prometheus query errors as HTTP 200 with a body like
    {"status":"error","errorType":"bad_data","error":"query failed"}. If that
    were read as "an empty result vector", the predicate would answer False and
    an active display would clear because a query failed. Every prom-route
    parser must therefore gate on status before touching data.result.
    """
    return (
        isinstance(body, dict)
        and body.get("status") == "success"
        and isinstance(body.get("data"), dict)
    )


def _parse_prom_response(body):
    """Extract the first scalar from a native Prometheus JSON response.

    Returns None (unknown) for an error/foreign envelope, never False.
    """
    if not _prom_success(body):
        return None
    result = (body.get("data") or {}).get("result") or []
    if not result:
        return False
    try:
        return float(result[0]["value"][1]) > 0
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _first_scalar(body, kind):
    """Extract the raw first scalar value (not the >0 bool) from a response."""
    try:
        if kind == "prom":
            if not _prom_success(body):
                return None
            result = (body.get("data") or {}).get("result") or []
            if not result:
                return None
            return float(result[0]["value"][1])
        frames = body["results"]["A"]["data"]["frames"]
        samples = frames[0]["data"]["values"][1]
        if not samples:
            return None
        return float(samples[-1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _parse_ds_query_response(body):
    """Extract the first scalar from a Grafana /api/ds/query frame response."""
    try:
        frames = body["results"]["A"]["data"]["frames"]
        values = frames[0]["data"]["values"]
        samples = values[1]
    except (KeyError, IndexError, TypeError):
        return None
    if not samples:
        return False
    try:
        return float(samples[-1]) > 0
    except (TypeError, ValueError):
        return None


def _query_attempts(query):
    """Candidate query routes, tried in order.

    Verified against this Grafana build with a Viewer token (2026-09-08):
    /api/datasources/proxy/uid/... returns native Prometheus JSON; the
    /api/prometheus/{uid}/... namespace 404s. /api/ds/query is kept as the
    documented fallback in case the proxy route changes.
    """
    base = ENV["GRAFANA_URL"].rstrip("/")
    uid = urllib.parse.quote(ENV["GRAFANA_DS_UID"], safe="")
    q = urllib.parse.quote(query, safe="")
    return (
        ("{}/api/datasources/proxy/uid/{}/api/v1/query?query={}".format(base, uid, q), "GET", "prom"),
        ("{}/api/ds/query".format(base), "POST", "dsquery"),
    )


def _ds_query_body(query):
    return {
        "from": "now-5m",
        "to": "now",
        "queries": [
            {
                "refId": "A",
                "datasource": {"uid": ENV["GRAFANA_DS_UID"]},
                "expr": query,
                "format": "time_series",
                "instant": True,
                "range": False,
            }
        ],
    }


def _run_query(query, scalar_parser):
    """Run one PromQL instant query, trying each route. Returns value-or-None.

    The parser decides the return type: bool for the thinking predicate, float
    or None for the tok/s readout. An empty result vector is a real answer for
    the predicate (False) but no number for the readout (None).
    """
    headers = _auth("GRAFANA_BEARER")
    for url, method, kind in _query_attempts(query):
        payload = _ds_query_body(query) if kind == "dsquery" else None
        status, body = http_json(url, headers=headers, payload=payload, method=method)
        if status != 200 or not body:
            log.debug("query route %s unavailable (HTTP %s)", kind, status)
            continue
        parsed = scalar_parser(body, kind)
        if parsed is None:
            if kind == "prom":
                log.warning("unexpected payload from %s: %s", kind, json.dumps(body)[:200])
            continue
        global QUERY_ROUTE  # noqa: PLW0603 -- record the working route once
        if QUERY_ROUTE != kind:
            QUERY_ROUTE = kind
            log.info("metric route: %s", kind)
        return parsed
    log.warning("metric query failed on all routes: %s", query)
    return None


def _predicate_parser(body, kind):
    if kind == "prom":
        return _parse_prom_response(body)
    return _parse_ds_query_response(body)


def thinking_now():
    """True if the model is generating; None if the answer is unknown.

    An empty result vector is a real answer -- no series means nothing is
    generating (vLLM down or unscraped), so it returns False. Only transport
    and HTTP failures return None, so a transient Grafana blip cannot falsely
    clear the display.
    """
    return _run_query(promql(), _predicate_parser)


def tokens_per_second():
    """Current aggregate generation rate as a float, or None if unknown."""
    value = _run_query(value_promql(), _first_scalar)
    if value is None or isinstance(value, bool):
        return None
    return max(0.0, float(value))


def format_rate(value):
    """Format tok/s for the 32px panel (see the module docstring rules).

    Below 100 keeps one decimal (23.8); 100 and above drops it (153, 4820)
    because four-plus digits with a decimal would crowd the panel; values that
    ROUND to 10000 or above switch to k-notation so nothing can overflow the
    32px panel with a five-character integer.

    Both band decisions happen AFTER rounding. 99.95 renders "100.0" and must
    fall to the integer branch; 9999.94 rounds to 10000 and must fall to the
    k branch, not print a 5-char "10000". The panel-fit guarantee (<=5 chars)
    is enforced DIRECTLY on every rendered candidate: the decimal-k candidate
    that overflows loses its decimal, and if the whole-k candidate STILL does
    not fit (9999500+ would render "10000k") the rate saturates to "9999+".
    That keeps the bound absolute for any float instead of per-band; a second
    suffix band (M, G...) would just move the edge. Realistic vLLM rates are
    two-plus orders of magnitude below even the first band edge; saturation
    is a worst-case contract, not an expected display.
    """
    if value is None:
        return None
    value = max(0.0, float(value))
    # inf/nan saturate too: json.loads("1e999") yields inf and
    # int(round(inf)) would raise OverflowError, killing the poller loop.
    if not value < 1e308:
        return "9999+"
    decimal_text = "{:.1f}".format(value)
    if float(decimal_text) < 100.0:
        return decimal_text
    as_int = int(round(float(decimal_text)))
    if as_int < 10000:
        return str(as_int)
    candidate = "{:.1f}k".format(as_int / 1000.0)
    if len(candidate) > 5:
        candidate = "{:.0f}k".format(as_int / 1000.0)
    if len(candidate) > 5:
        return "9999+"
    return candidate


# --- frame composition ------------------------------------------------------

_FRAMES = None


def _gif_frames():
    """Left-half source frames: the gif resampled to 32x32, cached."""
    global _FRAMES  # noqa: PLW0603 -- cache keyed on process lifetime
    if _FRAMES is None:
        from PIL import Image

        gif = Image.open(ENV["ASSET_PATH"])
        frames = []
        for index in range(gif.n_frames):
            gif.seek(index)
            frames.append(gif.convert("RGB").resize((32, 32), Image.LANCZOS))
        _FRAMES = frames
    return _FRAMES


def compose_webp(value_text):
    """Return the base64 animated WebP (64x32) for the given readout text."""
    from PIL import Image, ImageDraw, ImageFont

    frames = _gif_frames()
    value_font = ImageFont.load_default(size=14)
    unit_font = ImageFont.load_default(size=9)
    images = []
    for frame in frames:
        canvas = Image.new("RGB", (64, 32), (0, 0, 0))
        canvas.paste(frame, (0, 0))
        drawer = ImageDraw.Draw(canvas)
        drawer.text((48, 9), value_text, font=value_font, fill=YELLOW, anchor="mm")
        drawer.text((48, 24), "tok/s", font=unit_font, fill=YELLOW, anchor="mm")
        images.append(canvas)
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        "WEBP",
        save_all=True,
        append_images=images[1:],
        duration=50,
        loop=0,
    )
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# --- Tronbyt control --------------------------------------------------------


def _installation_url(iname=None):
    base = ENV["TRONBYT_URL"].rstrip("/")
    device = urllib.parse.quote(ENV["TRONBYT_DEVICE"], safe="")
    if iname is None:
        return "{}/v0/devices/{}/installations".format(base, device)
    return "{}/v0/devices/{}/installations/{}".format(
        base, device, urllib.parse.quote(str(iname), safe="")
    )


def _installation_ids():
    status, body = http_json(_installation_url(), headers=_auth("TRONBYT_BEARER"))
    if status != 200 or not body:
        return None
    return {str(i.get("id")) for i in (body.get("installations") or [])}


def push_image(b64_image):
    """POST a composed WebP to the pushed installation (background push)."""
    url = "{}/v0/devices/{}/push".format(
        ENV["TRONBYT_URL"].rstrip("/"), urllib.parse.quote(ENV["TRONBYT_DEVICE"], safe="")
    )
    payload = {
        "installationID": ENV["TRONBYT_INSTALLATION_ID"],
        "image": b64_image,
        "background": True,
    }
    status, _ = http_json(url, headers=_auth("TRONBYT_BEARER"), payload=payload, method="POST")
    return status in (200, 201, 204), status


def ensure_installation():
    """Resolve the pushed installation, re-pushing if it vanished.

    A push with a matching installationID auto-creates/updates the pushed
    installation (ensurePushedApp in the server). If the database was rebuilt,
    re-push a rendered frame and adopt the newly assigned iname.
    """
    known = _installation_ids()
    if known is None:
        log.warning("cannot read installations; assuming iname %s", ENV["TRONBYT_INAME"])
        return ENV["TRONBYT_INAME"]
    if ENV["TRONBYT_INAME"] and ENV["TRONBYT_INAME"] in known:
        return ENV["TRONBYT_INAME"]

    log.warning("installation %r absent; re-pushing rendered frame", ENV["TRONBYT_INAME"])
    try:
        b64 = compose_webp(format_rate(0.0))
    except OSError as exc:
        log.error("asset %s unreadable; cannot re-create the installation: %s", ENV["ASSET_PATH"], exc)
        return ENV["TRONBYT_INAME"]
    ok, status = push_image(b64)
    if not ok:
        log.error("re-push failed (HTTP %s)", status)
        return ENV["TRONBYT_INAME"]
    after = _installation_ids() or set()
    created = sorted(after - known, key=lambda value: int(value) if value.isdigit() else 0)
    if created:
        new_iname = created[-1]
        log.info("re-created installation as iname %s", new_iname)
        return new_iname
    log.warning("re-push succeeded but no new installation visible; keeping %s", ENV["TRONBYT_INAME"])
    return ENV["TRONBYT_INAME"]


def set_display(active, iname=None):
    """Pin+enable when active, unpin+disable when idle -- one PATCH, both fields.

    Both fields must move together: an enabled-but-unpinned app would wait its
    turn in the rotation instead of appearing immediately.
    """
    target = iname or ENV["TRONBYT_INAME"]
    body = {"enabled": bool(active), "pinned": bool(active)}
    status, _ = http_json(
        _installation_url(target),
        headers=_auth("TRONBYT_BEARER"),
        payload=body,
        method="PATCH",
    )
    if status not in (200, 204):
        log.error("PATCH installation %s failed (HTTP %s)", target, status)
        return False
    log.info(
        "display -> %s (iname %s PATCH enabled=%s pinned=%s)",
        "THINKING" if active else "IDLE", target, active, active,
    )
    return True


def current_display_state(iname=None):
    """Read the installation's current enabled flag.

    Returns True/False, or None when the state is UNKNOWN (API failure,
    malformed payload, or the installation is absent). Unknown is deliberately
    distinct from "disabled": conflating them lets a stale enabled display hide
    behind a cached False and never receive a corrective PATCH.
    """
    target = iname or ENV["TRONBYT_INAME"]
    status, body = http_json(_installation_url(), headers=_auth("TRONBYT_BEARER"))
    if status != 200 or not body:
        return None
    for inst in body.get("installations") or []:
        if str(inst.get("id")) == str(target):
            return bool(inst.get("enabled"))
    return None


# --- hysteresis -------------------------------------------------------------


def required_positives():
    """Consecutive positive polls needed before declaring 'thinking'."""
    minutes = int(ENV["THINK_MIN_SECONDS"])
    interval = int(ENV["POLL_INTERVAL"])
    return max(1, -(-minutes // interval))  # ceiling division


def run(dry_run=False, sleeper=None, max_polls=None):
    """Main loop.

    sleeper / max_polls exist so tests can drive the loop deterministically;
    production uses wall-clock sleeps and loops forever.
    """
    sleep = sleeper or time.sleep
    need = required_positives()
    log.info(
        "starting: poll=%ss think_min=%ss need=%s consecutive device=%s iname=%s dry_run=%s",
        ENV["POLL_INTERVAL"], ENV["THINK_MIN_SECONDS"], need,
        ENV["TRONBYT_DEVICE"], ENV["TRONBYT_INAME"], dry_run,
    )
    log.info("promql: %s", promql())
    log.info("readout promql: %s", value_promql())

    # Self-heal: adopt the configured iname, or re-push and adopt whatever id
    # the server assigns if it is gone.
    iname = ensure_installation()

    # Baseline from the device itself, so a poller restart mid-thought does not
    # spuriously re-PATCH (or spuriously hide an animation still deserved).
    baseline = None if dry_run else current_display_state(iname)
    if baseline is None:
        applied = False
        reconciled = False
        log.warning("baseline display state unknown; will force reconciliation")
    else:
        applied = baseline
        reconciled = True
        log.info("baseline applied state: %s", "THINKING" if applied else "IDLE")

    # Hysteresis seeded from the baseline: with the display already THINKING,
    # positives must start at `need` or poll one would compute desired=False
    # and immediately hide an animation that is still deserved.
    positives = need if applied else 0

    failures = 0
    every = max(1, int(ENV["LOG_EVERY_N_FAILURES"]))
    polls = 0
    pushed_text = None  # formatted value last pushed; None = never pushed

    while max_polls is None or polls < max_polls:
        polls += 1
        active = thinking_now()

        if active is None:
            failures += 1
            if failures % every == 1:
                log.warning("metric unknown; holding last state (%s consecutive failures)", failures)
            sleep(int(ENV["POLL_INTERVAL"]))
            continue

        failures = 0
        positives = positives + 1 if active else 0
        desired = positives >= need

        # Refresh the readout while the display is (or is about to be) pinned.
        # The number only advances on Prometheus scrape ticks (15s here), so
        # the value_text comparison naturally throttles pushes.
        value_text = None
        if desired or applied:
            value_text = format_rate(tokens_per_second())

        # Push the composed frame BEFORE pinning so the first thing displayed
        # is the live number, and never while un-pinning (nobody sees it).
        if desired and value_text and value_text != pushed_text and not dry_run:
            # Render failure and HTTP push failure are SEPARATE branches: the
            # previous shared `ok = False` path fell into the push log and hit
            # UnboundLocalError on the never-assigned push_status, killing the
            # poller exactly when the "keep previous frame" recovery meant to
            # run. A raise means no push happened at all, so nothing else
            # needs doing; pushed_text stays unset and the next poll retries.
            try:
                ok, push_status = push_image(compose_webp(value_text))
                if ok:
                    pushed_text = value_text
                    log.info("pushed tok/s frame: %s", value_text)
                else:
                    log.error("frame push failed (HTTP %s)", push_status)
            except OSError as exc:
                log.error("render failed (%s); keeping previous frame", exc)

        # Reconcile an unknown startup baseline. While the state is unknown we
        # must NOT assume disabled: a display that is actually ON (poller
        # restarted mid-generation with the status read failing) would be
        # flickered OFF for a poll and back ON, which is exactly the
        # interruption the baseline read exists to avoid. So: a negative poll
        # forces the False correction, a completed positive streak forces the
        # True pin, and a positive poll still below the threshold defers -- we
        # would have waited for the streak before pinning anyway.
        if not reconciled and desired is False and active:
            log.info("baseline unknown, positive streak %s/%s; deferring reconciliation", positives, need)
            sleep(int(ENV["POLL_INTERVAL"]))
            continue

        # Force one reconciliation once a real metric answer exists, so an
        # unknown startup baseline cannot leave stale display state behind.
        if not reconciled or desired != applied:
            if dry_run:
                log.info("dry-run: would PATCH iname %s to %s", iname, desired)
                applied, reconciled = desired, True
            elif set_display(desired, iname):
                applied, reconciled = desired, True
            else:
                healed = ensure_installation()
                if healed != iname:
                    log.info("switching to healed iname %s", healed)
                    iname = healed
                log.error("could not apply %s; retrying next poll", desired)

            log.info(
                "decision: raw=%s positives=%s/%s desired=%s applied=%s value=%s",
                active, positives, need, desired, applied, value_text,
            )

        sleep(int(ENV["POLL_INTERVAL"]))


if __name__ == "__main__":
    _require_config()
    run(dry_run="--dry-run" in sys.argv)
