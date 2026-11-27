"""The golden /api/v1 schema: generated, committed, and never stale."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from magent import api

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_api_schema.py"
_spec = importlib.util.spec_from_file_location("gen_api_schema", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
gen_api_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_api_schema)


def test_the_committed_schema_matches_the_wire_types():
    committed = gen_api_schema.SCHEMA_PATH.read_text(encoding="utf-8")
    assert committed == gen_api_schema.render(), (
        "docs/api/v1.schema.json is stale: run uv run python scripts/gen_api_schema.py"
    )


def test_every_route_names_a_wire_type():
    assert {r.data for r in api.ROUTES} <= set(api.WIRE_TYPES)


def test_every_object_is_closed():
    defs = gen_api_schema.build()["$defs"]
    row = defs["SessionRow"]
    assert row["additionalProperties"] is False
    assert set(row["required"]) == set(row["properties"])
    assert defs["Error"]["required"] == ["code", "message"]


def test_check_mode_reports_a_stale_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gen_api_schema, "SCHEMA_PATH", tmp_path / "v1.schema.json")
    assert gen_api_schema.main(["--check"]) == 1
    assert gen_api_schema.main([]) == 0
    assert gen_api_schema.main(["--check"]) == 0
