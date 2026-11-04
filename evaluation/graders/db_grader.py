"""ERPNext database grader.

Reads documents back via the Frappe REST API and compares to the task's
`expect` rubric. Supports two modes per grader entry:

  - `expect`: a flat dict of `field → value`. Binary pass/fail; all fields
    must match.
  - `expect_with_weights`: a dict of `field → {value, weight}`. Returns a
    fractional 0.0–1.0 score weighted by the field weights. Used by T2.

This module is graded-on-truthful-DB-state, not on actions. It does not
consult `actions.log` — that's `log_grader.py`'s job.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

# Local-import shim so this module works whether run as
# `evaluation.graders.db_grader` or as `graders.db_grader` (runner adds
# evaluation/ to sys.path).
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from erpnext_client import get_session, ERPNEXT_URL  # noqa: E402


def _fetch_doc(endpoint: str, base_url: str = ERPNEXT_URL,
               fetch_first_from_list: bool = False,
               max_retries: int = 3, retry_delay_s: float = 2.0) -> dict | None:
    """Fetch a Frappe doc via the REST API, retrying on transient 404 / empty-list.

    Retry-with-backoff: when the initial fetch returns None (404 or empty list),
    sleeps `retry_delay_s` and retries, up to `max_retries` total attempts.
    Handles the Frappe-eventual-consistency race condition: brain clicks Save,
    observer goes IDLE, grader queries immediately — but Frappe hasn't finished
    committing the new doc yet. The retry catches it on a later attempt.

    Default: 3 attempts, 2s between → up to ~4s wait for delayed commits."""
    for attempt in range(max_retries):
        result = _try_fetch_doc(endpoint, base_url, fetch_first_from_list)
        if result is not None:
            return result
        if attempt < max_retries - 1:
            time.sleep(retry_delay_s)
    return None


def _try_fetch_doc(endpoint: str, base_url: str = ERPNEXT_URL,
                   fetch_first_from_list: bool = False) -> dict | None:
    """One attempt at fetching a Frappe doc. Returns dict on success,
    None on missing/404. Internal helper to `_fetch_doc` (the retry wrapper).

    Default (`fetch_first_from_list=False`): GET `<endpoint>` expecting a
    single-doc response shape `{"data": {<fields>}}`. Returns that dict on
    200, None on 404, raises on other errors.

    When `fetch_first_from_list=True`: a TWO-STEP query. Used for doctypes
    whose `name` is auto-generated (Lead, Sales Invoice, etc.) where we
    can only locate the record via a unique secondary field.

      Step 1: GET the list endpoint to find matching records. We ignore
              any `fields=[...]` parameter the caller passed because
              Frappe's REST permission system gates many fields out of
              list responses (e.g. Lead.next_contact_date returns HTTP 417
              with "Field not permitted in query"). We always request
              just `["name"]` in step 1.

      Step 2: GET `/api/resource/<DocType>/<name>` for the first hit.
              This is a single-doc fetch, which returns ALL fields the
              user has permission to read — bypassing the list-query
              permission gate.

    Result: the same shape as a single-doc fetch (full field set).
    """
    session = get_session(base_url)

    if fetch_first_from_list:
        # Step 1: list query, force fields=["name"] to avoid Frappe's
        # list-permission gating. Replace any existing fields= parameter.
        list_endpoint = _override_fields_param(endpoint, '["name"]')
        list_url = f"{base_url}{list_endpoint}"
        response = session.get(list_url, timeout=30)
        if response.status_code >= 300:
            raise RuntimeError(
                f"DB list-query failed for {list_url}: "
                f"HTTP {response.status_code} — {response.text[:300]}"
            )
        body = response.json() if isinstance(response.json(), dict) else {}
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            return None
        name = data[0].get("name")
        if not name:
            return None

        # Step 2: fetch full doc by name. Doctype is the path component
        # right after /api/resource/ in the original endpoint.
        doctype = _extract_doctype_from_endpoint(endpoint)
        if doctype is None:
            return None
        doc_url = f"{base_url}/api/resource/{quote(doctype)}/{quote(name)}"
        r2 = session.get(doc_url, timeout=30)
        if r2.status_code == 404:
            return None
        if r2.status_code >= 300:
            raise RuntimeError(
                f"DB doc-fetch failed for {doc_url}: "
                f"HTTP {r2.status_code} — {r2.text[:300]}"
            )
        body2 = r2.json()
        return body2.get("data") if isinstance(body2, dict) else None

    # Single-doc path (the original behaviour).
    url = f"{base_url}{endpoint}"
    response = session.get(url, timeout=30)
    if response.status_code == 404:
        return None
    if response.status_code >= 300:
        raise RuntimeError(
            f"DB fetch failed for {url}: HTTP {response.status_code} — "
            f"{response.text[:300]}"
        )
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else None


_FIELDS_PARAM_RE = re.compile(r"([?&])fields=[^&]*")


def _override_fields_param(endpoint: str, new_fields: str) -> str:
    """Replace `fields=[...]` in a Frappe list-query URL with the given
    value. If no `fields=` parameter exists, append it. Used to force
    Step 1 of a two-step list-then-get query to request only the name."""
    if "fields=" in endpoint:
        return _FIELDS_PARAM_RE.sub(r"\1fields=" + new_fields, endpoint)
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}fields={new_fields}"


def _extract_doctype_from_endpoint(endpoint: str) -> str | None:
    """Pull `Lead` out of `/api/resource/Lead?filters=...`. Returns None
    if the endpoint doesn't match the expected /api/resource/<DocType> shape."""
    marker = "/api/resource/"
    if marker not in endpoint:
        return None
    after = endpoint.split(marker, 1)[1]
    # Stop at the next slash, ? or & (URL boundary)
    doctype = ""
    for ch in after:
        if ch in "/?&":
            break
        doctype += ch
    return doctype or None


