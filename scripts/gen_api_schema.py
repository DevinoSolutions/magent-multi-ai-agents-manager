"""Write docs/api/v1.schema.json: the golden JSON Schema of every /api/v1
payload (spec 3.9), generated from ``magent.api.WIRE_TYPES`` with pydantic's
``TypeAdapter``.

    uv run python scripts/gen_api_schema.py           # rewrite the file
    uv run python scripts/gen_api_schema.py --check   # exit 1 when it is stale

Every object schema is CLOSED (no extra keys) and every key is required
unless its default is null, so a field added, dropped or renamed in a wire
dataclass fails tests/unit/test_api_schema.py until this is re-run and the
diff reviewed. magent-app's adapter is written against this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from magent.api import WIRE_TYPES

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "api" / "v1.schema.json"
_REF = "#/$defs/{model}"


def _close(node: object) -> None:
    if isinstance(node, dict):
        props = node.get("properties")
        if node.get("type") == "object" and isinstance(props, dict):
            node["additionalProperties"] = False
            node["required"] = [
                key
                for key, sub in props.items()
                if not (
                    isinstance(sub, dict)
                    and "default" in sub
                    and sub["default"] is None
                )
            ]
        for value in node.values():
            _close(value)
    elif isinstance(node, list):
        for value in node:
            _close(value)


def build() -> dict[str, object]:
    defs: dict[str, object] = {}
    for name, wire_type in WIRE_TYPES.items():
        schema = TypeAdapter(wire_type).json_schema(ref_template=_REF)
        nested = schema.pop("$defs", {})
        for sub_name, sub in [*nested.items(), (name, schema)]:
            if defs.setdefault(sub_name, sub) != sub:
                msg = f"two different wire types are named {sub_name}"
                raise ValueError(msg)
    _close(defs)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://magent.now/api/v1.schema.json",
        "title": "magent /api/v1 wire types",
        "$defs": dict(sorted(defs.items())),
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = (
            SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        )
        if current != text:
            print(
                f"{SCHEMA_PATH.name} is stale: run uv run python scripts/gen_api_schema.py",
                file=sys.stderr,
            )
            return 1
        return 0
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
