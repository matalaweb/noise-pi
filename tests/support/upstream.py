"""Validators built from the vendored upstream OpenAPI (contract/upstream/device-api-v1.yaml)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

UPSTREAM = Path(__file__).resolve().parents[2] / "contract" / "upstream"


@lru_cache(maxsize=None)
def openapi() -> dict:
    return yaml.safe_load((UPSTREAM / "device-api-v1.yaml").read_text())


@lru_cache(maxsize=None)
def validator(component: str) -> Draft202012Validator:
    doc = openapi()
    return Draft202012Validator({"$ref": f"#/components/schemas/{component}", "components": doc["components"]})


def errors(component: str, instance) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(component).iter_errors(instance)]
