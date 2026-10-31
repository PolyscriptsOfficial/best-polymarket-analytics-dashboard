"""SQLAlchemy models for tick storage and backtest replay."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(8), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    base_slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    windows: Mapped[list[Window]] = relationship(back_populates="market")


class Window(Base):
    __tablename__ = "windows"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_windows_slug"),
        Index("ix_windows_asset_interval_end", "asset", "interval", "end_ts"),
        Index("ix_windows_asset_interval_start", "asset", "interval", "start_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    asset: Mapped[str] = mapped_column(String(8), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(80), default="")
    yes_token_id: Mapped[str] = mapped_column(String(80), default="")
    no_token_id: Mapped[str] = mapped_column(String(80), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    start_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    strike_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    opening_oracle_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    strike_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)
    final_oracle_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Settlement inputs: window-open 60s TWAP (the target) and the TWAP at close.
    twap_initial: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_twap: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "gamma" = official winner; "inferred" = our own final TWAP vs target.
    outcome_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    inferred_outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)
    outcome_mismatch: Mapped[bool] = mapped_column(Boolean, default=False)
    # Window features for regime stats (filled at finalize / backfill).
    realized_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_move: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    market: Mapped[Market] = relationship(back_populates="windows")
    ticks: Mapped[list[Tick]] = relationship(back_populates="window", cascade="all, delete-orphan")


class Tick(Base):
    """One market second: only values observed live (plus the model's fair value).

    Anything that is arithmetic on these columns (mids, spread, YES+NO,
    seconds left, distance to strike, TWAP gap, ...) is derived on read by
    ``db/tick_fields.py``, so it is never stored twice.
    """

    __tablename__ = "ticks"
    __table_args__ = (Index("ix_ticks_window_ts", "window_id", "ts_ms"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_id: Mapped[int] = mapped_column(ForeignKey("windows.id"), nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Feeds: Chainlink spot, Binance spot, Polymarket RTDS 60s Chainlink TWAP.
    oracle_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    binance_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    twap_oracle: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Best quotes in cents (0 = no quote).
    yes_ask: Mapped[float] = mapped_column(Float, default=0.0)
    yes_bid: Mapped[float] = mapped_column(Float, default=0.0)
    no_ask: Mapped[float] = mapped_column(Float, default=0.0)
    no_bid: Mapped[float] = mapped_column(Float, default=0.0)
    # Order book (shares): size at the best level and within the depth band.
    yes_ask_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_bid_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_ask_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_bid_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_ask_depth: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_bid_depth: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_ask_depth: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_bid_depth: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Trades: last fill price (cents) and USDC traded on YES+NO in this second.
    yes_last_trade: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_last_trade: Mapped[float | None] = mapped_column(Float, nullable=True)
    traded_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Fair-value model (analytics/fair_value.py), YES in cents. Stored because it
    # depends on the 15 minutes of spot before this second.
    fair_yes: Mapped[float | None] = mapped_column(Float, nullable=True)

    window: Mapped[Window] = relationship(back_populates="ticks")


class WindowSnapshot(Base):
    """One row per window per seconds-before-close checkpoint.

    Small and permanent (survives tick retention), so calibration, regime stats
    and rule backtests are SQL over this table instead of parquet scans.
    """

    __tablename__ = "window_snapshots"
    __table_args__ = (
        UniqueConstraint("window_id", "t_minus", name="uq_window_snapshots_window_t"),
        Index("ix_window_snapshots_market_t", "asset", "interval", "t_minus", "start_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_id: Mapped[int] = mapped_column(ForeignKey("windows.id"), nullable=False)
    asset: Mapped[str] = mapped_column(String(8), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    start_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    t_minus: Mapped[int] = mapped_column(Integer, nullable=False)
    yes_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_ask_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_ask_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    oracle_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    binance_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    twap_diff: Mapped[float | None] = mapped_column(Float, nullable=True)
    fair_yes: Mapped[float | None] = mapped_column(Float, nullable=True)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("asset", "interval", "ts", name="uq_candles_asset_interval_ts"),
        Index("ix_candles_asset_ts", "asset", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(8), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), default="1m")
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)


class CollectorGap(Base):
    __tablename__ = "collector_gaps"
    __table_args__ = (Index("ix_collector_gaps_source_ended", "source", "ended_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")


class BinanceTrade(Base):
    """Every Binance spot trade from the WebSocket stream."""

    __tablename__ = "binance_trades"
    __table_args__ = (Index("ix_binance_trades_asset_ts", "asset", "ts_ms"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(8), nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class OracleTick(Base):
    """Every Chainlink/RTDS (and fallback) oracle price update."""

    __tablename__ = "oracle_ticks"
    __table_args__ = (Index("ix_oracle_ticks_asset_ts", "asset", "ts_ms"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(8), nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="chainlink")


class ClobQuote(Base):
    """Every CLOB best-bid/ask change from the market WebSocket."""

    __tablename__ = "clob_quotes"
    __table_args__ = (
        Index("ix_clob_quotes_token_ts", "token_id", "ts_ms"),
        Index("ix_clob_quotes_window_ts", "window_id", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_id: Mapped[str] = mapped_column(String(80), nullable=False)
    window_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    asset: Mapped[str | None] = mapped_column(String(8), nullable=True)
    interval: Mapped[str | None] = mapped_column(String(8), nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    best_ask: Mapped[float] = mapped_column(Float, default=0.0)
    best_bid: Mapped[float] = mapped_column(Float, default=0.0)
    event_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
