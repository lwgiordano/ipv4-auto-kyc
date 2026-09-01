"""poc token identity binding

Revision ID: 009
Revises: 008

Binds each POC token to the identity it proves — RIR, POC handle (already
present), the associated ORG-ID, and/or the resource — plus a consumed_at stamp
for single-use enforcement. All nullable: pre-migration tokens have no binding
and therefore FAIL the tightened validator (fail-closed). Tokens are looked up
by id, so no extra index is added.
"""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("poc_tokens", sa.Column("rir", sa.Text(), nullable=True))
    op.add_column("poc_tokens", sa.Column("org_handle", sa.Text(), nullable=True))
    op.add_column("poc_tokens", sa.Column("resource", sa.Text(), nullable=True))
    op.add_column("poc_tokens", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("poc_tokens", "consumed_at")
    op.drop_column("poc_tokens", "resource")
    op.drop_column("poc_tokens", "org_handle")
    op.drop_column("poc_tokens", "rir")
