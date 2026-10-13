"""Real Chromium integration test for end-to-end browser verification evidence.

This suite is intentionally separate from the offline-safe unit tests because it
requires a Playwright Chromium binary.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.browser_automation import run_browser_automation


pytestmark = pytest.mark.browser_integration


class _VerificationHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/missing.json":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return

        html = b"""<!doctype html>
<html>
<head><title>Verification fixture</title></head>
<body>
  <main>
    <h1>Browser verification ready</h1>
    <label for="query">Query</label>
    <input id="query" name="query">
    <button id="submit" type="button"
      onclick="document.querySelector('#result').textContent = 'Submitted: ' + document.querySelector('#query').value">
      Submit
    </button>
    <p id="result">Waiting</p>
  </main>
  <script>
    fetch('/missing.json').catch(() => {});
  </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, _format, *_args):
        pass


@pytest.mark.asyncio
async def test_real_chromium_captures_browser_actions_network_and_screenshot():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VerificationHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        report = await run_browser_automation(
            url,
            wait_seconds=0,
            required_text=["Browser verification ready", "Submitted: code-conductor"],
            visible_selectors=["main", "#result"],
            hidden_selectors=["#missing-alert"],
            steps=[
                {"action": "fill", "selector": "#query", "value": "code-conductor"},
                {"action": "click", "selector": "#submit"},
            ],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert report["success"] is True, report
    assert report["verification_engine"] in {
        "playwright-cdp",
        "playwright-managed-chromium",
    }
    assert report["main_document_status"] == 200
    assert report["checks"]["browser_actions_passed"] is True
    assert [step["passed"] for step in report["action_results"]] == [True, True]
    assert report["acceptance_assertions"]["passed"] is True
    assert any(item["status"] == 404 and item["url"].endswith("/missing.json")
               for item in report["http_errors"])
    assert report["screenshot"].startswith("data:image/png;base64,")
