"""Partial grader for T1, T2, and T3 tasks.

Computes a partial_score (0.0–1.0) and partial_stages dict by checking
independently verifiable signals.  Which signals apply depends on task_type:

  text_edit         →  4 stages (right_page, right_value, committed, db_correct)
  other_edit        →  3 stages (right_page, committed, db_correct) — right_value
                       unverifiable for dropdown/checkbox/date interactions
  create            →  no partial scoring (returns partial_score=None)
  multi_field_create → 3 stages (right_page, committed, db_weighted) — uses the
                       continuous weighted field score instead of a binary threshold,
                       giving finer diagnostic resolution on multi-field creates (T2)
  chained_workflow  →  4 stages (right_page, committed, chain_depth, db_correct) —
                       T3. `chain_depth` is the fraction of the task's graders that
                       passed (each grader = one doc in the chain), giving credit
                       for partial progress along a multi-doctype workflow.
  None/unknown      →  4 stages for backward compatibility

right_page requires current_url to be logged in turns.log — populated for
runs after this grader was added. Older runs will have right_page=False.
When start_url is absent, doctype_slug is derived from the grader endpoint
as a fallback so the new-record URL can still be matched.

right_value is only verifiable for text-input interactions; dropdown/checkbox
tasks use coordinate clicks with no typed text to compare against.

db_weighted uses the continuous weighted score from the DB grader (0.0–1.0),
making partial scores more informative when only some fields are correct.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

# Maps task_type (set in each task YAML) → the subset of stages that are
# meaningful for that interaction type.  Scoring = mean of applicable stages.
TASK_TYPE_STAGES: dict[str, list[str]] = {
    "text_edit":          ["right_page", "right_value", "committed", "db_correct"],
    "other_edit":         ["right_page", "committed", "db_correct"],
    "create":             [],  # no partial — show '—' in report
    "multi_field_create": ["right_page", "committed", "db_weighted"],  # T2
    "chained_workflow":   ["right_page", "committed", "chain_depth", "db_correct"],  # T3
}

_ALL_STAGES = ["right_page", "right_value", "committed", "db_correct"]


def _record_name_from_spec(grader_specs: list[dict]) -> str:
    """Extract the record name from the first grader endpoint.

    /api/resource/Customer/Acme Corp d238b610  →  'acme corp d238b610'
    """
    if not grader_specs:
        return ""
    endpoint = (grader_specs[0].get("endpoint") or "").strip()
    m = re.search(r"/api/resource/[^/]+/(.+)", endpoint)
    return m.group(1).lower() if m else ""


def _app_path_from_start_url(start_url: str) -> tuple[str, str]:
    """Return (full_app_path, doctype_slug) from a start_url.

    'http://erpnext-frontend:8080/app/customer'   → ('customer', 'customer')
    'http://erpnext-frontend:8080/app/crm/lead'   → ('crm/lead', 'lead')
    'http://erpnext-frontend:8080/app/stock/item' → ('stock/item', 'item')
    """
    m = re.search(r"/app/([^?#]+)", start_url or "")
    if not m:
        return "", ""
    path = m.group(1).lower().rstrip("/")
    slug = path.split("/")[-1]
    return path, slug


def _grader_doctype(grader_specs: list[dict]) -> str:
    """Target doctype slug from the first grader endpoint.

    Works for both record endpoints and filter queries:
      /api/resource/Customer/Acme Corp d2  → 'customer'
      /api/resource/Lead?filters=[...]      → 'lead'
    This is the TARGET the task must reach, independent of where it starts
    (a sidebar-nav task starts on /app/home but must reach a Lead form).
    """
    if not grader_specs:
        return ""
    endpoint = (grader_specs[0].get("endpoint") or "")
    m = re.search(r"/api/resource/([^/?&]+)", endpoint)
    return m.group(1).lower().replace(" ", "-") if m else ""


def _check_right_page(turns: list[dict], grader_specs: list[dict],
                      start_url: str) -> bool:
    # Build (doctype, record_name) targets from ALL graders, not just the first.
    # Chained (T3) tasks have several graders spanning multiple doctypes and the
    # agent navigates the whole chain (e.g. Customer -> Issue), so reaching ANY
    # graded document counts as navigation. Using only grader[0] mis-scored
    # tasks whose first grader is a non-navigable doctype (e.g. Comment, which
    # is added inline on the Issue form and has no /app/comment page).
    targets: list[tuple[str, str]] = []
    for spec in grader_specs:
        endpoint = (spec.get("endpoint") or "")
        dt_m = re.search(r"/api/resource/([^/?&]+)", endpoint)
        doctype_slug = dt_m.group(1).lower().replace(" ", "-") if dt_m else ""
        rn_m = re.search(r"/api/resource/[^/]+/(.+)", endpoint)
        record_name = rn_m.group(1).lower() if rn_m else ""
        if doctype_slug or record_name:
            targets.append((doctype_slug, record_name))

    if not targets:
        return False

    for turn in turns:
        raw_url = (turn.get("current_url") or "")
        if not raw_url:
            continue
        url = unquote(raw_url).lower()

        for doctype_slug, record_name in targets:
            # Must be in the right app section (the target doctype).
            if doctype_slug and f"/app/{doctype_slug}" not in url:
                continue

            # Change task: exact record name present in URL (spaces or hyphens).
            if record_name:
                record_name_hyphen = record_name.replace(" ", "-")
                if record_name in url or record_name_hyphen in url:
                    return True

            # Create task: new-form URL (e.g. /app/customer/new-customer-1).
            if doctype_slug and f"new-{doctype_slug}" in url:
                return True

            # Nav task (filter-query grader → no record_name): credit reaching
            # ANY form of the target doctype, i.e. the agent drilled from list
            # into a record (/app/lead/<id>), not merely the list or a workspace.
            if doctype_slug and not record_name:
                m = re.search(rf"/app/{doctype_slug}/([^/?#]+)", url)
                if m and m.group(1) not in ("view", "list", "new"):
                    return True

    return False


def _check_right_value(actions: list[dict], grader_specs: list[dict]) -> bool:
    expected = set()
    for spec in grader_specs:
        # T1 tasks use `expect: {field: value}`
        for v in (spec.get("expect") or {}).values():
            if isinstance(v, str) and v.strip():
                expected.add(v.lower().strip())
        # T2 tasks use `expect_with_weights: {field: {value: ..., weight: ...}}`
        for entry in (spec.get("expect_with_weights") or {}).values():
            v = entry.get("value", "") if isinstance(entry, dict) else ""
            if isinstance(v, str) and v.strip():
                expected.add(v.lower().strip())
    if not expected:
        return False

    for a in actions:
        action = a.get("action") or a
        atype = action.get("type", "")
        # type action (both brains)
        if atype == "type":
            if (action.get("text") or "").lower().strip() in expected:
                return True
        # select_option action (Claude only)
        if atype == "select_option":
            if (action.get("value") or "").lower().strip() in expected:
                return True

    return False


def _check_committed(actions: list[dict]) -> bool:
    for a in actions:
        if a.get("decision") != "approve":
            continue
        act = a.get("action") or a
        # A Save-button click is tagged category=commit...
        if act.get("category") == "commit":
            return True
        # ...but a keyboard save (Ctrl+S / Cmd+S) is a `key` action that is NOT
        # tagged commit, yet it DOES save the form. Count it too — otherwise
        # tasks the model saves via Ctrl+S register committed=False even on
        # success, dragging partial_score below the full score.
        if act.get("type") == "key" and str(act.get("key", "")).lower() in ("ctrl+s", "cmd+s", "command+s"):
            return True
    return False


def grade(actions: list[dict], turns: list[dict],
          grader_specs: list[dict], db_correct: bool,
          start_url: str = "", task_type: str | None = None,
          nav_assist_ok: bool = False,
          db_score: float | None = None,
          grader_results: list[dict] | None = None) -> dict:
    """Compute partial_score and partial_stages for one task run.

    Args:
        task_type:      value from the task YAML's `task_type` field.  Determines
                        which stages are applicable.  None → all 4 (back-compat).
        nav_assist_ok:  when True the harness pre-navigated the browser before
                        the agent started, so right_page is credited regardless
                        of whether the agent issued an explicit navigate action.
        db_score:       continuous weighted score from the DB grader (0.0–1.0).
                        Used as `db_weighted` for multi_field_create tasks, giving
                        finer resolution than the binary db_correct flag.
                        Defaults to None → db_weighted falls back to float(db_correct).
        grader_results: per-grader results (list of {"success": bool, ...}) from
                        the runner's dispatch loop.  Used to compute `chain_depth`
                        for chained_workflow (T3) tasks: fraction of the task's
                        graders that passed.  Defaults to None → chain_depth
                        falls back to float(db_correct).

    Returns:
        {
            "partial_score":  float | None,  # None for simple create tasks
            "partial_stages": {              # raw values for all stages
                "right_page":  bool,
                "right_value": bool,
                "committed":   bool,
                "db_correct":  bool,
                "db_weighted": float,        # continuous; equals float(db_correct)
                "chain_depth": float,        # T3: fraction of graders that passed
            },                               # for binary-graded tasks
            "applicable_stages": list[str],  # which stages were averaged
        }
    """
    right_page = _check_right_page(turns, grader_specs, start_url)
    # nav-assist delivered the agent to the START page before any turn was
    # recorded. Only credit right_page for it when that start page IS the
    # target (start doctype == grader doctype) — e.g. customer tasks start on
    # /app/customer, which is the right page. For sidebar-nav tasks that start
    # on /app/home, nav-assist landed on home, NOT the target Lead/Customer
    # form, so the agent must still navigate there itself; crediting from
    # nav_assist would be an unearned floor. In that case rely on the URL check.
    _, _start_slug = _app_path_from_start_url(start_url)
    _target_slug = _grader_doctype(grader_specs)
    if nav_assist_ok and _start_slug and _target_slug and _start_slug == _target_slug:
        right_page = True

    # db_weighted: use continuous score when available; fall back to 0.0/1.0.
    # For T1 binary tasks this equals float(db_correct) so existing scores are
    # unchanged. For T2 weighted tasks it carries the per-field accuracy.
    _db_weighted = db_score if db_score is not None else float(db_correct)

    # chain_depth: fraction of graders that passed. For T3 chained_workflow
    # tasks each grader entry checks one doc in the workflow chain, so this
    # captures "3 of 5 docs landed." Falls back to float(db_correct) so the
    # stage is well-defined for T1/T2 tasks that don't use it.
    if grader_results:
        _chain_depth = round(
            sum(1 for g in grader_results if g.get("success")) / len(grader_results),
            4,
        )
    else:
        _chain_depth = float(db_correct)

    all_stages = {
        "right_page":  right_page,
        "right_value": _check_right_value(actions, grader_specs),
        "committed":   _check_committed(actions),
        "db_correct":  db_correct,
        "db_weighted": _db_weighted,
        "chain_depth": _chain_depth,
    }

    if task_type is not None and task_type in TASK_TYPE_STAGES:
        applicable = TASK_TYPE_STAGES[task_type]
    else:
        applicable = _ALL_STAGES  # backward compat / unknown type

    if not applicable:
        # simple create tasks — no meaningful partial grading
        return {
            "partial_score":    None,
            "partial_stages":   all_stages,
            "applicable_stages": [],
        }

    partial_score = round(sum(all_stages[s] for s in applicable) / len(applicable), 4)
    # Guarantee partial_score >= full score. The proxy stages (right_page,
    # right_value, committed) exist to give diagnostic credit on tasks that did
    # NOT fully succeed. On a fully-correct task the intermediate steps
    # definitionally happened, even if a proxy failed to detect them (an unusual
    # save path, a value set via a non-`type` action, an unlogged record URL),
    # so partial must never fall below the DB score. This makes partial a true
    # lower-bounded superset of the full score.
    partial_score = max(partial_score, round(_db_weighted, 4))
    return {
        "partial_score":    partial_score,
        "partial_stages":   all_stages,
        "applicable_stages": applicable,
    }
