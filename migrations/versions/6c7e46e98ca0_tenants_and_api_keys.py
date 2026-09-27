"""tenants and API keys

Multi-tenancy. Endpoints, and therefore events and deliveries, belong to a
tenant; API keys authenticate a caller to exactly one tenant.

The column is added in three steps rather than one, because a single
`ADD COLUMN ... NOT NULL` has no default and no backfill: it fails outright
against any database that already contains endpoints. Autogenerate produced
exactly that, so this revision is hand-written.

  1. add `tenant_id` nullable
  2. create a `legacy` tenant and point existing endpoints at it
  3. add the foreign key, then enforce NOT NULL

Step 2 is the data migration. It keeps pre-existing endpoints usable and
reachable under the new authorisation model rather than orphaning them.

Revision ID: 6c7e46e98ca0
Revises: 867af545e9dd
Create Date: 2026-09-27 16:46:31.835255

"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision = '6c7e46e98ca0'
down_revision = '867af545e9dd'
branch_labels = None
depends_on = None

LEGACY_TENANT_NAME = "legacy"
TENANT_FK = "fk_endpoints_tenant_id"


def upgrade() -> None:
    op.create_table(
        'tenants',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'api_keys',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('key_prefix', sa.String(length=16), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key_prefix', name='uq_api_keys_prefix'),
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index('ix_api_keys_tenant', ['tenant_id'], unique=False)

    # 1. nullable first
    with op.batch_alter_table('endpoints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=32), nullable=True))

    # 2. data migration: one legacy tenant owns everything that predates this.
    # CURRENT_TIMESTAMP is inlined as SQL; a bound parameter would send the
    # string "now" rather than the database clock.
    connection = op.get_bind()
    legacy_id = uuid.uuid4().hex
    connection.execute(
        sa.text(
            "INSERT INTO tenants (id, name, created_at) "
            "VALUES (:id, :name, CURRENT_TIMESTAMP)"
        ),
        {"id": legacy_id, "name": LEGACY_TENANT_NAME},
    )
    connection.execute(
        sa.text("UPDATE endpoints SET tenant_id = :id WHERE tenant_id IS NULL"),
        {"id": legacy_id},
    )

    # 3. integrity from here on
    with op.batch_alter_table('endpoints', schema=None) as batch_op:
        batch_op.create_foreign_key(
            TENANT_FK, 'tenants', ['tenant_id'], ['id'], ondelete='CASCADE'
        )
        batch_op.create_index('ix_endpoints_tenant', ['tenant_id'], unique=False)
        batch_op.alter_column('tenant_id', existing_type=sa.String(length=32), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('endpoints', schema=None) as batch_op:
        batch_op.drop_constraint(TENANT_FK, type_='foreignkey')
        batch_op.drop_index('ix_endpoints_tenant')
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index('ix_api_keys_tenant')

    op.drop_table('api_keys')
    # The legacy tenant is left behind rather than deleted: if it still owns
    # endpoints after a partially-completed downgrade, dropping it would fail
    # the migration rather than silently orphan rows.
    op.drop_table('tenants')
