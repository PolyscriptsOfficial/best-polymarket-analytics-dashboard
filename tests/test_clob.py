from pmanalysis.feeds.clob import ClobFeed, parse_clob_ws


def test_parse_best_bid_ask_event():
    msg = {
        "event_type": "best_bid_ask",
        "asset_id": "123",
        "best_bid": "0.48",
        "best_ask": "0.52",
    }
    updates = parse_clob_ws(msg)
    assert len(updates) == 1
    assert updates[0]["token_id"] == "123"
    assert updates[0]["best_bid"] == 48.0
    assert updates[0]["best_ask"] == 52.0


def test_apply_quote_merges_partial_updates():
    feed = ClobFeed()
    feed.apply_quote("tok", 52.0, 48.0)
    feed.apply_quote("tok", 53.0, 0.0)
    ask, bid = feed.get_quote("tok")
    assert ask == 53.0
    assert bid == 48.0


def test_book_depth_and_trades_from_ws_events():
    feed = ClobFeed()
    feed.apply_book_events({
        "event_type": "book",
        "asset_id": "tok",
        "asks": [{"price": "0.52", "size": "100"}, {"price": "0.53", "size": "50"}, {"price": "0.60", "size": "999"}],
        "bids": [{"price": "0.48", "size": "70"}],
    })
    feed.apply_quote("tok", 52.0, 48.0)
    stats = feed.book_stats("tok")
    assert stats["ask_size"] == 100.0
    assert stats["ask_depth"] == 150.0  # within 2c of the best ask
    assert stats["bid_size"] == 70.0

    feed.apply_book_events({
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "tok", "price": "0.52", "size": "0", "side": "SELL"},
            {"asset_id": "tok", "price": "0.54", "size": "25", "side": "SELL"},
        ],
    })
    feed.apply_quote("tok", 53.0, 48.0)
    stats = feed.book_stats("tok")
    assert stats["ask_size"] == 50.0
    assert stats["ask_depth"] == 75.0

    feed.apply_book_events({"event_type": "last_trade_price", "asset_id": "tok", "price": "0.53", "size": "20"})
    assert feed.last_trade("tok") == 53.0
    assert abs(feed.traded_notional("tok") - 10.6) < 1e-9

    feed.prune_quotes([])
    assert feed.book_stats("tok")["ask_size"] is None
