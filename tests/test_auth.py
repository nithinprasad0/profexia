from app.main import app
from tests.helpers import route_paths


EXPECTED = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/users/me",
}


def test_auth_routes_exist():
    routes = route_paths(app)
    assert EXPECTED.issubset(routes)


def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "Profexia API"


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert "status" in response.json()


def test_auth_openapi():
    paths = app.openapi()["paths"]
    assert "/api/v1/auth/register" in paths
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/auth/refresh" in paths
    assert "/api/v1/auth/logout" in paths
    assert "/api/v1/users/me" in paths
