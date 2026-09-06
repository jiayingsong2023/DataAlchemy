"""Small OIDC authorization-code provider for non-production interoperability tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_CODES: dict[str, dict[str, str]] = {}
_TOKENS: dict[str, dict[str, Any]] = {}


def _challenge(verifier: str) -> str:
    value = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if parsed.path == "/authorize":
            query = urllib.parse.parse_qs(parsed.query)
            client_id = query.get("client_id", [""])[0]
            redirect_uri = query.get("redirect_uri", [""])[0]
            if (
                client_id != os.environ["TEST_IDP_CLIENT_ID"]
                or redirect_uri != os.environ["TEST_IDP_REDIRECT_URI"]
                or query.get("response_type", [""])[0] != "code"
                or query.get("code_challenge_method", [""])[0] != "S256"
            ):
                self._json(400, {"error": "invalid_request"})
                return
            code = secrets.token_urlsafe(24)
            _CODES[code] = {
                "challenge": query.get("code_challenge", [""])[0],
                "redirect_uri": redirect_uri,
            }
            location = (
                redirect_uri
                + "?"
                + urllib.parse.urlencode({"code": code, "state": query.get("state", [""])[0]})
            )
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        if parsed.path == "/userinfo":
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            profile = _TOKENS.get(token)
            if profile is None:
                self._json(401, {"error": "invalid_token"})
                return
            self._json(200, profile)
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/token":
            self._json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        code = form.get("code", [""])[0]
        pending = _CODES.pop(code, None)
        secret = os.getenv("TEST_IDP_CLIENT_SECRET", "")
        if (
            pending is None
            or form.get("grant_type", [""])[0] != "authorization_code"
            or form.get("client_id", [""])[0] != os.environ["TEST_IDP_CLIENT_ID"]
            or form.get("redirect_uri", [""])[0] != pending["redirect_uri"]
            or _challenge(form.get("code_verifier", [""])[0]) != pending["challenge"]
            or (secret and form.get("client_secret", [""])[0] != secret)
        ):
            self._json(400, {"error": "invalid_grant"})
            return
        token = secrets.token_urlsafe(32)
        _TOKENS[token] = {
            "sub": os.getenv("TEST_IDP_SUBJECT", "pilot-admin"),
            "tenant_id": os.getenv("TEST_IDP_TENANT_ID", "rtd-q5-test"),
            "groups": os.getenv("TEST_IDP_GROUPS", "admins").split(","),
        }
        self._json(200, {"access_token": token, "token_type": "Bearer"})

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    if os.getenv("TEST_IDP_ENABLED") != "true":
        raise RuntimeError("test_idp_requires_explicit_enablement")
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
