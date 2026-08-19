"""Single source of truth for every tunable value in the system.

All thresholds live in ``thresholds.toml`` at the repo root. Modules import
their constants from here instead of defining defaults, so there is exactly one
place to change a number and exactly one place to audit them -- which is also
what makes the values citable in the paper (docs/METHODS.md points at the file).

Two escape hatches, both read once at import time:

* ``BEEMON_THRESHOLDS=/path/to/other.toml`` -- use a different file entirely.
  Useful for a sensitivity sweep or a second deployment.
* ``BEEMON_<KEY>`` -- override a single value, where ``<KEY>`` is the dotted
  path uppercased with dots replaced by underscores
  (``BEEMON_STATUS_WATCH_SCORE=25``). Numeric strings are converted to match the
  type in the file, so a typo fails loudly instead of silently becoming a string.

There is deliberately no in-code default for any threshold: a missing key raises
at import rather than falling back to a value nobody wrote down.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path(__file__).resolve().parent.parent / "thresholds.toml"
ENV_FILE_VAR = "BEEMON_THRESHOLDS"
ENV_PREFIX = "BEEMON_"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(
            f"Thresholds file not found: {path}. Every tunable value lives in "
            f"thresholds.toml; set {ENV_FILE_VAR} to point at another copy."
        )
    with path.open("rb") as handle:
        return tomllib.load(handle)


THRESHOLDS_PATH = Path(os.environ.get(ENV_FILE_VAR, DEFAULT_PATH))
_VALUES = _load(THRESHOLDS_PATH)


def _env_name(path: str) -> str:
    return ENV_PREFIX + path.replace(".", "_").upper()


def _coerce(raw: str, path: str, sample: Any) -> Any:
    """Convert an environment override to the type the file uses, so an override
    cannot silently turn a threshold into a string that compares wrongly."""
    if isinstance(sample, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(sample, str):
        return raw
    try:
        # int-typed keys accept a float override (10 -> 12.5); every consumer
        # casts numerics anyway.
        return int(raw) if isinstance(sample, int) else float(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError as error:
            raise RuntimeError(
                f"Environment override {_env_name(path)}={raw!r} is not a valid "
                f"{type(sample).__name__} for threshold '{path}'."
            ) from error


def value(path: str) -> Any:
    """Look up a dotted key, e.g. ``value("quality.jumps.max_weight_jump_kg")``."""
    node: Any = _VALUES
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise RuntimeError(
                f"Missing threshold '{path}' in {THRESHOLDS_PATH}. Every tunable "
                f"value must be declared there -- add the key rather than "
                f"reintroducing a default in code."
            )
        node = node[part]
    override = os.environ.get(_env_name(path))
    if override is not None and not isinstance(node, dict):
        return _coerce(override, path, node)
    return node


def number(path: str) -> float:
    return float(value(path))


def integer(path: str) -> int:
    return int(value(path))


def text(path: str) -> str:
    return str(value(path))


def section(path: str) -> dict[str, Any]:
    node = value(path)
    if not isinstance(node, dict):
        raise RuntimeError(f"Threshold '{path}' in {THRESHOLDS_PATH} is a value, not a section.")
    # Re-read each leaf through value() so environment overrides apply to keys
    # fetched in bulk (metric weights) exactly as they do to single lookups.
    return {key: value(f"{path}.{key}") for key in node}
