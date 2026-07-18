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
    """Dedup ledger: one alert per (cruise, price level, profile). A new alert
    fires only when the price drops below every previously alerted price for
    that cruise within the same profile (see deals.alerted_at_or_below) —
    profiles never suppress each other."""

    __tablename__ = "alerts_sent"

    cruise_id: Mapped[int] = mapped_column(
        ForeignKey("cruises.id", ondelete="CASCADE"), primary_key=True
    )
    price_eur: Mapped[Decimal] = mapped_column(Numeric(10, 2), primary_key=True)
    profile: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="default", server_default="default"
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


class TelegramChat(Base):
    """Chats subscribed via /start; every hot-deal alert goes to all of them."""

    __tablename__ = "telegram_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
