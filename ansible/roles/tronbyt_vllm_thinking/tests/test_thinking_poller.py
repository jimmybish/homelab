#!/usr/bin/env python3
"""Focused tests for the Tronbyt thinking poller state machine.

Stdlib unittest only: the control node has no pip and no pytest. Run from the
role root with

    python3 tests/test_thinking_poller.py

or from anywhere with the file path. These drive run() directly with
max_polls/sleeper seams and fake API functions -- no sockets, no sleeping.

The four cases the staged review demands: active restart, idle restart,
state-read failure, failed PATCH -- plus the hysteresis boundary and the
Authorization header literal (a scrubbed 'Bearer ' prefix caused 401s once).
"""

import importlib.util
import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "files" / "thinking_poller.py"


try:
    from PIL import Image, features

    PIL_AVAILABLE = features.check("webp")
except ImportError:
    PIL_AVAILABLE = False


def load_poller():
    spec = importlib.util.spec_from_file_location("thinking_poller", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PollerTestCase(unittest.TestCase):
    """Shared fixture: full env isolation, ALL network paths stubbed.

    Every external touchpoint (both queries, both HTTP verbs on Tronbyt,
    the renderer) is replaced here, so no test can open a socket even if it
    forgets to stub individually.
    """

    def setUp(self):
        self.poller = load_poller()
        env_patch = {
            "GRAFANA_URL": "http://grafana.invalid:3000",
            "GRAFANA_BEARER": "g" * 46,
            "TRONBYT_URL": "http://tronbyt.invalid:8010",
            "TRONBYT_INAME": "100",
            "TRONBYT_BEARER": "t" * 64,
            "POLL_INTERVAL": "5",
            "THINK_MIN_SECONDS": "10",
        }
        saved = {key: os.environ.get(key) for key in env_patch}
        os.environ.update(env_patch)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        self.poller.refresh_env()
        self.patches = []
        self.pushes = []
        self.metrics = []
        self.baseline = False

        def fake_ensure(iname=None):
            return "100"

        def fake_state(iname=None):
            return self.baseline

        def fake_metric():
            if not self.metrics:
                return False
            return self.metrics.pop(0)

        def fake_set(active, iname=None):
            self.patches.append(bool(active))
            return True

        self.poller.ensure_installation = fake_ensure
        self.poller.current_display_state = fake_state
        self.poller.thinking_now = fake_metric
        self.poller.set_display = fake_set
        # Readout + renderer stubs: without these, any test that reaches
        # "desired" enters the real HTTP/render path.
        self.poller.tokens_per_second = lambda: 23.8
        self.poller.compose_webp = lambda text: "stub:" + text
        self.poller.push_image = lambda b64: (self.pushes.append(b64) or (True, 200))

    def drive(self, polls=10):
        self.poller.run(sleeper=lambda _s: None, max_polls=polls)
        return self.patches


class TestHysteresisBoundary(PollerTestCase):
    def test_min_seconds_needs_two_positives(self):
        self.assertEqual(self.poller.required_positives(), 2)

    def test_single_positive_does_not_activate(self):
        self.baseline = False
        self.metrics = [True]
        self.drive(1)
        self.assertEqual(self.patches, [])

    def test_two_positives_activate_once(self):
        self.baseline = False
        self.metrics = [True, True, True]
        self.drive(3)
        self.assertEqual(self.patches, [True])

    def test_negative_resets_streak(self):
        self.baseline = False
        self.metrics = [True, False, True, True]
        self.drive(4)
        self.assertEqual(self.patches, [True])


class TestIdleRestart(PollerTestCase):
    """Baseline disabled: activate only after the streak, never before."""

    def test_idle_restart_activates_after_streak(self):
        self.baseline = False
        self.metrics = [True, True, False]
        self.drive(3)
        self.assertEqual(self.patches, [True, False])

    def test_idle_restart_does_not_patch_when_staying_idle(self):
        self.baseline = False
        self.metrics = [False, False, False]
        self.drive(3)
        # Baseline known-disabled is already correct: one write is wasted work.
        self.assertEqual(self.patches, [])


class TestActiveRestart(PollerTestCase):
    """Baseline enabled (restart mid-generation): must NOT hide the animation."""

    def test_active_restart_survives_positive_polls(self):
        self.baseline = True
        self.metrics = [True, True, True]
        self.drive(3)
        self.assertEqual(self.patches, [])

    def test_active_restart_clears_on_first_negative(self):
        self.baseline = True
        self.metrics = [True, False]
        self.drive(2)
        self.assertEqual(self.patches, [False])


class TestUnknownBaseline(PollerTestCase):
    """State-read failure: unknown must reconcile, never hold stale state,
    and must NEVER interrupt an active display with a False flicker."""

    def test_unknown_baseline_forces_write_even_when_idle(self):
        self.baseline = None
        self.metrics = [False, False]
        self.drive(2)
        # A display stuck ON behind an unreadable API must be turned OFF.
        self.assertEqual(self.patches, [False])

    def test_unknown_baseline_sustained_activity_never_flickers_off(self):
        # Regression: the forced reconciliation used to PATCH False on poll 1
        # (positives=1 < need=2 while applied defaulted False), then True on
        # poll 2 -- a one-poll interruption exactly when the status read fails.
        self.baseline = None
        self.metrics = [True, True, True]
        self.drive(3)
        # Positive polls below the streak DEFER; the first write must be True.
        self.assertEqual(self.patches, [True])

    def test_unknown_baseline_then_negative_still_corrects(self):
        self.baseline = None
        self.metrics = [False]
        self.drive(2)
        self.assertEqual(self.patches[:1], [False])

    def test_unknown_baseline_activates_with_metric(self):
        self.baseline = None
        self.metrics = [True, True]
        self.drive(2)
        self.assertEqual(self.patches, [True])

    def test_metric_unknown_holds_state(self):
        self.baseline = False
        self.metrics = [None, None, None, True, True]
        self.drive(5)
        self.assertEqual(self.patches, [True])


class TestFailedPatch(PollerTestCase):
    def test_failed_patch_retries_next_poll(self):
        calls = {"n": 0}

        def flaky(active, iname=None):
            calls["n"] += 1
            self.patches.append(bool(active))
            return calls["n"] > 1  # first attempt fails, second succeeds

        self.poller.set_display = flaky
        self.baseline = False
        self.metrics = [True, True, True]
        self.drive(3)
        # Attempted on the poll it became due, then retried until applied.
        self.assertEqual(self.patches[:2], [True, True])


class TestPromErrorPayloads(unittest.TestCase):
    """HTTP 200 does NOT mean the query worked: Grafana proxies Prometheus
    errors as 200 with status=error. They must read as UNKNOWN, never False."""

    ERROR_BODY = {"status": "error", "errorType": "bad_data", "error": "query failed"}

    def setUp(self):
        self.poller = load_poller()

    def test_error_envelope_is_unknown_not_false(self):
        self.assertIsNone(self.poller._parse_prom_response(self.ERROR_BODY))
        self.assertIsNone(self.poller._first_scalar(self.ERROR_BODY, "prom"))

    def test_missing_status_is_unknown(self):
        self.assertIsNone(self.poller._parse_prom_response({"data": {"result": []}}))

    def test_success_empty_vector_is_false(self):
        body = {"status": "success", "data": {"result": []}}
        self.assertIs(self.poller._parse_prom_response(body), False)

    def test_success_value_still_parses(self):
        body = {"status": "success", "data": {"result": [{"value": [0, "3.5"]}]}}
        self.assertIs(self.poller._parse_prom_response(body), True)
        self.assertEqual(self.poller._first_scalar(body, "prom"), 3.5)

    def test_ds_query_error_frame_is_unknown(self):
        # Grafana /api/ds/query encodes per-query errors inside a 200 body.
        body = {"results": {"A": {"error": "bad_data"}}}
        self.assertIsNone(self.poller._parse_ds_query_response(body))


class TestErrorPayloadHoldBehaviour(PollerTestCase):
    """End-to-end: a proxied error must NOT clear an active display, and must
    fall through to the dsquery route when that one has a real answer."""

    def setUp(self):
        super().setUp()
        self.proxied_errors = 0

        def fake_http(url, headers=None, payload=None, method="GET"):
            # The prom route ALWAYS returns Grafana's proxied Prometheus error
            # with HTTP 200; the dsquery route transport-fails too.
            if "proxy" in url:
                self.proxied_errors += 1
                return 200, TestPromErrorPayloads.ERROR_BODY
            return None, None

        # The shared fixture replaces thinking_now/tokens_per_second with
        # metric stubs; a fresh module gives the REAL ones back, with only
        # http_json faked.
        self.live = load_poller()
        self.live.http_json = fake_http

    def test_error_envelope_makes_metric_unknown(self):
        self.assertIsNone(self.live.thinking_now())
        self.assertEqual(self.proxied_errors, 1)

    def test_readout_unknown_on_error(self):
        self.assertIsNone(self.live.tokens_per_second())
        self.assertGreaterEqual(self.proxied_errors, 1)

    def test_error_never_clears_active_display(self):
        # Regression path: with the parser fixed, thinking_now() yields None
        # (not False) for the error body, and the state machine holds. The
        # None sequence must cover EVERY poll (the fixture's fake_metric
        # answers False once its list is exhausted).
        self.assertIsNone(self.live.thinking_now())
        self.baseline = True
        self.metrics = [None] * 4
        self.drive(4)
        self.assertEqual(self.patches, [])

    def test_error_never_activates_display_either(self):
        self.assertIsNone(self.live.thinking_now())
        self.baseline = False
        self.metrics = [None] * 4
        self.drive(4)
        self.assertEqual(self.patches, [])


class TestRestartCountAssertion(unittest.TestCase):
    """The deploy-time health assertion once read `baseline.container.RestartCount`
    while `baseline` was ALREADY the container dict. The path resolved to
    nothing, defaulted to 0, and any container with a nonzero historical
    restart count would fail every future deploy. These tests render the REAL
    expressions out of tasks/main.yaml so the path stays locked."""

    def setUp(self):
        import yaml

        with open(HERE.parent / "tasks" / "main.yaml") as handle:
            tasks = yaml.safe_load(handle)
        for task in tasks:
            block = task.get("ansible.builtin.assert")
            if block and any("RestartCount" in item for item in block.get("that", [])):
                # `vars:` sits at TASK level (sibling of the module), while
                # that:/fail_msg: live inside the assert block. Keep both.
                self.assert_task = block
                self.task_vars = task.get("vars")
                return
        self.fail("health assert task not found in tasks/main.yaml")

    def _render(self, template, variables):
        from jinja2.nativetypes import NativeEnvironment

        # Ansible evaluates bare `that:` expressions as if wrapped in {{ }};
        # Jinja only returns native objects inside delimiters, so wrap here.
        if "{{" not in template:
            template = "{{ " + template + " }}"
        return NativeEnvironment().from_string(template).render(**variables)

    def _restart_expr(self):
        for item in self.assert_task["that"]:
            if "RestartCount" in item:
                return item
        self.fail("restart-count assertion expression not found")

    def _variables(self, before_count, after_count):
        container = {"State": {"Running": True, "Status": "running"}}
        before = {"exists": True, "container": dict(container, RestartCount=before_count)}
        after = {"exists": True, "container": dict(container, RestartCount=after_count)}
        variables = {
            "tronbyt_vllm_thinking_container_before": before,
            "tronbyt_vllm_thinking_container": after,
            "tronbyt_vllm_thinking_logs": {"stdout": "", "stderr": ""},
        }
        variables["baseline"] = self._render(self.task_vars["baseline"], variables)
        variables["logs"] = self._render(self.task_vars["logs"], variables)
        return variables

    def test_baseline_is_the_container_dict(self):
        variables = self._variables(5, 5)
        self.assertEqual(variables["baseline"]["RestartCount"], 5)

    def test_equal_nonzero_restart_counts_pass(self):
        # The exact case the old bug failed: stable container, restarts=5->5.
        variables = self._variables(5, 5)
        self.assertTrue(self._render(self._restart_expr(), variables))

    def test_restart_during_run_fails(self):
        variables = self._variables(5, 6)
        self.assertFalse(self._render(self._restart_expr(), variables))

    def test_absent_before_still_passes_zero_after(self):
        variables = self._variables(0, 0)
        self.assertTrue(self._render(self._restart_expr(), variables))

    def test_fail_msg_reads_the_same_path(self):
        variables = self._variables(5, 6)
        rendered = self._render(self.assert_task["fail_msg"], variables)
        self.assertIn("restart_count=6", rendered)
        self.assertIn("baseline 5", rendered)


class TestRenderFailure(PollerTestCase):
    """A broken asset must degrade to 'keeping previous frame', never crash.

    Regression: the except-OSError branch set ok=False and fell into the push
    failure log, referencing the never-assigned push_status -- an
    UnboundLocalError that terminated the poller mid-generation.
    """

    def test_compose_oserror_does_not_kill_loop_or_touch_display(self):
        def boom(_text):
            raise OSError("asset unreadable")

        self.poller.compose_webp = boom
        self.baseline = False
        self.metrics = [True, True, True]
        self.drive(3)
        # The state machine ran to completion and pinned normally;
        # no frame was ever pushed (push_image must not be reached).
        self.assertEqual(self.patches, [True])
        self.assertEqual(self.pushes, [])

    def test_loop_retries_after_render_failure(self):
        calls = {"n": 0}

        def flaky(text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient")
            return "stub:" + text

        self.poller.compose_webp = flaky
        self.baseline = False
        self.metrics = [True, True, True]
        self.drive(3)
        self.assertEqual(self.patches, [True])
        # First poll failed, second succeeded -- recovery works.
        self.assertEqual(self.pushes, ["stub:23.8"])

    @unittest.skipUnless(PIL_AVAILABLE, "exercises the real Pillow renderer")
    def test_real_asset_unreadable_end_to_end(self):
        # Fresh module = the REAL compose_webp, pointed at a missing gif, so
        # the OSError comes from Pillow itself, not a stub.
        live = load_poller()
        live.pushes = []
        live.patches = []

        def fake_state(iname=None):
            return False

        def fake_set(active, iname=None):
            live.patches.append(bool(active))
            return True

        live.ensure_installation = lambda iname=None: "100"
        live.current_display_state = fake_state
        live.thinking_now = lambda: True
        live.set_display = fake_set
        live.tokens_per_second = lambda: 23.8
        live.push_image = lambda b64: live.pushes.append(b64) or (True, 200)
        os.environ["ASSET_PATH"] = str(HERE / "no-such-file.gif")
        live.refresh_env()
        try:
            live.run(sleeper=lambda _s: None, max_polls=3)  # must not raise
        finally:
            os.environ.pop("ASSET_PATH", None)
        self.assertEqual(live.patches, [True])  # pin still happened
        self.assertEqual(live.pushes, [])  # nothing pushed from a broken render


class TestFormatterWorstCase(unittest.TestCase):
    """The <=5-char panel guarantee must be absolute for ANY float."""

    def setUp(self):
        self.poller = load_poller()

    def test_k_band_upper_edge(self):
        self.assertEqual(self.poller.format_rate(9999499), "9999k")
        # 9999500 rounds to 10000k (6 chars) -> saturates, never 6 chars.
        self.assertEqual(self.poller.format_rate(9999500), "9999+")
        self.assertEqual(self.poller.format_rate(10000000), "9999+")
        self.assertEqual(self.poller.format_rate(999999999999), "9999+")

    def test_infinity_and_nan_saturate(self):
        # json.loads("1e999") -> inf; int(round(inf)) used to raise
        # OverflowError and kill the loop.
        self.assertEqual(self.poller.format_rate(float("inf")), "9999+")
        self.assertEqual(self.poller.format_rate(float("1e999")), "9999+")
        self.assertIn(self.poller.format_rate(float("nan")), ("0.0", "9999+"))

    def test_absolute_length_bound(self):
        import math
        import random

        random.seed(4)
        samples = [0.0, 99.94, 99.95, 9999.44, 9999.5, 9999499.0, 9999500.0]
        samples += [random.uniform(0, 2e8) for _ in range(200)]
        samples += [random.uniform(1e6, 1e12) for _ in range(200)]
        samples += [math.inf, 1e308]
        for value in samples:
            text = self.poller.format_rate(value)
            self.assertLessEqual(
                len(text), 5, "format_rate({!r}) = {!r} overflows the panel".format(value, text)
            )


class TestRateFormatting(unittest.TestCase):
    """The panel rule Jimmy set: decimal under 100, integer at/above 100."""

    def setUp(self):
        self.poller = load_poller()

    def test_decimal_below_100(self):
        self.assertEqual(self.poller.format_rate(23.84), "23.8")
        self.assertEqual(self.poller.format_rate(0.04), "0.0")
        self.assertEqual(self.poller.format_rate(99.94), "99.9")

    def test_integer_at_or_above_100(self):
        # 99.95 renders "100.0" at .1f (the double is slightly ABOVE 99.95),
        # and the rule says: once the displayed value reaches 100, no decimal.
        self.assertEqual(self.poller.format_rate(99.95), "100")
        self.assertEqual(self.poller.format_rate(99.94), "99.9")
        self.assertEqual(self.poller.format_rate(100.0), "100")
        self.assertEqual(self.poller.format_rate(153.4), "153")
        self.assertEqual(self.poller.format_rate(4820.6), "4821")

    def test_k_notation_above_10000(self):
        self.assertEqual(self.poller.format_rate(12345), "12.3k")

    def test_10k_boundary_uses_rounded_value(self):
        # Regression: the branch used to be chosen from the UNROUNDED value,
        # so 9999.94 printed the 5-char "10000" and overflowed the panel.
        # 9999.5 and above round to 10000: must be k-notation, never "10000".
        # 9999.49 renders "9999.5" at .1f, and rounding THAT lands on 10000
        # (banker's rounding to even), so it also takes the k branch.
        self.assertEqual(self.poller.format_rate(9999.44), "9999")
        self.assertEqual(self.poller.format_rate(9999.5), "10.0k")
        self.assertEqual(self.poller.format_rate(9999.94), "10.0k")
        self.assertEqual(self.poller.format_rate(9999.95), "10.0k")
        self.assertEqual(self.poller.format_rate(9999.96), "10.0k")
        self.assertEqual(self.poller.format_rate(10000.0), "10.0k")
        for value in (9999.4, 9999.44, 9999.5, 9999.9, 9999.96, 10000.0, 12345.6):
            self.assertLessEqual(len(self.poller.format_rate(value)), 5)

    def test_large_rates_stay_within_panel(self):
        self.assertEqual(self.poller.format_rate(99999.94), "100k")
        self.assertEqual(self.poller.format_rate(123456), "123k")
        self.assertEqual(self.poller.format_rate(1000000), "1000k")
        for value in (99994.9, 99999.94, 123456, 999999, 1000000):
            self.assertLessEqual(len(self.poller.format_rate(value)), 5)

    def test_negative_and_none(self):
        self.assertEqual(self.poller.format_rate(-1.0), "0.0")
        self.assertIsNone(self.poller.format_rate(None))


class TestPushThrottle(PollerTestCase):
    """Push only on formatted-value change, and only while pinned."""

    def test_pushes_once_per_value_change(self):
        self.baseline = False
        values = iter([23.8, 23.8, 24.1])

        def fake_tps():
            return next(values)

        self.poller.tokens_per_second = fake_tps
        self.metrics = [True, True, True, True]
        self.drive(4)
        # activation pushes; the repeat 23.8 does not; 24.1 does.
        self.assertEqual(self.pushes, ["stub:23.8", "stub:24.1"])

    def test_no_pushes_while_idle(self):
        self.baseline = False
        self.metrics = [False, False]
        self.drive(2)
        self.assertEqual(self.pushes, [])


class TestAuthHeader(unittest.TestCase):
    """The scheme literal must survive source scrubbing (401 lesson)."""

    def setUp(self):
        self.poller = load_poller()
        # Independent class: set EVERY required value here. It previously
        # inherited GRAFANA_BEARER/TRONBYT_INAME from earlier tests and only
        # passed in a full-suite run.
        required = {
            "GRAFANA_BEARER": "g" * 46,
            "TRONBYT_INAME": "100",
            "TRONBYT_BEARER": "t" * 64,
        }
        saved = {key: os.environ.get(key) for key in required}
        os.environ.update(required)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        self.poller.refresh_env()

    def test_header_is_scheme_plus_token(self):
        header = self.poller._auth("TRONBYT_BEARER")["Authorization"]
        self.assertEqual(len(header), 7 + 64)
        self.assertEqual(sum(map(ord, header[:7])), 625)  # 'Bearer '
        self.assertEqual(header.count("*"), 0)

    def test_both_secrets_present(self):
        self.poller._require_config()  # exits nonzero on missing; must not here


@unittest.skipUnless(PIL_AVAILABLE, "Pillow with WebP required (runs in the container image / build venv)")
class TestRenderer(PollerTestCase):
    """compose_webp() output is what the device actually renders."""

    def setUp(self):
        super().setUp()
        # The shared fixture stubs the renderer; reload the module to get the
        # REAL compose_webp back (each load execs a fresh module object).
        self.poller = load_poller()
        # refresh_env re-reads os.environ, so the override must live there.
        os.environ["ASSET_PATH"] = str(HERE.parent / "files" / "think.gif")
        self.poller.refresh_env()
        self.addCleanup(os.environ.pop, "ASSET_PATH", None)

    def _decoded(self, text="23.8"):
        import base64

        return base64.b64decode(self.poller.compose_webp(text))

    def test_webp_container_and_asset_limit(self):
        raw = self._decoded()
        self.assertEqual(raw[0:4], b"RIFF")
        self.assertEqual(raw[8:12], b"WEBP")
        self.assertEqual(raw[12:16], b"VP8X")
        self.assertLessEqual(len(raw), 131072)

    def test_canvas_frames_and_loop(self):
        import io

        raw = self._decoded()
        img = Image.open(io.BytesIO(raw))
        self.assertEqual(img.size, (64, 32))
        self.assertEqual(img.n_frames, 20)
        self.assertEqual(img.info.get("loop"), 0)  # infinite

    def test_panel_is_black_and_text_is_emoji_yellow(self):
        import io

        img = Image.open(io.BytesIO(self._decoded("88.8")))
        img.seek(0)
        rgb = img.convert("RGB")
        # Panel background corners must be (near) black: lossy WebP quantises
        # pure #000 to values within a few units, so assert darkness, not 0.
        for x, y in ((33, 0), (63, 0), (33, 31), (63, 31)):
            pixel = rgb.getpixel((x, y))
            self.assertTrue(all(channel <= 16 for channel in pixel), (x, y, pixel))
        # Text pixels in the right panel: glyph strokes must be a warm
        # yellow-ish hue. Lossy WebP desaturates thin anti-aliased strokes
        # (#FBC84C quantises toward ~#CCB98E), and this decoded asset IS what
        # the device renders, so assert hue family, not the source hex.
        warm = [
            rgb.getpixel((x, y))
            for x in range(32, 64)
            for y in range(32)
            if rgb.getpixel((x, y))[0] > 150
            and rgb.getpixel((x, y))[1] > 120
            and rgb.getpixel((x, y))[2] < rgb.getpixel((x, y))[0] - 30
        ]
        self.assertGreater(len(warm), 20, "tok/s text missing from panel")

    def test_left_half_is_the_gif(self):
        import io

        img = Image.open(io.BytesIO(self._decoded()))
        img.seek(0)
        rgb = img.convert("RGB")
        # The gif corner is not pure black, proving the animation landed left.
        self.assertNotEqual(rgb.getpixel((0, 0)), (0, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
