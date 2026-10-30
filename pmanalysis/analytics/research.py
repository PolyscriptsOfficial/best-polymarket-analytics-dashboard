"""Research queries over window_snapshots + windows: calibration, regimes, backtest.

Everything here reads the small per-window tables (never parquet), so a
year of 5m windows is a few hundred thousand narrow rows at most.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from pmanalysis.analytics.features import checkpoints_for
from pmanalysis.config import INTERVAL_SECONDS
from pmanalysis.db.models import Window, WindowSnapshot

ET = ZoneInfo("America/New_York")
MAX_DAYS = 366
_RESOLVED = ("UP", "DOWN")


def _since(days: int) -> int:
    days = max(1, min(MAX_DAYS, int(days)))
    return int(time.time()) - days * 86400


def _wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson interval for a win rate, in percent."""
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(100 * (centre - half), 1), round(100 * (centre + half), 1)


def _rate_row(label: str, wins: int, n: int, **extra) -> dict:
    return {
        "label": label,
        "n": n,
        "up": wins,
        "up_rate": round(100 * wins / n, 1) if n else None,
        "ci": _wilson(wins, n),
        **extra,
    }


def _snapshot_rows(session: Session, asset: str, interval: str, t_minus: int, days: int):
    return (
        session.query(WindowSnapshot, Window.outcome)
        .join(Window, Window.id == WindowSnapshot.window_id)
        .filter(
            WindowSnapshot.asset == asset,
            WindowSnapshot.interval == interval,
            WindowSnapshot.t_minus == t_minus,
            WindowSnapshot.start_ts >= _since(days),
            Window.outcome.in_(_RESOLVED),
        )
        .order_by(WindowSnapshot.start_ts)
        .all()
    )


def _market_yes(snap: WindowSnapshot) -> float | None:
    """Market-implied YES in cents: bid/ask mid, else the ask."""
    if snap.yes_ask and snap.yes_bid:
        return (snap.yes_ask + snap.yes_bid) / 2
    return snap.yes_ask or None


# --- Calibration -------------------------------------------------------------


