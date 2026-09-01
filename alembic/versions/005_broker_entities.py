"""broker_entities, seeded from the normative broker_policy.json

Revision ID: 005
Revises: 004
"""

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None

_POLICY_FILE = (
    Path(__file__).resolve().parents[2]
    / "KYC_Tool_Build_Package"
    / "machine_readable"
    / "broker_policy.json"
)


def upgrade() -> None:
    table = op.create_table(
        "broker_entities",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("policy", sa.Text(), nullable=False),
        sa.Column("aliases", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("domains", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("email_domains", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("org_ids", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("poc_handles", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("asns", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    policy = json.loads(_POLICY_FILE.read_text())
    op.bulk_insert(
        table,
        [
            {"id": uuid.uuid4().hex, "name": entity["name"], "policy": entity["policy"]}
            for entity in policy["entities"]
        ],
    )


def downgrade() -> None:
    op.drop_table("broker_entities")
