from fastapi.testclient import TestClient

from pmanalysis.web.app import create_app


def test_dashboard_renders():
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert "Initial TWAP" in response.text
    assert "p-origin-removebg.png" in response.text
    assert "lightweight-charts" in response.text


def test_api_markets():
    client = TestClient(create_app())
    response = client.get("/api/markets")
    assert response.status_code == 200
    assert len(response.json()) == 6


def test_market_redirect():
    client = TestClient(create_app())
    response = client.get("/market/BTC/5m")
    assert response.status_code == 200
    assert "BTC" in response.text


def test_history_page_has_no_clear_button():
    client = TestClient(create_app())
    response = client.get("/history")
    assert response.status_code == 200
    assert "Clear Database" not in response.text
    assert "win-rate-stats" in response.text
    assert "/api/stats" in response.text or "UP win rate" in response.text


def test_status_page_has_feed_lag():
    client = TestClient(create_app())
    response = client.get("/status")
    assert response.status_code == 200
    assert "Feed lag" in response.text
    assert "status.js" in response.text


def test_api_stats_shape():
    client = TestClient(create_app())
    all_stats = client.get("/api/stats")
    assert all_stats.status_code == 200
    body = all_stats.json()
    assert "items" in body
    assert len(body["items"]) == 6
    one = client.get("/api/stats/BTC/5m")
    assert one.status_code == 200
    row = one.json()
    assert row["asset"] == "BTC"
    assert row["interval"] == "5m"
    assert "win_rate_up_pct" in row


def test_api_status_shape():
    client = TestClient(create_app())
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert "feeds" in body
    assert "gaps_open" in body
    assert "gaps_recent" in body
    assert len(body["feeds"]) == 3
    sources = {f["source"] for f in body["feeds"]}
    assert sources == {"binance", "chainlink", "clob"}


def test_brand_assets_served():
    client = TestClient(create_app())
    logo = client.get("/static/img/p-origin-removebg.png")
    badge = client.get("/static/img/polymarket-badge.png")
    favicon = client.get("/favicon.ico")
    assert logo.status_code == 200
    assert badge.status_code == 200
    assert favicon.status_code == 200
    assert logo.headers["content-type"].startswith("image/")


def test_clear_db_endpoint_removed():
    client = TestClient(create_app())
    response = client.post("/api/admin/clear-db", json={"password": "secret"})
    assert response.status_code == 404
