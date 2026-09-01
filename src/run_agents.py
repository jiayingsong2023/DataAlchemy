"""Thin HTTP client for the governed DataAlchemy API."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _post(base_url: str, path: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"API request failed ({error.code}): {detail}") from error


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Governed DataAlchemy API client")
    parser.add_argument(
        "--base-url",
        default=os.getenv("DATAALCHEMY_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--token", default=os.getenv("DATAALCHEMY_TOKEN"))
    commands = parser.add_subparsers(dest="command", required=True)

    chat = commands.add_parser("chat", help="Run a governed chat request")
    chat.add_argument("--query", required=True)
    chat.add_argument("--session-id")

    task = commands.add_parser("task", help="Submit a complete task contract")
    task.add_argument("--spec", type=Path, required=True)

    args = parser.parse_args(argv)
    if not args.token:
        parser.error("--token or DATAALCHEMY_TOKEN is required")
    if args.command == "chat":
        payload = {"query": args.query}
        if args.session_id:
            payload["session_id"] = args.session_id
        result = _post(args.base_url, "/api/chat", args.token, payload)
    else:
        result = _post(
            args.base_url,
            "/api/tasks",
            args.token,
            json.loads(args.spec.read_text(encoding="utf-8")),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
