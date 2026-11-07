"""Make the database match the models for api_keys.key_hash and vendor_services.key.

What each side said before this revision:

* The models declare ``unique=True, index=True`` on ``ApiKey.key_hash`` and
  ``VendorService.key``. SQLAlchemy renders that as ONE unique index named
  ``ix_<table>_<column>`` and no separate constraint.
* Migrations 0001 / 0013 created a named UNIQUE CONSTRAINT (``uq_api_keys_key_hash``,
  ``uq_vendor_services_key``) PLUS a separate NON-unique index with the
  ``ix_`` name. On Postgres the constraint is backed by its own unique index,
  so uniqueness was in fact enforced, but the schema differs from the models
  (``alembic check`` reports the constraint as removed, the ``ix_`` index as
  removed, and the ``ix_`` index as changed from non-unique to unique).

This revision makes the database the same shape as the models: the ``ix_`` index
becomes UNIQUE and the redundant named constraint is dropped. Uniqueness is held
throughout (the unique index is created before the constraint is dropped).

Fails closed: before making anything unique the migration looks for duplicate
values and raises a RuntimeError naming the table and the number of duplicated
values. It never deletes or rewrites data; resolve the duplicates by hand and
re-run.

Idempotent: each step inspects the live schema and is skipped when already done.
SQLite uses ``batch_alter_table`` for the constraint drop.

Revision ID: 0043_unique_key_constraints
Revises: 0042_agent_releases
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0043_unique_key_constraints"
down_revision: Union[str, None] = "0042_agent_releases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, index name, constraint name)
_TARGETS = (
    ("api_keys", "key_hash", "ix_api_keys_key_hash", "uq_api_keys_key_hash"),
    ("vendor_services", "key", "ix_vendor_services_key", "uq_vendor_services_key"),
)


def _inspector():
    # Offline (--sql) mode has no connection to inspect; callers then assume the
    # state the earlier migrations left behind.
    if context.is_offline_mode():
        return None
    return sa.inspect(op.get_bind())


def _find_index(insp, table: str, name: str):
    if insp is None:
        return {"unique": False}
    for ix in insp.get_indexes(table):
        if ix.get("name") == name:
            return ix
    return None


def _has_constraint(insp, table: str, name: str) -> bool:
    if insp is None:
        return True
    return any(uc.get("name") == name for uc in insp.get_unique_constraints(table))


def _assert_no_duplicates(table: str, column: str) -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    # table/column come from the constant _TARGETS above, never from input.
    dupes = bind.execute(
        sa.text(
            f'SELECT COUNT(*) FROM (SELECT "{column}" FROM "{table}" '  # noqa: S608
            f'GROUP BY "{column}" HAVING COUNT(*) > 1) AS d'
        )
    ).scalar_one()
    if dupes:
        raise RuntimeError(
            f"Cannot make {table}.{column} unique: {dupes} duplicated value(s) found in "
            f"table {table}. Nothing was changed or deleted. Resolve the duplicates "
            f"manually, then re-run the migration."
        )


def upgrade() -> None:
    for table, column, ix_name, uq_name in _TARGETS:
        insp = _inspector()
        ix = _find_index(insp, table, ix_name)
        has_uq = _has_constraint(insp, table, uq_name)

        if ix is None or not ix.get("unique"):
            _assert_no_duplicates(table, column)
            if ix is not None:
                op.drop_index(ix_name, table_name=table)
            op.create_index(ix_name, table, [column], unique=True)

        if has_uq:
            with op.batch_alter_table(table) as batch_op:
                batch_op.drop_constraint(uq_name, type_="unique")


def downgrade() -> None:
    # Restore the shape migrations 0001 / 0013 created: named unique constraint
    # plus a plain (non-unique) index. Uniqueness stays enforced by the constraint.
    for table, column, ix_name, uq_name in _TARGETS:
        insp = _inspector()
        ix = _find_index(insp, table, ix_name)
        has_uq = _has_constraint(insp, table, uq_name)

        if not has_uq:
            _assert_no_duplicates(table, column)
            with op.batch_alter_table(table) as batch_op:
                batch_op.create_unique_constraint(uq_name, [column])

        if ix is not None and ix.get("unique"):
            op.drop_index(ix_name, table_name=table)
            ix = None
        if ix is None:
            op.create_index(ix_name, table, [column])
