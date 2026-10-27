"""One OHLC candle per Polymarket up/down window.

5m markets use a 5-minute candle; 15m markets use a 15-minute candle.
Open is the window's initial TWAP / opening oracle (the price to beat);
close is the latest (or final) oracle. Body color is therefore UP vs DOWN.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from pmanalysis.config import INTERVAL_SECONDS
from pmanalysis.db.models import Candle, Window


@dataclass(slots=True)
class Ohlc:
    open: float
    high: float
    low: float
    close: float


def apply_price(bar: Ohlc | None, price: float, open_price: float | None = None) -> Ohlc:
    """Fold a new print into a candle. `open_price` pins the official window open."""
    if bar is None:
        opened = open_price if open_price is not None else price
        return Ohlc(
            open=opened,
            high=max(opened, price),
            low=min(opened, price),
            close=price,
        )
    opened = open_price if open_price is not None else bar.open
    return Ohlc(
        open=opened,
        high=max(bar.high, opened, price),
        low=min(bar.low, opened, price),
        close=price,
    )


def direction(open_px: float, close_px: float, outcome: str | None = None) -> str:
    if outcome in ("UP", "DOWN"):
        return outcome
    return "UP" if close_px >= open_px else "DOWN"


def _open_from_window(window: Window) -> float | None:
    return window.opening_oracle_price if window.opening_oracle_price is not None else window.strike_price


def _close_from_window(window: Window) -> float | None:
    return window.final_oracle_price if window.final_oracle_price is not None else _open_from_window(window)


class CandleBook:
    """In-memory live candles, one current bar per asset×interval."""

    def __init__(self) -> None:
        self._bars: dict[tuple[str, str], tuple[int, Ohlc]] = {}
        self._dirty: set[tuple[str, str]] = set()

    def restore(self, asset: str, interval: str, ts: int, bar: Ohlc) -> None:
        self._bars[(asset, interval)] = (ts, bar)

    def update(
        self,
        asset: str,
        interval: str,
        start_ts: int,
        price: float,
        open_price: float | None = None,
    ) -> Ohlc:
        key = (asset, interval)
        prev = self._bars.get(key)
        if prev is None or prev[0] != start_ts:
            bar = apply_price(None, price, open_price)
        else:
            bar = apply_price(prev[1], price, open_price)
        self._bars[key] = (start_ts, bar)
        self._dirty.add(key)
        return bar

    def snapshot(self, asset: str, interval: str) -> dict | None:
        item = self._bars.get((asset, interval))
        if not item:
            return None
        ts, bar = item
        return {
            "time": ts,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "live": True,
        }

    def take_dirty(self) -> list[tuple[str, str, int, Ohlc]]:
        out: list[tuple[str, str, int, Ohlc]] = []
        for key in self._dirty:
            held = self._bars.get(key)
            if held:
                out.append((key[0], key[1], held[0], held[1]))
        self._dirty.clear()
        return out


def hydrate_book(session: Session, book: CandleBook, windows: list[Window]) -> None:
    for window in windows:
        row = (
            session.query(Candle)
            .filter_by(asset=window.asset, interval=window.interval, ts=window.start_ts)
            .first()
        )
        if row:
            book.restore(
                window.asset,
                window.interval,
                row.ts,
                Ohlc(open=row.open, high=row.high, low=row.low, close=row.close),
            )


def persist_candles(session: Session, rows: list[tuple[str, str, int, Ohlc]]) -> None:
    if not rows:
        return
    for asset, interval, ts, bar in rows:
        existing = (
            session.query(Candle).filter_by(asset=asset, interval=interval, ts=ts).first()
        )
        if existing:
            existing.open = bar.open
            existing.close = bar.close
            existing.high = max(existing.high, bar.high, bar.open, bar.close)
            existing.low = min(existing.low, bar.low, bar.open, bar.close)
        else:
            session.add(
                Candle(
                    asset=asset,
                    interval=interval,
                    ts=ts,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                )
            )
    session.commit()


def seal_window_candle(session: Session, window: Window) -> None:
    """Pin close to the final oracle. Keep the open already recorded for the window
    (initial TWAP). Only fall back to oracle/strike when no candle exists yet.
    """
    close_px = window.final_oracle_price
    if close_px is None or not window.start_ts:
        return
    existing = (
        session.query(Candle)
        .filter_by(asset=window.asset, interval=window.interval, ts=window.start_ts)
        .first()
    )
    if existing:
        existing.close = close_px
        existing.high = max(existing.high, close_px, existing.open)
        existing.low = min(existing.low, close_px, existing.open)
        return
    open_px = _open_from_window(window)
    if open_px is None:
        return
    bar = apply_price(None, close_px, open_px)
    session.add(
        Candle(
            asset=window.asset,
            interval=window.interval,
            ts=window.start_ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
    )


def _merge_live(items: list[dict], live: dict | None) -> list[dict]:
    if not live:
        return items
    for i, row in enumerate(items):
        if row["time"] != live["time"]:
            continue
        items[i] = {
            **row,
            "open": live["open"],
            "close": live["close"],
            "high": max(row["high"], live["high"]),
            "low": min(row["low"], live["low"]),
            "live": True,
        }
        return items
    items.append({**live, "slug": "", "outcome": None, "live": True})
    return items


def list_window_candles(
    session: Session,
    asset: str,
    interval: str,
    *,
    limit: int = 180,
    live: dict | None = None,
) -> dict:
    asset = asset.upper()
    limit = max(1, min(int(limit), 500))
    windows = (
        session.query(Window)
        .filter(
            Window.asset == asset,
            Window.interval == interval,
            Window.start_ts > 0,
        )
        .order_by(Window.start_ts.desc())
        .limit(limit)
        .all()
    )
    stored = {}
    if windows:
        ts_list = [w.start_ts for w in windows]
        stored = {
            row.ts: row
            for row in session.query(Candle)
            .filter(
                Candle.asset == asset,
                Candle.interval == interval,
                Candle.ts.in_(ts_list),
            )
            .all()
        }

    items: list[dict] = []
    for window in reversed(windows):
        row = stored.get(window.start_ts)
        open_px = row.open if row is not None else _open_from_window(window)
        close_px = row.close if row is not None else _close_from_window(window)
        if open_px is None or close_px is None:
            continue
        high = row.high if row is not None else max(open_px, close_px)
        low = row.low if row is not None else min(open_px, close_px)
        items.append(
            {
                "time": window.start_ts,
                "open": open_px,
                "high": high,
                "low": low,
                "close": close_px,
                "slug": window.slug,
                "outcome": window.outcome,
                "live": False,
            }
        )
    items = _merge_live(items, live)
    return {
        "asset": asset,
        "interval": interval,
        "step_sec": INTERVAL_SECONDS.get(interval, 300),
        "items": items,
    }