def grade_binary(endpoint: str, expect: dict, base_url: str = ERPNEXT_URL,
                 fetch_first_from_list: bool = False) -> dict:
    """Binary pass/fail grader. All fields in `expect` must match.

    Returns:
      {
        "success": bool,
        "score":   1.0 if success else 0.0,
        "details": {
          "found":         bool,
          "field_results": {field: {"expected": ..., "actual": ..., "match": bool}},
          "reason":        str (only on failure)
        }
      }
    """
    doc = _fetch_doc(endpoint, base_url=base_url,
                    fetch_first_from_list=fetch_first_from_list)
    if doc is None:
        return {
            "success": False,
            "score":   0.0,
            "details": {
                "found":  False,
                "reason": f"document not found at {endpoint}",
                "field_results": {},
            },
        }

    field_results = {}
    all_match = True
    for field, expected in expect.items():
        actual = doc.get(field)
        match = _values_equal(actual, expected)
        field_results[field] = {
            "expected": expected,
            "actual":   actual,
            "match":    match,
        }
        if not match:
            all_match = False

    return {
        "success": all_match,
        "score":   1.0 if all_match else 0.0,
        "details": {
            "found":         True,
            "field_results": field_results,
            "reason":        None if all_match else "one or more field mismatches",
        },
    }


def grade_weighted(endpoint: str, expect_with_weights: dict,
                   base_url: str = ERPNEXT_URL,
                   fetch_first_from_list: bool = False) -> dict:
    """Partial-credit grader. Score = sum(weight × {1 if match}) / sum(weights).

    `expect_with_weights` shape per field:
      {field: {"value": expected_value, "weight": float}}
    """
    doc = _fetch_doc(endpoint, base_url=base_url,
                    fetch_first_from_list=fetch_first_from_list)
    if doc is None:
        return {
            "success": False,
            "score":   0.0,
            "details": {
                "found":  False,
                "reason": f"document not found at {endpoint}",
                "field_results": {},
            },
        }

    field_results = {}
    earned = 0.0
    total_weight = 0.0
    for field, spec in expect_with_weights.items():
        expected = spec.get("value")
        weight = float(spec.get("weight", 1.0))
        total_weight += weight
        actual = doc.get(field)
        match = _values_equal(actual, expected)
        if match:
            earned += weight
        field_results[field] = {
            "expected": expected,
            "actual":   actual,
            "weight":   weight,
            "match":    match,
        }

    score = (earned / total_weight) if total_weight > 0 else 0.0
    # By convention, "success" means >= 0.95 — gives a small margin for
    # ambiguous fields (e.g., trailing-whitespace differences in text).
    return {
        "success": score >= 0.95,
        "score":   score,
        "details": {
            "found":         True,
            "field_results": field_results,
            "earned":        earned,
            "total_weight":  total_weight,
            "reason":        None if score >= 0.95 else f"weighted score {score:.2f} below 0.95 threshold",
        },
    }


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """Strip simple HTML tags from a string. ERPNext rich-text fields wrap
    their content in <div><p>...</p></div>; the brain types the inner text,
    so the grader needs to compare the inner text, not the wrapped form."""
    return _HTML_TAG_RE.sub("", s).strip()