def _calibration_bins(pairs: list[tuple[float, bool]], bins: int) -> dict:
    width = 100 / bins
    buckets = [[0, 0, 0.0] for _ in range(bins)]  # n, up, sum(price)
    brier = 0.0
    for price, up in pairs:
        idx = min(bins - 1, max(0, int(price // width)))
        b = buckets[idx]
        b[0] += 1
        b[1] += int(up)
        b[2] += price
        brier += (price / 100 - (1.0 if up else 0.0)) ** 2
    rows = []
    for i, (n, up, total) in enumerate(buckets):
        if not n:
            continue
        lo, hi = round(i * width), round((i + 1) * width)
        rows.append(
            _rate_row(f"{lo}–{hi}¢", up, n, lo=lo, hi=hi, avg_price=round(total / n, 1))
        )
    n = len(pairs)
    return {"n": n, "brier": round(brier / n, 4) if n else None, "bins": rows}


def calibration(
    session: Session,
    *,
    asset: str,
    interval: str,
    t_minus: int,
    days: int = 30,
    bins: int = 10,
) -> dict:
    """Implied YES (market mid and model fair value) vs realized UP frequency."""
    market: list[tuple[float, bool]] = []
    fair: list[tuple[float, bool]] = []
    for snap, outcome in _snapshot_rows(session, asset, interval, t_minus, days):
        up = outcome == "UP"
        price = _market_yes(snap)
        if price is not None:
            market.append((price, up))
        if snap.fair_yes is not None:
            fair.append((snap.fair_yes, up))
    return {
        "asset": asset,
        "interval": interval,
        "t_minus": t_minus,
        "days": days,
        "checkpoints": list(checkpoints_for(interval)),
        "market": _calibration_bins(market, bins),
        "fair": _calibration_bins(fair, bins),
    }


# --- Regime stats ------------------------------------------------------------


def _quantile_edges(values: list[float], parts: int) -> list[float]:
    ordered = sorted(values)
    return [ordered[min(len(ordered) - 1, int(len(ordered) * k / parts))] for k in range(1, parts)]


def _bucket(value: float, edges: list[float]) -> int:
    for i, edge in enumerate(edges):
        if value < edge:
            return i
    return len(edges)


def _quantile_table(pairs: list[tuple[float, bool]], names: list[str], fmt) -> list[dict]:
    if len(pairs) < len(names):
        return []
    edges = _quantile_edges([v for v, _ in pairs], len(names))
    counts = [[0, 0, math.inf, -math.inf] for _ in names]
    for value, up in pairs:
        c = counts[_bucket(value, edges)]
        c[0] += 1
        c[1] += int(up)
        c[2] = min(c[2], value)
        c[3] = max(c[3], value)
    return [
        _rate_row(name, up, n, range=f"{fmt(lo)} … {fmt(hi)}")
        for name, (n, up, lo, hi) in zip(names, counts)
        if n
    ]


def regime_stats(session: Session, *, asset: str, interval: str, days: int = 30) -> dict:
    rows = (
        session.query(Window.start_ts, Window.outcome, Window.realized_vol, Window.open_move)
        .filter(
            Window.asset == asset,
            Window.interval == interval,
            Window.start_ts >= _since(days),
            Window.outcome.in_(_RESOLVED),
        )
        .order_by(Window.start_ts)
        .all()
    )
    step = INTERVAL_SECONDS[interval]
    hours = [[0, 0] for _ in range(24)]
    streak_up = {k: [0, 0] for k in range(1, 5)}
    streak_down = {k: [0, 0] for k in range(1, 5)}
    vol_pairs: list[tuple[float, bool]] = []
    move_pairs: list[tuple[float, bool]] = []
    wins = 0
    run_side: str | None = None
    run_len = 0
    prev_start: int | None = None

    for start_ts, outcome, realized_vol, open_move in rows:
        up = outcome == "UP"
        wins += int(up)
        hour = datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(ET).hour
        hours[hour][0] += 1
        hours[hour][1] += int(up)

        # Streaks only count back-to-back windows (a gap breaks the run).
        if prev_start is not None and start_ts - prev_start == step and run_side:
            table = streak_up if run_side == "UP" else streak_down
            for k in range(1, min(run_len, 4) + 1):
                table[k][0] += 1
                table[k][1] += int(up)
        else:
            run_side, run_len = None, 0
        if outcome == run_side:
            run_len += 1
        else:
            run_side, run_len = outcome, 1
        prev_start = start_ts

        if realized_vol is not None:
            vol_pairs.append((realized_vol, up))
        if open_move is not None:
            move_pairs.append((open_move, up))

    n = len(rows)
    return {
        "asset": asset,
        "interval": interval,
        "days": days,
        "overall": _rate_row("All windows", wins, n),
        "by_hour_et": [
            _rate_row(f"{h:02d}:00", up, cnt, hour=h) for h, (cnt, up) in enumerate(hours) if cnt
        ],
        "after_streak": [
            _rate_row(f"After {k}× UP", up, cnt, side="UP", k=k)
            for k, (cnt, up) in streak_up.items()
            if cnt
        ]
        + [
            _rate_row(f"After {k}× DOWN", up, cnt, side="DOWN", k=k)
            for k, (cnt, up) in streak_down.items()
            if cnt
        ],
        "by_volatility": _quantile_table(
            vol_pairs, ["Low vol", "Mid vol", "High vol"], lambda v: f"${v:.3f}/√s"
        ),
        "by_open_move": _quantile_table(
            move_pairs,
            ["Strong down", "Down", "Flat", "Up", "Strong up"],
            lambda v: f"{v:+.2f}",
        ),
    }


# --- Rule backtest -----------------------------------------------------------


@dataclass(frozen=True)
class BacktestSpec:
    asset: str
    interval: str
    t_minus: int
    days: int = 30
    side: str = "AUTO"  # YES | NO | AUTO (follow the sign of the TWAP diff)
    min_ask: float = 1.0
    max_ask: float = 99.0
    min_diff: float | None = None  # TWAP diff in the side's favour, dollars
    min_edge: float | None = None  # fair value - ask, cents
    shares: float = 10.0
    require_size: bool = False  # skip when best-ask size < shares
    slippage_cents: float = 0.0
    fee_pct: float = 0.0  # taker fee, % of notional


def _max_drawdown(equity: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst


def run_backtest(session: Session, spec: BacktestSpec) -> dict:
    side_rule = spec.side.upper()
    trades: list[dict] = []
    skipped = {"no_quote": 0, "price": 0, "diff": 0, "edge": 0, "size": 0, "flat": 0}

    for snap, outcome in _snapshot_rows(session, spec.asset, spec.interval, spec.t_minus, spec.days):
        diff = snap.twap_diff
        if side_rule == "AUTO":
            if diff is None or diff == 0:
                skipped["flat"] += 1
                continue
            side = "YES" if diff > 0 else "NO"
        else:
            side = side_rule
        ask = snap.yes_ask if side == "YES" else snap.no_ask
        if not ask:
            skipped["no_quote"] += 1
            continue
        if not spec.min_ask <= ask <= spec.max_ask:
            skipped["price"] += 1
            continue
        if spec.min_diff is not None:
            favour = None if diff is None else (diff if side == "YES" else -diff)
            if favour is None or favour < spec.min_diff:
                skipped["diff"] += 1
                continue
        edge = None
        if snap.fair_yes is not None:
            fair_side = snap.fair_yes if side == "YES" else 100 - snap.fair_yes
            edge = round(fair_side - ask, 1)
        if spec.min_edge is not None and (edge is None or edge < spec.min_edge):
            skipped["edge"] += 1
            continue
        size = snap.yes_ask_size if side == "YES" else snap.no_ask_size
        if spec.require_size and (size is None or size < spec.shares):
            skipped["size"] += 1
            continue

        fill = min(99.9, ask + spec.slippage_cents)
        fee = fill * spec.fee_pct / 100
        won = (outcome == "UP") == (side == "YES")
        pnl_cents = (100.0 if won else 0.0) - fill - fee
        trades.append(
            {
                "start_ts": snap.start_ts,
                "side": side,
                "ask": ask,
                "fill": round(fill, 2),
                "twap_diff": diff,
                "fair_yes": snap.fair_yes,
                "edge": edge,
                "outcome": outcome,
                "won": won,
                "pnl_usd": round(pnl_cents / 100 * spec.shares, 4),
            }
        )

    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    cost = sum((t["fill"] + t["fill"] * spec.fee_pct / 100) / 100 * spec.shares for t in trades)
    equity: list[float] = []
    running = 0.0
    for trade in trades:
        running += trade["pnl_usd"]
        equity.append(running)
    stride = max(1, n // 500)
    curve = [
        {"ts": trades[i]["start_ts"], "equity": round(equity[i], 2)}
        for i in range(0, n, stride)
    ]
    if n and curve[-1]["ts"] != trades[-1]["start_ts"]:
        curve.append({"ts": trades[-1]["start_ts"], "equity": round(equity[-1], 2)})
    avg_fill = sum(t["fill"] for t in trades) / n if n else None
    return {
        "spec": spec.__dict__,
        "summary": {
            "trades": n,
            "wins": wins,
            "win_rate": round(100 * wins / n, 1) if n else None,
            "win_rate_ci": _wilson(wins, n),
            # Win rate needed to break even at the average fill (fees included).
            "breakeven_rate": (
                round(avg_fill * (1 + spec.fee_pct / 100), 1) if avg_fill is not None else None
            ),
            "avg_fill": round(avg_fill, 2) if avg_fill is not None else None,
            "pnl_usd": round(running, 2),
            "cost_usd": round(cost, 2),
            "roi_pct": round(100 * running / cost, 2) if cost else None,
            "max_drawdown_usd": round(_max_drawdown(equity), 2),
            "skipped": skipped,
        },
        "equity": curve,
        "trades": trades[-200:][::-1],
    }
