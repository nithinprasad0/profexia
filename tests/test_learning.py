from app.main import app
from tests.helpers import route_paths

def test_learning_routes_exist():
    routes = route_paths(app)
    expected = {
        "/api/v1/skills", "/api/v1/teachers", "/api/v1/courses",
        "/api/v1/courses/{course_id}", "/api/v1/courses/{course_id}/enroll",
        "/api/v1/enrollments/me", "/api/v1/enrollments/{enrollment_id}/progress",
    }
    assert expected.issubset(routes)

def test_learning_openapi():
    paths = app.openapi()["paths"]
    assert "/api/v1/courses" in paths
    assert "/api/v1/courses/{course_id}/enroll" in paths
    assert "/api/v1/enrollments/{enrollment_id}/progress" in paths
