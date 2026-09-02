from fastapi.testclient import TestClient

from zira_dashboard.app import app


def test_static_assets_require_revalidation_instead_of_staying_immutable():
    response = TestClient(app).get("/static/people-performance.css")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, no-cache"
    assert "immutable" not in response.headers["cache-control"]
    assert response.headers.get("etag") or response.headers.get("last-modified")
