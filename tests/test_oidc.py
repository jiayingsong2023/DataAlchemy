import json
from unittest.mock import MagicMock

import pytest

from src.utils import oidc


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
