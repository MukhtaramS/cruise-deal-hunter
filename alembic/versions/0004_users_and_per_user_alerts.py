"""multi-user: users table + per-user alerts_sent; drops telegram_chats

- users: onboarding preferences per Telegram chat. Existing telegram_chats
  rows are migrated as already-onboarded users with NO preferences set
  (all-NULL = "match everything"), so the existing subscriber keeps
  receiving every alert exactly as before.
- alerts_sent: `profile` (default|visa_ru) is replaced by `chat_id`.
  Existing 'default' rows are copied once per migrated chat so nobody gets
  re-alerted for deals they already saw; 'visa_ru' rows are dropped (their
  alert set was a subset of default's). Table recreate keeps this portable
  across Postgres and SQLite, same approach as migration 0003.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("home_region", sa.String(length=32), nullable=True),
        sa.Column("passport_country", sa.String(length=32), nullable=True),
        sa.Column("budget_per_night_max", sa.Numeric(10, 2), nullable=True),
        sa.Column("trip_length_pref", sa.String(length=16), nullable=True),
        sa.Column("departure_prefs", sa.Text(), nullable=True),
        sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "INSERT INTO users (chat_id, onboarded_at) "
        "SELECT chat_id, subscribed_at FROM telegram_chats"
    )

    op.create_table(
        "alerts_sent_new",
        sa.Column(
            "cruise_id",
            sa.Integer(),
            sa.ForeignKey("cruises.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("price_eur", sa.Numeric(10, 2), primary_key=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=False,
            server_default="0",
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        "INSERT INTO alerts_sent_new (cruise_id, price_eur, chat_id, sent_at) "
        "SELECT a.cruise_id, a.price_eur, t.chat_id, a.sent_at "
        "FROM alerts_sent a, telegram_chats t "
        "WHERE a.profile = 'default'"
    )
    op.drop_table("alerts_sent")
    op.rename_table("alerts_sent_new", "alerts_sent")

    op.drop_table("telegram_chats")


def downgrade() -> None:
    op.create_table(
        "telegram_chats",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column(
            "subscribed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        "INSERT INTO telegram_chats (chat_id, subscribed_at) "
        "SELECT chat_id, COALESCE(onboarded_at, CURRENT_TIMESTAMP) FROM users"
    )

    op.create_table(
        "alerts_sent_old",
        sa.Column(
            "cruise_id",
            sa.Integer(),
            sa.ForeignKey("cruises.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("price_eur", sa.Numeric(10, 2), primary_key=True),
        sa.Column(
            "profile", sa.String(length=32), primary_key=True, server_default="default"
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        "INSERT INTO alerts_sent_old (cruise_id, price_eur, profile, sent_at) "
        "SELECT cruise_id, price_eur, 'default', MIN(sent_at) FROM alerts_sent "
        "GROUP BY cruise_id, price_eur"
    )
    op.drop_table("alerts_sent")
    op.rename_table("alerts_sent_old", "alerts_sent")

    op.drop_table("users")
