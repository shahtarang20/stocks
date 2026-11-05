"""Migrations and models must agree: one head, every table/column the models declare exists after `upgrade head`,
and the new migrations downgrade cleanly. Runs on a throwaway SQLite file (never a real database)."""

from __future__ import annotations

import pathlib

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.base import Base
import app.models  # noqa: F401  (populate metadata)

API_DIR = pathlib.Path(__file__).resolve().parents[1]


def _cfg() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    return cfg


def test_single_head_unique_ids_and_short_revision_ids():
    script = ScriptDirectory.from_config(_cfg())
    assert len(script.get_heads()) == 1, script.get_heads()
    ids = [r.revision for r in script.walk_revisions()]
    assert len(ids) == len(set(ids))
    # alembic_version.version_num is VARCHAR(32)
    assert all(len(i) <= 32 for i in ids), [i for i in ids if len(i) > 32]


def test_upgrade_head_matches_models_and_downgrades(tmp_path, monkeypatch):
    from app.core.config import settings

    db = tmp_path / "mig.db"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    cfg = _cfg()
    command.upgrade(cfg, "head")

    eng = sa.create_engine(f"sqlite:///{db}")
    insp = sa.inspect(eng)
    migrated = set(insp.get_table_names()) - {"alembic_version"}
    declared = set(Base.metadata.tables)
    assert migrated == declared, (sorted(migrated - declared), sorted(declared - migrated))
    for name, table in Base.metadata.tables.items():
        cols = {c["name"] for c in insp.get_columns(name)}
        assert cols == {c.name for c in table.columns}, (name, cols ^ {c.name for c in table.columns})

    command.downgrade(cfg, "0022_amd_gpu_support")
    left = set(sa.inspect(eng).get_table_names())
    for gone in ("stake_sync_runs", "withdrawals", "kits", "data_objects", "fanout_groups", "unit_quotes", "abuse_flags",
                 "compute_sessions", "pipeline_runs", "byoc_image_denylist", "services", "node_groups"):
        assert gone not in left, gone
    cols = {c["name"] for c in sa.inspect(eng).get_columns("provider_machines")}
    assert not ({"stake_unverified", "stake_synced_at", "datacenter_group", "cluster_addr", "sealed_verdict"} & cols)
    eng.dispose()
