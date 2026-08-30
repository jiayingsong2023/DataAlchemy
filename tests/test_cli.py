import json

from src import run_agents


def test_cli_routes_chat_and_strict_task_to_api(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(
        run_agents,
        "_post",
        lambda base_url, path, token, payload: calls.append((base_url, path, token, payload))
        or {"ok": True},
    )

    run_agents.main(["--base-url", "http://api", "--token", "secret", "chat", "--query", "hi"])
    spec = tmp_path / "task.json"
    spec.write_text(json.dumps({"goal": "governed", "execution_mode": "strict"}))
    run_agents.main(["--base-url", "http://api", "--token", "secret", "task", "--spec", str(spec)])

    assert calls == [
        ("http://api", "/api/chat", "secret", {"query": "hi"}),
        (
            "http://api",
            "/api/tasks",
            "secret",
            {"goal": "governed", "execution_mode": "strict"},
        ),
    ]
    assert capsys.readouterr().out.count('"ok": true') == 2
