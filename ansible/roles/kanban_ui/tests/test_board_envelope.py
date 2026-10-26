#!/usr/bin/env python3
"""PR#13 review finding 4373043552: the authenticated board check in
verify_session.sh must reject any HTTP 200 that is not the real board
API response. A SWAG/HTML fallback page or proxy-generated success page
must NOT be reported as "board read verified".

Locks the two halves of the step-6 gate by EXECUTING them rather than
grepping:

  1. The content-type pipeline (grep/cut/tr over the curl -D header
     dump): application/json (with or without charset) passes; text/html,
     missing, or any other type fails.
  2. The BOARD_ENVELOPE_PY heredoc, extracted verbatim from
     verify_session.sh the same way test_dispatch_nonmutation_gate.py
     extracts its heredocs: the pinned plugin's get_board envelope
     (non-empty `columns` array of {name: str, tasks: list} plus
     tenants/assignees/latest_event_id) prints OK; an HTML fallback body,
     an empty columns list, a column missing `tasks`, missing top-level
     keys, and non-JSON garbage all print INVALID.

No secrets or live endpoints here: everything is fixtures under tmp.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFIER = HERE.parent / "files" / "verify_session.sh"
SCRIPT = VERIFIER.read_text()


def _extract_heredoc(text, tag):
    m = re.search(r"<<'%s'[^\n]*\n(.*?\n)%s\n" % (tag, tag), text, re.S)
    assert m, f"heredoc {tag} not found in verify_session.sh"
    return m.group(1)


ENVELOPE_SRC = _extract_heredoc(SCRIPT, "BOARD_ENVELOPE_PY")

# The shipped content-type pipeline, extracted verbatim (the CTYPE=
# assignment through its closing line). Executed in bash rather than
# paraphrased so quoting/word-splitting drift is caught.
_lines = SCRIPT.splitlines(keepends=True)
_start = next((i for i, l in enumerate(_lines)
               if l.startswith("CTYPE=$(grep -i '^content-type:'")), None)
assert _start is not None, "CTYPE content-type pipeline not found in verify_session.sh"
_end = next(i for i in range(_start, len(_lines))
            if _lines[i].rstrip().endswith("[:lower:]')"))
CTYPE_SRC = "".join(_lines[_start:_end + 1])


def run_envelope(payload_text):
    """Write the fixture body to a tmp file and run the extracted
    predicate exactly the way verify_session.sh invokes it:
    python3 - <path>, stdout is the single verdict token."""
    with tempfile.TemporaryDirectory() as td:
        body = Path(td) / "board.json"
        body.write_text(payload_text)
        out = subprocess.run(
            ["python3", "-", str(body)],
            input=ENVELOPE_SRC, capture_output=True, text=True,
        )
        return out.stdout.strip()


def run_ctype(header_dump):
    """Feed a curl -D style header dump through the extracted pipeline.
    The pipeline reads $WORK/board.hdrs verbatim, so WORK is exported to
    the tmp dir holding the fixture."""
    with tempfile.TemporaryDirectory() as td:
        hdrs = Path(td) / "board.hdrs"
        hdrs.write_bytes(header_dump)
        script = 'WORK="%s"\n' % td + CTYPE_SRC + 'printf "%s" "$CTYPE"\n'
        out = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout


GOOD = json.dumps({
    "columns": [
        {"name": "triage", "tasks": []},
        {"name": "done", "tasks": [{"id": "t_1", "title": "x"}]},
    ],
    "tenants": [], "assignees": ["virtuajimmy"],
    "latest_event_id": 42, "now": 1,
})


class TestBoardEnvelopePredicate(unittest.TestCase):
    def test_pinned_envelope_passes(self):
        self.assertEqual(run_envelope(GOOD), "OK")

    def test_html_fallback_page_rejected(self):
        # The exact regression: a 200 body that is an HTML fallback page.
        self.assertEqual(
            run_envelope("<!DOCTYPE html><html><body>SWAG</body></html>"),
            "INVALID",
        )

    def test_empty_columns_rejected(self):
        # An empty columns list proves nothing about the board API.
        self.assertEqual(run_envelope(json.dumps(
            {"columns": [], "tenants": [], "assignees": [],
             "latest_event_id": 0})), "INVALID")

    def test_column_without_tasks_list_rejected(self):
        self.assertEqual(run_envelope(json.dumps(
            {"columns": [{"name": "todo"}], "tenants": [], "assignees": [],
             "latest_event_id": 0})), "INVALID")

    def test_columns_as_object_rejected(self):
        self.assertEqual(run_envelope(json.dumps(
            {"columns": {"todo": []}, "tenants": [], "assignees": [],
             "latest_event_id": 0})), "INVALID")

    def test_missing_sibling_keys_rejected(self):
        self.assertEqual(run_envelope(json.dumps(
            {"columns": [{"name": "todo", "tasks": []}], "foo": 1})),
            "INVALID")

    def test_non_dict_json_rejected(self):
        self.assertEqual(run_envelope("[1,2,3]"), "INVALID")

    def test_empty_and_binary_garbage_rejected(self):
        self.assertEqual(run_envelope(""), "INVALID")
        self.assertEqual(run_envelope("\x00\x01\xff not json"), "INVALID")

    def test_predicates_are_static_text(self):
        # The predicate source must not smuggle shell expansion: it is
        # delivered via a quoted heredoc in the script.
        self.assertIn("<<'BOARD_ENVELOPE_PY'", SCRIPT)
        self.assertNotIn("$", ENVELOPE_SRC.split("import json, sys")[1])


class TestContentTypePipeline(unittest.TestCase):
    def test_application_json_variants_pass(self):
        for hdr in (b"HTTP/2 200 \r\ncontent-type: application/json\r\n\r\n",
                    b"HTTP/2 200 \r\ncontent-type: application/json; charset=utf-8\r\n\r\n",
                    b"HTTP/2 200 \r\nContent-Type: APPLICATION/JSON\r\n\r\n"):
            self.assertEqual(run_ctype(hdr), "application/json")

    def test_html_and_missing_types_fail(self):
        self.assertEqual(run_ctype(
            b"HTTP/2 200 \r\nContent-Type: text/html\r\n\r\n"), "text/html")
        self.assertEqual(run_ctype(
            b"HTTP/2 200 \r\ncontent-type: text/html; charset=utf-8\r\n\r\n"),
            "text/html")
        # Missing header -> empty CTYPE (the script then fails the check).
        self.assertEqual(run_ctype(b"HTTP/2 200 \r\n\r\n"), "")


if __name__ == "__main__":
    unittest.main()
