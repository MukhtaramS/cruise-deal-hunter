"""initial schema: cruises, price_snapshots, alerts_sent

Revision ID: 0001
Revises:
Create Date: 2026-07-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cruises",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("cruise_line", sa.String(length=100), nullable=False),
        sa.Column("ship", sa.String(length=100), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("route_hash", sa.String(length=40), nullable=False),
        sa.Column("departure_port", sa.String(length=100), nullable=False),
        sa.Column("departure_date", sa.Date(), nullable=False),
        sa.Column("nights", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.UniqueConstraint("source", "url", name="uq_cruises_source_url"),
    )
    op.create_index("ix_cruises_route_hash", "cruises", ["route_hash"])

    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cruise_id",
            sa.Integer(),
            sa.ForeignKey("cruises.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cabin_type", sa.String(length=50), nullable=False),
        sa.Column("price_eur", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "scraped_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_price_snapshots_cruise_scraped",
        "price_snapshots",
        ["cruise_id", "scraped_at"],
    )

    op.create_table(
        "alerts_sent",
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


def downgrade() -> None:
    op.drop_table("alerts_sent")
    op.drop_index("ix_price_snapshots_cruise_scraped", table_name="price_snapshots")
    op.drop_table("price_snapshots")
    op.drop_index("ix_cruises_route_hash", table_name="cruises")
    op.drop_table("cruises")
