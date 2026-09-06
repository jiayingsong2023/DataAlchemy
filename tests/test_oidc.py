import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import MagicMock

import pytest

from src.harness.test_idp import Handler
from src.utils import oidc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def test_oidc_requires_signed_state_and_maps_server_claims(monkeypatch):
    monkeypatch.setattr(oidc, "OIDC_AUTHORIZE_URL", "https://issuer.example/authorize")
    monkeypatch.setattr(oidc, "OIDC_TOKEN_URL", "https://issuer.example/token")
    monkeypatch.setattr(oidc, "OIDC_USERINFO_URL", "https://issuer.example/userinfo")
    monkeypatch.setattr(oidc, "OIDC_CLIENT_ID", "client")
    monkeypatch.setattr(oidc, "OIDC_REDIRECT_URI", "https://app.example/callback")
    monkeypatch.setattr(oidc, "OIDC_GROUP_ROLE_MAP", json.dumps({"admins": "admin"}))
    authorization_url, state = oidc.begin()
    assert "state=" in authorization_url
    with pytest.raises(PermissionError):
        oidc.finish("code", state + "tampered")

    token = MagicMock()
    token.read.return_value = b'{"access_token":"provider-token"}'
    profile = MagicMock()
    profile.read.return_value = b'{"sub":"alice","tenant_id":"acme","groups":["admins"]}'
    token_context = MagicMock()
    token_context.__enter__.return_value = token
    profile_context = MagicMock()
    profile_context.__enter__.return_value = profile
    monkeypatch.setattr(
        oidc.urllib.request, "urlopen", MagicMock(side_effect=[token_context, profile_context])
    )

    assert oidc.finish("code", state) == {"username": "alice", "tenant_id": "acme", "role": "admin"}


def test_oidc_interoperates_with_pkce_test_provider(monkeypatch):
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    redirect_uri = "http://app.example/api/auth/oidc/callback"
    monkeypatch.setenv("TEST_IDP_CLIENT_ID", "client")
    monkeypatch.setenv("TEST_IDP_REDIRECT_URI", redirect_uri)
    monkeypatch.setenv("TEST_IDP_SUBJECT", "alice")
    monkeypatch.setenv("TEST_IDP_TENANT_ID", "acme")
    monkeypatch.setenv("TEST_IDP_GROUPS", "admins")
    monkeypatch.setattr(oidc, "OIDC_AUTHORIZE_URL", f"{base}/authorize")
    monkeypatch.setattr(oidc, "OIDC_TOKEN_URL", f"{base}/token")
    monkeypatch.setattr(oidc, "OIDC_USERINFO_URL", f"{base}/userinfo")
    monkeypatch.setattr(oidc, "OIDC_CLIENT_ID", "client")
    monkeypatch.setattr(oidc, "OIDC_REDIRECT_URI", redirect_uri)
    monkeypatch.setattr(oidc, "OIDC_GROUP_ROLE_MAP", json.dumps({"admins": "admin"}))
    try:
        authorization_url, _ = oidc.begin()
        opener = urllib.request.build_opener(_NoRedirect)
        with pytest.raises(urllib.error.HTTPError) as redirect:
            opener.open(authorization_url)
        callback = urllib.parse.urlparse(redirect.value.headers["Location"])
        query = urllib.parse.parse_qs(callback.query)
        assert oidc.finish(query["code"][0], query["state"][0]) == {
            "username": "alice",
            "tenant_id": "acme",
            "role": "admin",
        }
    finally:
        server.shutdown()
        thread.join()
        for name in tuple(os.environ):
            if name.startswith("TEST_IDP_"):
                monkeypatch.delenv(name, raising=False)
