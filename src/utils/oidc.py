"""Minimal OIDC authorization-code client with server-side claim mapping."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from config import (
    AUTH_ALGORITHM,
    AUTH_SECRET_KEY,
    OIDC_AUTHORIZE_URL,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_GROUP_ROLE_MAP,
    OIDC_REDIRECT_URI,
    OIDC_TENANT_CLAIM,
    OIDC_TOKEN_URL,
    OIDC_USERINFO_URL,
)


def begin() -> tuple[str, str]:
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    challenge = challenge.rstrip(b"=").decode()
    state = jwt.encode(
        {
            "purpose": "oidc",
            "nonce": nonce,
            "verifier": verifier,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        },
        AUTH_SECRET_KEY,
        algorithm=AUTH_ALGORITHM,
    )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": OIDC_REDIRECT_URI,
            "scope": "openid profile email groups",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{OIDC_AUTHORIZE_URL}?{query}", state


def finish(code: str, state: str) -> dict[str, str]:
    try:
        claims = jwt.decode(state, AUTH_SECRET_KEY, algorithms=[AUTH_ALGORITHM])
    except JWTError as error:
        raise PermissionError("Invalid OIDC state") from error
    if claims.get("purpose") != "oidc":
        raise PermissionError("Invalid OIDC state")
    payload = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OIDC_REDIRECT_URI,
            "client_id": OIDC_CLIENT_ID,
            "code_verifier": claims["verifier"],
            **({"client_secret": OIDC_CLIENT_SECRET} if OIDC_CLIENT_SECRET else {}),
        }
    ).encode()
    request = urllib.request.Request(OIDC_TOKEN_URL, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        token = json.loads(response.read())
    access_token = token.get("access_token")
    if not access_token:
        raise PermissionError("OIDC response lacks access token")
    request = urllib.request.Request(
        OIDC_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        profile: dict[str, Any] = json.loads(response.read())
    username = profile.get("sub")
    tenant_id = profile.get(OIDC_TENANT_CLAIM)
    if not isinstance(username, str) or not isinstance(tenant_id, str):
        raise PermissionError("OIDC profile lacks subject or tenant claim")
    mapping = json.loads(OIDC_GROUP_ROLE_MAP)
    groups = set(profile.get("groups", []))
    role = next((mapping[group] for group in groups if group in mapping), "user")
    if role not in {"user", "admin"}:
        raise PermissionError("OIDC role mapping is invalid")
    return {"username": username, "tenant_id": tenant_id, "role": role}
