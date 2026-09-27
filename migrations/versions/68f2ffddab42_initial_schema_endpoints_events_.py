"""initial schema: endpoints, events, deliveries

Establishes the three tables that make up the delivery pipeline:

  endpoints  the registered destination URL, its signing secret and its rate limit
  events     the accepted payload plus the optional client idempotency key
  deliveries one row per (event, endpoint) with retry/DLQ state

Two constraints carry real correctness weight and must not be dropped:

  uq_events_endpoint_key       two concurrent ingests with the same
                               (endpoint_id, idempotency_key) cannot both
                               insert. The application handles the resulting
                               IntegrityError and returns the winning event,
                               so a client retry never double-delivers.
                               NULL keys are exempt (no dedupe requested).

  ix_deliveries_status_next    the worker's hot path is "what is due now?".
                               Without this index every poll is a full scan.

Revision ID: 68f2ffddab42
Revises:
Create Date: 2026-09-27 16:19:03.243368

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '68f2ffddab42'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('endpoints',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('secret', sa.String(length=128), nullable=False),
    sa.Column('rate_limit_per_minute', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('events',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('endpoint_id', sa.String(length=32), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['endpoint_id'], ['endpoints.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('endpoint_id', 'idempotency_key', name='uq_events_endpoint_key')
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index('ix_events_endpoint_created', ['endpoint_id', 'created_at'], unique=False)

    op.create_table('deliveries',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('event_id', sa.String(length=32), nullable=False),
    sa.Column('endpoint_id', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_status_code', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['endpoint_id'], ['endpoints.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('deliveries', schema=None) as batch_op:
        batch_op.create_index('ix_deliveries_endpoint_created', ['endpoint_id', 'created_at'], unique=False)
        batch_op.create_index('ix_deliveries_event', ['event_id'], unique=False)
        batch_op.create_index('ix_deliveries_status_next', ['status', 'next_attempt_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('deliveries', schema=None) as batch_op:
        batch_op.drop_index('ix_deliveries_status_next')
        batch_op.drop_index('ix_deliveries_event')
        batch_op.drop_index('ix_deliveries_endpoint_created')

    op.drop_table('deliveries')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index('ix_events_endpoint_created')

    op.drop_table('events')
    op.drop_table('endpoints')
