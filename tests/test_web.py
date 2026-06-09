from fastapi.testclient import TestClient

from pmanalysis.web.app import create_app


def test_dashboard_renders():
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert "Oracle Price" in response.text
    assert "lightweight-charts" in response.text


def test_api_markets():
    client = TestClient(create_app())
    response = client.get("/api/markets")
    assert response.status_code == 200
    assert len(response.json()) == 9


def test_market_redirect():
    client = TestClient(create_app())
    response = client.get("/market/BTC/5m")
    assert response.status_code == 200
    assert "BTC" in response.text
