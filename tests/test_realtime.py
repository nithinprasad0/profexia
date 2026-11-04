from app.main import app
from tests.helpers import route_paths


def test_realtime_routes_exist():
    paths = route_paths(app)
    expected = {
        "/api/v1/chat/{room_id}/messages",
        "/api/v1/notifications",
        "/api/v1/notifications/{notification_id}/read",
        "/api/v1/notifications/read-all",
        "/api/v1/learning-sessions",
        "/api/v1/learning-sessions/{session_id}",
        "/api/v1/ws/chat/{room_id}",
        "/api/v1/ws/notifications",
        "/api/v1/ws/learning-sessions/{session_id}",
    }
    assert expected.issubset(paths)


def test_realtime_openapi():
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/chat/{room_id}/messages",
        "/api/v1/notifications",
        "/api/v1/learning-sessions",
    ):
        assert path in paths
