from pmanalysis.feeds.slugs import boundary_ts, build_market_slug, start_ts_from_slug


def test_boundary_ts_5m():
    assert boundary_ts(1780338799, "5m") == 1780338600


def test_build_market_slug_5m():
    assert build_market_slug("BTC", "5m", 1780338799) == "btc-updown-5m-1780338600"


def test_build_market_slug_15m():
    assert build_market_slug("ETH", "15m", 1780338799) == "eth-updown-15m-1780338600"


def test_start_ts_from_slug():
    assert start_ts_from_slug("btc-updown-5m-1780338600") == 1780338600
    assert start_ts_from_slug("not-a-slug") is None
