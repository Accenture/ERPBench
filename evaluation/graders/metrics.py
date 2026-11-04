"""Cross-run aggregator. Takes N per-run result dicts for the SAME task and
returns per-task aggregate metrics (mean, stddev, distribution).

Two-stage design:
  1. `aggregate_task(results)` — N runs of one task → one summary dict.
  2. `aggregate_suite(per_task_summaries)` — N tasks → one whole-suite dict.

The scoreboard renderer (`reports/scoreboard.py`) calls these directly to
build the Markdown table.
"""

from __future__ import annotations

import statistics
from collections import defaultdict


def _safe_mean(values):
    """Mean of a list, ignoring None. Returns None if all None or empty."""
    clean = [v for v in values if v is not None]
    return (sum(clean) / len(clean)) if clean else None


def _safe_stdev(values):
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return 0.0
    return statistics.stdev(clean)


def _safe_sum(values):
    clean = [v for v in values if v is not None]
    return sum(clean) if clean else 0.0


def aggregate_task(results: list[dict]) -> dict:
    """Aggregate N runs of one task into a summary."""
    n = len(results)
    if n == 0:
        return {"n_runs": 0}

    successes = sum(1 for r in results if r.get("success"))
    scores = [r.get("score", 0.0) for r in results]
    log_metrics_list = [r.get("log_metrics") or {} for r in results]

    def _collect(key):
        return [lm.get(key) for lm in log_metrics_list]

    actions_counts = [r.get("actions_count", 0) for r in results]
    timed_outs = sum(1 for r in results if r.get("timed_out"))
    outcomes = [r.get("outcome") for r in results]

    return {
        "task_id":        results[0].get("task_id"),
        "tier":           results[0].get("tier"),
        "brain":          results[0].get("brain"),
        "n_runs":         n,
        "n_success":      successes,
        "success_rate":   successes / n,
        "success_dist":   f"{successes}/{n}",
        "score_mean":     sum(scores) / n,
        "score_stddev":   _safe_stdev(scores),

        "n_timed_out":    timed_outs,
        "outcomes":       {o: outcomes.count(o) for o in set(outcomes) if o},

        # From log_metrics:
        "actions_mean":              _safe_mean(_collect("n_proposals")) or _safe_mean(actions_counts),
        "first_attempt_acceptance":  _safe_mean(_collect("first_attempt_acceptance")),
        "server_upgrade_rate":       _safe_mean(_collect("server_upgrade_rate")),
        "fn_irreversible_total":     int(_safe_sum(_collect("fn_irreversible_count"))),
        "cost_usd_mean":             _safe_mean(_collect("cost_usd")),
        "cost_usd_total":            _safe_sum(_collect("cost_usd")),
        "median_thinking_ms":        _safe_mean(_collect("median_thinking_ms")),
        "total_input_tokens":        int(_safe_sum(_collect("total_input_tokens"))),
        "total_output_tokens":       int(_safe_sum(_collect("total_output_tokens"))),

        # Cost per successful task — punishes flailing. None if zero successes.
        "cost_per_success":          (
            _safe_sum(_collect("cost_usd")) / successes if successes > 0 else None
        ),
    }


def aggregate_suite(task_summaries: list[dict]) -> dict:
    """Whole-suite roll-up. Means across tasks; weighted equally per task."""
    if not task_summaries:
        return {"n_tasks": 0}

    by_tier = defaultdict(list)
    for ts in task_summaries:
        by_tier[ts.get("tier", "?")].append(ts)

    overall = {
        "n_tasks":         len(task_summaries),
        "n_runs_total":    sum(ts["n_runs"] for ts in task_summaries),
        "n_success_total": sum(ts["n_success"] for ts in task_summaries),
        "success_rate":    _safe_mean([ts["success_rate"] for ts in task_summaries]),
        "score_mean":      _safe_mean([ts["score_mean"] for ts in task_summaries]),
        "fn_irreversible_total": sum(ts.get("fn_irreversible_total", 0) for ts in task_summaries),
        "cost_total_usd":  _safe_sum([ts.get("cost_usd_total") for ts in task_summaries]),
        "per_tier": {
            tier: {
                "n_tasks":      len(tasks),
                "n_runs_total": sum(t["n_runs"] for t in tasks),
                "n_success_total": sum(t["n_success"] for t in tasks),
                "success_rate": _safe_mean([t["success_rate"] for t in tasks]),
                "score_mean":   _safe_mean([t["score_mean"] for t in tasks]),
            }
            for tier, tasks in sorted(by_tier.items())
        },
    }
    return overall


def load_per_task_results(run_dir) -> dict:
    """Walk `<run_dir>/per_task/*.json` and group by task_id.

    Returns: { task_id: [result_dict, result_dict, ...] } sorted by run_n.
    """
    import json
    from pathlib import Path
    grouped: dict[str, list[dict]] = defaultdict(list)
    run_dir = Path(run_dir)
    for path in sorted((run_dir / "per_task").glob("*.json")):
        with open(path) as f:
            r = json.load(f)
        grouped[r["task_id"]].append(r)
    # Sort each task's runs by run_n.
    for tid in grouped:
        grouped[tid].sort(key=lambda r: r.get("run_n", 0))
    return dict(grouped)
