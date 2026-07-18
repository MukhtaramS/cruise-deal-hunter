"""profiles: per-profile alert dedup + route_countries cache

alerts_sent gains a `profile` column as part of the PK. The table is
recreated (portable across Postgres and SQLite) and existing rows are copied
for BOTH known profiles, so enabling visa_ru does not re-alert deals the user
already saw under the old single-profile scheme.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
            "profile",
            sa.String(length=32),
            primary_key=True,
            server_default="default",
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        "INSERT INTO alerts_sent_new (cruise_id, price_eur, profile, sent_at) "
        "SELECT cruise_id, price_eur, 'default', sent_at FROM alerts_sent "
        "UNION ALL "
        "SELECT cruise_id, price_eur, 'visa_ru', sent_at FROM alerts_sent"
    )
    op.drop_table("alerts_sent")
    op.rename_table("alerts_sent_new", "alerts_sent")

    op.create_table(
        "route_countries",
        sa.Column("route_hash", sa.String(length=40), primary_key=True),
        sa.Column("countries", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "inferred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("route_countries")

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
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        "INSERT INTO alerts_sent_old (cruise_id, price_eur, sent_at) "
        "SELECT cruise_id, price_eur, sent_at FROM alerts_sent "
        "WHERE profile = 'default'"
    )
    op.drop_table("alerts_sent")
    op.rename_table("alerts_sent_old", "alerts_sent")
