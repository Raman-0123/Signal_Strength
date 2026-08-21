"""Create durable email campaign tables.

Revision ID: 20260818_01
Revises:
"""
from __future__ import annotations

from typing import Sequence

from alembic import op

from speedy_scraper.campaign_models import Base

revision: str = "20260818_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
