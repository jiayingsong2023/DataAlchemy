from webui.app import app


def test_webui_route_manifest_survives_router_split():
    routes = [
        (method, route.path)
        for route in app.routes
        for method in sorted(getattr(route, "methods", None) or {"WEBSOCKET"})
        if route.path.startswith("/api") or route.path in {"/metrics", "/ws/chat"}
    ]

    assert len(routes) == 75
    assert len(routes) == len(set(routes))
    assert {
        ("WEBSOCKET", "/ws/chat"),
        ("POST", "/api/chat"),
        ("POST", "/api/tasks"),
        ("POST", "/api/pilot-runs/document"),
        ("POST", "/api/h5/releases/{release_id}/advance"),
        ("POST", "/api/feedback"),
        ("POST", "/api/memories"),
    } <= set(routes)
