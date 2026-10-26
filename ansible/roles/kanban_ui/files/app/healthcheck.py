#!/usr/bin/env python3
"""Container healthcheck for kanban-ui.

Liveness is defined by the token gate actually enforcing: an anonymous
request to a /api/ path MUST come back 401. A 200 means the gate is off
(unhealthy), a refused connection or timeout means the app is down.

Exit 0 = healthy, 1 = unhealthy.
"""
import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("KANBAN_UI_HEALTH_PORT", "9470")
url = f"http://127.0.0.1:{port}/api/health"

try:
    urllib.request.urlopen(url, timeout=5)  # noqa: S310
except urllib.error.HTTPError as exc:
    sys.exit(0 if exc.code == 401 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
sys.exit(1)
