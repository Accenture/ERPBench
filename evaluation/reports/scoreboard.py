"""Generate a Markdown scoreboard from a results directory.

Usage:
  python evaluation/reports/scoreboard.py results/<run_id>/
  python evaluation/reports/scoreboard.py results/<run_id>/ > scores.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Local imports — add evaluation/ to sys.path so we can `from graders import metrics`
HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from graders import metrics    # noqa: E402


def _fmt_pct(x):
    """Format 0–1 float as 'NN%' or '—' if None."""
    if x is None:
        return "—"
    return f"{x * 100:.0f}%"


def _fmt_num(x, places=2):
    if x is None:
        return "—"
    return f"{x:.{places}f}"


def _fmt_money(x):
    if x is None:
        return "—"
    return f"${x:.4f}"


def _fmt_ms(x):
    if x is None:
        return "—"
    return f"{x:.0f}ms"


def render(run_dir: Path) -> str:
    """Render a Markdown scoreboard for one run directory."""
    grouped = metrics.load_per_task_results(run_dir)
    summaries = [metrics.aggregate_task(rs) for rs in grouped.values()]
    suite = metrics.aggregate_suite(summaries)

    # Load manifest for metadata.
    manifest_path = run_dir / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)

    lines = []
    lines.append(f"# Benchmark scoreboard — `{run_dir.name}`")
    lines.append("")
    lines.append(
        f"**Brain:** {manifest.get('brain', '?')}  ·  "
        f"**Mode:** {manifest.get('mode', '?')}  ·  "
        f"**Tasks run:** {suite.get('n_tasks', 0)}  ·  "
        f"**Total instances:** {suite.get('n_runs_total', 0)}"
    )
    lines.append("")
    lines.append(
        f"**Overall success rate:** {_fmt_pct(suite.get('success_rate'))}  ·  "
        f"**Mean score:** {_fmt_num(suite.get('score_mean'))}  ·  "
        f"**FN-irreversible (total):** {suite.get('fn_irreversible_total', 0)}  ·  "
        f"**Total cost (USD):** {_fmt_money(suite.get('cost_total_usd'))}"
    )
    lines.append("")

    # Per-tier roll-up
    per_tier = suite.get("per_tier", {})
    if per_tier:
        lines.append("## Per-tier summary")
        lines.append("")
        lines.append("| Tier | Tasks | Instances | Success rate | Mean score |")
        lines.append("|---|---|---|---|---|")
        for tier in sorted(per_tier.keys()):
            t = per_tier[tier]
            lines.append(
                f"| **{tier}** | {t['n_tasks']} | {t['n_runs_total']} | "
                f"{_fmt_pct(t['success_rate'])} | {_fmt_num(t['score_mean'])} |"
            )
        lines.append("")

    # Per-task table
    lines.append("## Per-task")
    lines.append("")
    lines.append(
        "| Task | Tier | Success | Score | Actions | First-attempt | "
        "Server-upgrade | FN-irrev | Cost (USD) | Median thinking |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    for s in sorted(summaries, key=lambda x: (x.get("tier", ""), x.get("task_id", ""))):
        lines.append(
            f"| `{s['task_id']}` | {s.get('tier', '?')} | "
            f"**{s['success_dist']}** ({_fmt_pct(s['success_rate'])}) | "
            f"{_fmt_num(s['score_mean'])} ± {_fmt_num(s['score_stddev'])} | "
            f"{_fmt_num(s.get('actions_mean'), places=1)} | "
            f"{_fmt_pct(s.get('first_attempt_acceptance'))} | "
            f"{_fmt_pct(s.get('server_upgrade_rate'))} | "
            f"{s.get('fn_irreversible_total', 0)} | "
            f"{_fmt_money(s.get('cost_per_success') or s.get('cost_usd_mean'))} | "
            f"{_fmt_ms(s.get('median_thinking_ms'))} |"
        )
    lines.append("")

    # Outcomes breakdown (if any non-completed)
    interesting_outcomes = set()
    for s in summaries:
        for o in s.get("outcomes", {}):
            if o and o not in ("completed",):
                interesting_outcomes.add(o)
    if interesting_outcomes:
        lines.append("## Non-completed outcomes")
        lines.append("")
        lines.append("| Task | " + " | ".join(sorted(interesting_outcomes)) + " |")
        lines.append("|" + "---|" * (1 + len(interesting_outcomes)))
        for s in sorted(summaries, key=lambda x: x.get("task_id", "")):
            counts = [str(s.get("outcomes", {}).get(o, 0)) for o in sorted(interesting_outcomes)]
            lines.append(f"| `{s['task_id']}` | " + " | ".join(counts) + " |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Legend:*")
    lines.append("- **Success** — runs that fully passed all graders (binary for T1; weighted for T2).")
    lines.append("- **Score** — mean ± stddev of fractional score over N runs.")
    lines.append("- **First-attempt** — % of brain proposals approved without retry.")
    lines.append("- **Server-upgrade** — % of click actions whose category the server raised.")
    lines.append("- **FN-irrev** — count of actions where brain tagged safe/commit but ground truth was irreversible.")
    lines.append("- **Cost (USD)** — cost-per-success if any succeeded, else mean cost per run.")
    lines.append("- **Median thinking** — median Claude API wall-clock per turn.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", help="path to results/<run_id>/")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 1
    if not (run_dir / "per_task").is_dir():
        print(f"error: {run_dir}/per_task not found — is this a results dir?", file=sys.stderr)
        return 1
    print(render(run_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
