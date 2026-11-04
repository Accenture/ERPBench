"""Unique-name reset strategy + ERPNext pre-seed helper.

Why unique names: instead of resetting the ERPNext DB between tasks, each run
uses a fresh per-run-id suffix on every doc name. Two runs of "create Acme
Corp" actually create "Acme Corp RUN-abc12345" and "Acme Corp RUN-def67890".
Zero state collision; no DB restore needed for dev-grade runs.

Trade-off: ERPNext DB grows over the suite. Acceptable for ~30-task slice
suites; swap in Frappe backup/restore for full-suite runs if DB size matters.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests

# Local-import shim so this module works whether run as `evaluation.reset.unique_names`
# or as `reset.unique_names` (the runner adds evaluation/ to sys.path).
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from erpnext_client import get_session, ERPNEXT_URL  # noqa: E402


def generate_run_id() -> str:
    """Compact 8-char run identifier. Stable per-run; embedded into doc names."""
    return f"RUN-{secrets.token_hex(4)}"


def substitute(value, run_id: str, run_index: int = 0) -> str:
    """Replace `{customer_name}` style placeholders inside any string-or-collection.

    Substitution rule: a placeholder of the form `{name}` is replaced by
    `<base_name> <run_id>`, where the base is taken from the placeholder
    semantics:
      - {customer_name}  → "Acme Corp <run_id>"
      - {supplier_name}  → "Acme Supplier <run_id>"
      - {item_name}      → "Acme Item <run_id>"
      - {lead_name}      → "Acme Lead <run_id>"
      - {run_id}         → just <run_id>
      - {run_index}      → str(run_index)

    Recursive over dicts and lists. Non-string leaves are returned unchanged.
    """
    # Build a short suffix unique per (task, run_index).
    # instance_run_id: "RUN-<brain>-<base8hex>-<task4hex>-<run_n>"
    # _short includes base_hex so different runner invocations never collide,
    # even if the same task + run_n is retried after a purge.
    import re as _re
    _parts = _re.findall(r'[0-9a-f]+', run_id.lower())
    if len(_parts) >= 3:
        # base[-4:] + task[-2:] + run_n — unique across runs, tasks, and retries
        _short = _parts[-3][-4:] + _parts[-2][-2:] + _parts[-1]
    elif len(_parts) >= 2:
        _short = _parts[-2] + _parts[-1]
    else:
        _short = run_id[-8:] if len(run_id) >= 8 else run_id

    bases = {
        "customer_name": f"Acme Corp {_short}",
        "supplier_name": f"Acme Supplier {_short}",
        "item_name":     f"Acme Item {_short}",
        "lead_name":     f"Acme Lead {_short}",
        "slug":          _short,
        "run_id":        run_id,
        "run_index":     str(run_index),
    }

    if isinstance(value, str):
        out = value
        for key, replacement in bases.items():
            out = out.replace("{" + key + "}", replacement)
        return out
    if isinstance(value, dict):
        return {k: substitute(v, run_id, run_index) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, run_id, run_index) for v in value]
    return value


def pre_seed(doctype: str, fields: dict, base_url: str = ERPNEXT_URL) -> dict:
    """POST a new document to ERPNext via REST.

    Returns the created doc's JSON payload. Raises on HTTP error so the
    runner can fail-fast: a missing required field or duplicate name kills
    the run before any brain time is wasted.
    """
    session = get_session(base_url)
    response = session.post(
        f"{base_url}/api/resource/{quote(doctype)}",
        json=fields,
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError(
            f"pre_seed {doctype} failed: HTTP {response.status_code} — "
            f"{response.text[:300]}"
        )
    return response.json()


def delete_doc(doctype: str, name: str, base_url: str = ERPNEXT_URL) -> None:
    """Best-effort cleanup. Silently swallows 404s (doc never existed)."""
    session = get_session(base_url)
    response = session.delete(
        f"{base_url}/api/resource/{quote(doctype)}/{quote(name)}",
        timeout=30,
    )
    if response.status_code not in (200, 202, 404):
        # Not fatal; just log via stderr-friendly mechanism in caller.
        raise RuntimeError(
            f"delete {doctype}/{name} failed: HTTP {response.status_code}"
        )


def apply_fixtures(fixture: dict, run_id: str, run_index: int = 0,
                   base_url: str = ERPNEXT_URL) -> list[dict]:
    """Run all pre_seed entries in a task's `fixture` block. Returns the
    list of seeded payloads (each with `doctype` + the created fields).

    Each pre_seed entry should have shape:
      { doctype: <str>, fields: {...} }

    Placeholders are substituted via `substitute()` against the run_id.
    """
    seeded = []
    for entry in fixture.get("pre_seed", []):
        doctype = entry["doctype"]
        raw_fields = entry.get("fields", {})
        substituted = substitute(raw_fields, run_id, run_index)
        # Many DocTypes use the name field's value as the auto-generated `name`,
        # so making sure customer_name / item_name / etc. is set is enough.
        result = pre_seed(doctype, substituted, base_url=base_url)
        seeded.append({
            "doctype": doctype,
            "fields": substituted,
            "result": result,
        })
    return seeded