_CURRENCY_CHARS_RE = re.compile(r"[\s,$€£¥₹]")
_TRUTHY = {"true", "yes", "1", "y", "t", "on", "checked", "✓"}
_FALSY  = {"false", "no", "0", "n", "f", "off", "unchecked", ""}

NUMERIC_TOLERANCE = 0.01    # absolute tolerance. Loose enough to absorb
                            # ERPNext's currency formatting + floating-point
                            # rounding; tight enough to catch real errors
                            # (e.g. 99.5 vs 99.6 still mismatches).


def _to_number(value: Any) -> float | None:
    """Best-effort numeric coercion. Returns None if not parseable.

    Handles all the formatting variance the brain + ERPNext + Frappe REST
    can throw at us:
      - 99.5 (float) / 99 (int) → 99.5 / 99.0
      - "99.50" (string-numeric) → 99.5  (was a mismatch before)
      - "$99.50" / "₹ 99.50" / "1,234.56" → strips symbols + commas
      - True / False → 1.0 / 0.0
      - None / "" → None (caller falls back to other paths)
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = _CURRENCY_CHARS_RE.sub("", value)
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _to_bool(value: Any) -> bool | None:
    """Best-effort boolean coercion. Returns None if value isn't recognised
    as boolean-ish. Handles ERPNext's checkbox returning 1/0 while the
    YAML rubric might say True/False/"Yes"/"No"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # Only treat tiny integers as booleans (0/1). Otherwise this would
        # eat real numeric comparisons.
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUTHY:
            return True
        if s in _FALSY and s != "":   # "" already covered by empty check
            return False
    return None


def _values_equal(actual: Any, expected: Any) -> bool:
    """Loose equality with smart coercion.

    Match precedence (first match wins):
      1. **Empty equivalence**: both None / "" → match.
      2. **Numeric** (with coercion): both sides parse as numbers → compare
         as floats within NUMERIC_TOLERANCE (default 0.01). Handles
         "99.50" string vs 99.5 float, "$99.50" vs 99.5, etc.
      3. **Boolean-ish**: both sides are clearly boolean (True/False, 0/1,
         "Yes"/"No", "True"/"False") → compare as bool. Handles ERPNext's
         checkbox `disabled: 1` vs YAML `disabled: True`.
      4. **String** (with HTML strip + whitespace): both sides strings →
         compare after stripping HTML tags and surrounding whitespace.
      5. **Fallback**: `==`.

    Order matters: numeric is tried before boolean because we want 1 vs 1.0
    to be a numeric match, not coerced through bool. Boolean only fires
    when both sides are unambiguously boolean-ish (handles edge cases
    like Frappe's `is_group: 0` returning as int but spec says False)."""
    actual_empty = actual is None or actual == ""
    expected_empty = expected is None or expected == ""
    if actual_empty and expected_empty:
        return True
    if actual_empty or expected_empty:
        return False

    # Numeric path — handles "99.50" vs 99.5, "$99.50" vs 99.5, etc.
    a_num = _to_number(actual)
    e_num = _to_number(expected)
    if a_num is not None and e_num is not None:
        return abs(a_num - e_num) < NUMERIC_TOLERANCE

    # Boolean path — only fires when BOTH sides are unambiguously boolean
    # (avoids accidentally collapsing "Y" the abbreviation onto "Yes" the value).
    a_bool = _to_bool(actual)
    e_bool = _to_bool(expected)
    if a_bool is not None and e_bool is not None:
        return a_bool == e_bool

    # String path — HTML strip + whitespace.
    if isinstance(expected, str) and isinstance(actual, str):
        return _strip_html(actual).strip() == expected.strip()

    # Fallback.
    return actual == expected


def grade(grader_spec: dict, base_url: str = ERPNEXT_URL) -> dict:
    """Dispatch entry point used by the runner. `grader_spec` is one entry
    from the task YAML's `graders:` list, post placeholder-substitution."""
    endpoint = grader_spec["endpoint"]
    fetch_first_from_list = bool(grader_spec.get("fetch_first_from_list"))
    if "expect_with_weights" in grader_spec:
        return grade_weighted(endpoint, grader_spec["expect_with_weights"],
                              base_url=base_url,
                              fetch_first_from_list=fetch_first_from_list)
    if "expect" in grader_spec:
        return grade_binary(endpoint, grader_spec["expect"], base_url=base_url,
                            fetch_first_from_list=fetch_first_from_list)
    return {
        "success": False,
        "score":   0.0,
        "details": {"reason": "grader spec has neither 'expect' nor 'expect_with_weights'"},
    }
