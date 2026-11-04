from app.main import app
from tests.helpers import route_paths


def test_assessment_routes_exist():
    routes = route_paths(app)
    expected = {
        "/api/v1/courses/{course_id}/questions",
        "/api/v1/courses/{course_id}/tests",
        "/api/v1/questions/{question_id}",
        "/api/v1/tests/{test_id}",
        "/api/v1/tests/{test_id}/attempts",
        "/api/v1/attempts/{attempt_id}",
        "/api/v1/attempts/{attempt_id}/submit",
        "/api/v1/results/{result_id}",
        "/api/v1/tests/{test_id}/results",
    }
    assert expected.issubset(routes)


def test_assessment_openapi():
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/courses/{course_id}/questions",
        "/api/v1/courses/{course_id}/tests",
        "/api/v1/tests/{test_id}/attempts",
        "/api/v1/attempts/{attempt_id}/submit",
        "/api/v1/results/{result_id}",
    ):
        assert path in paths
