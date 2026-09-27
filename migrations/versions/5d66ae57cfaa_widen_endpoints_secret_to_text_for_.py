"""widen endpoints.secret to Text for Fernet envelopes

The secret column was VARCHAR(128), sized for a 64-character hex secret. Once
secrets are encrypted at rest the stored value is a Fernet envelope
(`enc:v1:<token>`), which is about 147 characters for a generated secret.

SQLite does not enforce VARCHAR length, so the whole suite passed locally and
on the old CI runner. PostgreSQL does enforce it, and endpoint registration
failed with StringDataRightTruncation the first time it ran there. Widening to
Text removes the bound rather than guessing a new one.

Revision ID: 5d66ae57cfaa
Revises: 68f2ffddab42
Create Date: 2026-09-27 16:30:14.679910

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '5d66ae57cfaa'
down_revision = '68f2ffddab42'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('endpoints', schema=None) as batch_op:
        batch_op.alter_column('secret',
               existing_type=sa.VARCHAR(length=128),
               type_=sa.Text(),
               existing_nullable=False)


def downgrade() -> None:
    # Deliberately lossy on the way down: existing Fernet envelopes exceed
    # VARCHAR(128) and cannot be represented. Downgrading an encrypted database
    # to the old schema will truncate secrets rather than fail silently, so
    # this is only safe on an empty or pre-encryption database.
    with op.batch_alter_table('endpoints', schema=None) as batch_op:
        batch_op.alter_column('secret',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=128),
               existing_nullable=False)
