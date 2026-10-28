"""Fixed-size 1-second price tape (last price per wall-clock second)."""

from __future__ import annotations

from collections import deque


class SecondTape:
    """Ring buffer of (second, last price) fed from a raw tick stream.

    Raw feeds arrive at very different rates (Binance trades: many per second;
    Chainlink: ~1 Hz). Bucketing to whole seconds gives every analytic the same
    grain and bounded memory regardless of feed rate.
    """

    __slots__ = ("_buf",)

    def __init__(self, maxlen: int = 1800) -> None:
        self._buf: deque[tuple[int, float]] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self._buf)

    def add(self, ts_ms: int, price: float) -> None:
        if price is None or price <= 0:
            return
        sec = int(ts_ms) // 1000
        if self._buf:
            last_sec = self._buf[-1][0]
            if sec == last_sec:
                self._buf[-1] = (sec, float(price))
                return
            if sec < last_sec:
                return  # late/out-of-order tick; the tape is append-only
        self._buf.append((sec, float(price)))

    def last(self) -> float | None:
        return self._buf[-1][1] if self._buf else None

    def last_sec(self) -> int | None:
        return self._buf[-1][0] if self._buf else None

    def dense(self, start_sec: int, end_sec: int) -> list[float | None]:
        """Forward-filled price for every second in [start_sec, end_sec].

        Seconds before the first known price are None.
        """
        if end_sec < start_sec:
            return []
        out: list[float | None] = [None] * (end_sec - start_sec + 1)
        carry: float | None = None
        it = iter(self._buf)
        pending = next(it, None)
        # Seed carry with the last price at or before start_sec.
        while pending is not None and pending[0] <= start_sec:
            carry = pending[1]
            pending = next(it, None)
        for i in range(len(out)):
            sec = start_sec + i
            while pending is not None and pending[0] <= sec:
                carry = pending[1]
                pending = next(it, None)
            out[i] = carry
        return out
