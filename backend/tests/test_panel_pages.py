"""
Tests for what the panel actually serves the browser.

A global `before_request` hook used to stamp `Content-Type: application/json`
on every response, including `/` and `/login`. With
`X-Content-Type-Options: nosniff` a browser then refuses to render those pages
and shows the markup as source text — the panel "just shows code". These tests
pin both halves of the contract: the HTML pages are `text/html`, and the API
(including error bodies) stays `application/json`.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

os.environ.setdefault("ADMIN_PASSWORD", "test-password-123")


def _call(path, method="GET", cookies=None, body=None, content_type=None):
    """Call the Bottle app via WSGI; returns (status, headers, body_text)."""
    from backend.app import app

    payload = body.encode() if isinstance(body, str) else (body or b"")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(payload),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": str(len(payload)),
    }
    if content_type:
        environ["CONTENT_TYPE"] = content_type
    if cookies:
        environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    captured = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = {k.lower(): v for k, v in response_headers}

    chunks = app(environ, start_response)
    body_out = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
    code = int(captured.get("status", "500").split()[0])
    return code, captured.get("headers", {}), body_out.decode("utf-8", "replace")


FORM = "application/x-www-form-urlencoded"
FORM_PASSWORD = "test-password-123"


def _strip_js_comments(source: str) -> str:
    """Remove /* ... */ and whole-line // comments from JavaScript.

    `//` is only treated as a comment when it starts a line or follows
    whitespace, so a URL such as `https://…` inside a string survives.
    """
    import re
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", without_blocks)


def _configure_admin():
    """main() sets the admin hash at startup; tests must do it themselves."""
    from backend.auth import hash_secret
    from backend.config import config as cfg
    cfg.ADMIN_PASSWORD_HASH = hash_secret(FORM_PASSWORD)


class HtmlPagesAreHtmlTests(unittest.TestCase):
    """A browser must render these pages, not print them."""

    def test_dashboard_is_served_as_html(self):
        code, headers, body = _call("/")
        self.assertEqual(code, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"),
                        headers["content-type"])
        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn("RaheeCraft", body)

    def test_login_page_is_served_as_html_with_the_form(self):
        code, headers, body = _call("/login")
        self.assertEqual(code, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"),
                        headers["content-type"])
        self.assertIn('id="login-form"', body)
        self.assertIn('id="password"', body)

    def test_static_assets_keep_their_own_types(self):
        code, headers, _ = _call("/static/css/main.css")
        self.assertEqual(code, 200)
        self.assertTrue(headers["content-type"].startswith("text/css"),
                        headers["content-type"])

    def test_security_headers_are_still_added(self):
        _, headers, _ = _call("/")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")


class LoginFlowTests(unittest.TestCase):
    """The form path a browser actually takes.

    Before this was fixed the form had no method and no submit handler, so it
    natively GET-ed to itself: the panel log showed
    `GET /login?username=admin&password=...` and nobody was logged in.
    """

    def setUp(self):
        _configure_admin()

    def test_login_form_never_submits_as_get(self):
        _, _, body = _call("/login")
        self.assertIn('method="post"', body)
        self.assertIn('action="/login"', body)
        # and the page wires the form up instead of relying on a native submit
        self.assertIn("addEventListener('submit'", body)
        self.assertIn("Auth.login", body)

    def test_form_login_redirects_with_a_session_cookie(self):
        code, headers, body = _call(
            "/login", "POST", body=f"username=admin&password={FORM_PASSWORD}",
            content_type=FORM)
        self.assertEqual(code, 303)
        self.assertEqual(headers["location"], "/")
        cookie = headers.get("set-cookie", "")
        self.assertIn("raheecraft_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn(FORM_PASSWORD, body)

        # The cookie from that redirect must actually authenticate.
        session = cookie.split("raheecraft_session=", 1)[1].split(";", 1)[0]
        code, headers, body = _call("/api/status",
                                    cookies={"raheecraft_session": session})
        self.assertEqual(code, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        json.loads(body)

    def test_wrong_password_redirects_without_leaking_anything(self):
        code, headers, body = _call(
            "/login", "POST", body="username=admin&password=wrong-secret-value",
            content_type=FORM)
        self.assertEqual(code, 303)
        self.assertEqual(headers["location"], "/login?error=1")
        self.assertNotIn("set-cookie", headers)
        self.assertNotIn("wrong-secret-value", headers["location"])
        self.assertNotIn("wrong-secret-value", body)


class ClientAuthContractTests(unittest.TestCase):
    """The browser cannot see the HttpOnly session cookie.

    Regression: `dashboard.js` asked `document.cookie` whether the user was
    signed in. That is always false for an HttpOnly cookie, so a valid login was
    bounced straight back to /login — "logged in, then immediately kicked".
    """

    def setUp(self):
        _configure_admin()

    def _session_cookie(self) -> str:
        code, headers, _ = _call(
            "/login", "POST", body=f"username=admin&password={FORM_PASSWORD}",
            content_type=FORM)
        self.assertEqual(code, 303)
        return headers["set-cookie"].split("raheecraft_session=", 1)[1].split(";", 1)[0]

    def test_dashboard_page_states_whether_the_session_is_valid(self):
        _, _, body = _call("/")
        self.assertIn('data-logged-in="0"', body)
        _, _, body = _call("/", cookies={"raheecraft_session": self._session_cookie()})
        self.assertIn('data-logged-in="1"', body)

    def test_client_scripts_never_read_the_session_cookie(self):
        """Code check: no client script may consult the cookie jar for auth.

        Comments are stripped first — several of them name the mistake in order
        to explain why the server must answer the question instead.
        """
        for name in ("dashboard.js", "auth.js"):
            code = _strip_js_comments(
                (REPO / "backend" / "static" / "js" / name).read_text())
            self.assertNotIn("document.cookie", code, name)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_check_login_does_not_bounce_a_valid_session(self):
        """Run the real dashboard.js against a stub DOM with an empty cookie jar.

        A source assertion cannot prove this behaviour, so the harness executes
        `checkLogin()` and fails when a logged-in page with an invisible cookie
        redirects to /login.
        """
        harness = REPO / "backend" / "tests" / "js" / "check_login.js"
        proc = subprocess.run([shutil.which("node"), str(harness)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        cases = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(cases["logged_in_flag_1"]["redirected"])
        self.assertTrue(cases["anonymous_flag_0"]["redirected"])


class ApiStaysJsonTests(unittest.TestCase):
    """The hook change must not hand the dashboard text where it expects JSON."""

    def test_health_is_json(self):
        code, headers, body = _call("/health")
        self.assertEqual(code, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"),
                        headers["content-type"])
        self.assertEqual(json.loads(body)["status"], "healthy")

    def test_unauthenticated_api_error_is_json(self):
        code, headers, body = _call("/api/status")
        self.assertEqual(code, 401)
        self.assertTrue(headers["content-type"].startswith("application/json"),
                        headers["content-type"])
        self.assertIn("error", json.loads(body))

    def test_unknown_api_path_is_a_json_404(self):
        code, headers, body = _call("/api/definitely-not-a-route")
        self.assertEqual(code, 404)
        self.assertTrue(headers["content-type"].startswith("application/json"),
                        headers["content-type"])
        self.assertTrue(json.loads(body))


if __name__ == "__main__":
    unittest.main()
