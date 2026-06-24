"""Regression checks for idempotent SQL compatibility migrations."""

from __future__ import annotations

from loom.db import SCHEMA_PATH


def _schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def test_schema_migrates_legacy_memory_project_column() -> None:
    sql = _schema_sql()

    assert "ALTER TABLE memories ADD COLUMN IF NOT EXISTS project" in sql
    assert "UPDATE memories SET project = 'default'" in sql
    assert "ALTER TABLE memories ALTER COLUMN project SET NOT NULL" in sql


def test_schema_migrates_legacy_context_conflict_target() -> None:
    sql = _schema_sql()

    assert "ADD COLUMN IF NOT EXISTS topic_index" in sql
    assert "DROP CONSTRAINT %I" in sql
    assert "UNIQUE (workspace_id, channel, thread_ts, topic_index)" in sql


def test_schema_migrates_legacy_blob_conflict_target() -> None:
    sql = _schema_sql()

    assert "UPDATE conversation_blobs SET workspace_id = ''" in sql
    assert "PARTITION BY workspace_id, channel, thread_ts" in sql
    assert "UNIQUE (workspace_id, channel, thread_ts)" in sql
