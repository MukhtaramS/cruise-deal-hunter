from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Cruise(Base):
    """One cruise offer page on one portal. The same physical cruise on a
    different portal is a separate row, linked by an identical route_hash."""

    __tablename__ = "cruises"
    __table_args__ = (
        UniqueConstraint("source", "url", name="uq_cruises_source_url"),
        Index("ix_cruises_route_hash", "route_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50))  # portal slug, e.g. "kreuzfahrten-de"
    cruise_line: Mapped[str] = mapped_column(String(100))
    ship: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text)
    route_hash: Mapped[str] = mapped_column(String(40))  # sha1 over normalized identity fields
    departure_port: Mapped[str] = mapped_column(String(100))
    departure_date: Mapped[date] = mapped_column(Date)
    nights: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text)

    snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="cruise", cascade="all, delete-orphan"
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (
        Index("ix_price_snapshots_cruise_scraped", "cruise_id", "scraped_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cruise_id: Mapped[int] = mapped_column(
        ForeignKey("cruises.id", ondelete="CASCADE")
    )
    cabin_type: Mapped[str] = mapped_column(String(50))  # inside | outside | balcony | suite
    price_eur: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    cruise: Mapped[Cruise] = relationship(back_populates="snapshots")


class AlertSent(Base):
    """Dedup ledger: one alert per (cruise, price level, user chat). A new
    alert fires only when the price drops below every previously alerted
    price for that cruise for that user (see deals.alerted_at_or_below) —
    users never suppress each other. chat_id 0 is the legacy/seed sentinel
    used by the single-channel detect_hot_deals path."""

    __tablename__ = "alerts_sent"

    cruise_id: Mapped[int] = mapped_column(
        ForeignKey("cruises.id", ondelete="CASCADE"), primary_key=True
    )
    price_eur: Mapped[Decimal] = mapped_column(Numeric(10, 2), primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False, default=0, server_default="0"
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RouteCountries(Base):
    """LLM-inferred countries visited per route, cached forever by route_hash.
    `countries` is comma-separated ISO alpha-2 ("TR,GR"); empty string means
    the LLM couldn't tell (cached so we don't re-ask every run)."""

    __tablename__ = "route_countries"

    route_hash: Mapped[str] = mapped_column(String(40), primary_key=True)
    countries: Mapped[str] = mapped_column(Text, default="")
    inferred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class User(Base):
    """One Telegram user with onboarding preferences. Alerts are matched and
    deduped per user (see app/matching.py + jobs.route_deals). All preference
    fields are nullable = "no filter"; a user only receives alerts once
    onboarded_at is set. departure_prefs is comma-separated region slugs
    (mediterranean, northern_europe, caribbean, black_sea) — same CSV
    convention as route_countries; NULL = any region."""

    __tablename__ = "users"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    home_region: Mapped[str | None] = mapped_column(String(32), nullable=True)  # de|uk|fr|cis|other
    passport_country: Mapped[str | None] = mapped_column(String(32), nullable=True)  # RU|KZ|EU|UK|other
    budget_per_night_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    trip_length_pref: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 2-4|5-9|10+
    departure_prefs: Mapped[str | None] = mapped_column(Text, nullable=True)
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
