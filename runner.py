"""ERPBench runner (autonomous mode).

Usage:
  python runner.py --task tasks/t1_single_field/change_industry.yaml \
                   --brain claude --runs 1
  python runner.py --suite t1 --brain claude --runs 5
  python runner.py --suite t1,t2,t3 --brain claude --runs 5

Flow per task instance:
  1. Generate a fresh run-id suffix.
  2. Substitute placeholders ({customer_name} etc.) into the task YAML.
  3. Health-check the observer server.
  4. Apply fixture pre-seeds via ERPNext REST.
  5. Note the byte offset of actions.log so we can slice this run's lines later.
  6. POST /chat with the substituted prompt.
  7. Start the auto-approver thread.
  8. Poll /status every 0.5s until phase == IDLE with a last_task_outcome.
  9. Stop the auto-approver.
 10. Slice actions.log + turns.log by the recorded offsets.
 11. Call graders (db_grader for now).
 12. Write a result JSON to results/<run_id>/per_task/<task_id>_<run_n>.json.

This runner intentionally has zero LLM logic — every brain decision is made
inside the long-lived observer-brain daemon. The runner just dispatches and
grades.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

# Local imports — evaluation/ holds all host-side benchmark code
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "evaluation"))

from auto_approver import run_auto_approver          # noqa: E402
from graders import db_grader, log_grader, partial_grader  # noqa: E402
from reset.unique_names import (                     # noqa: E402
    generate_run_id,
    substitute,
    apply_fixtures,
    delete_doc,
)

# ── Constants ──────────────────────────────────────────────────────────
SERVER_URL = os.environ.get("OBSERVER_URL", "http://localhost:5052")
ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
# In-container URL for Chromium to navigate to after each task. Uses the
# internal Docker network hostname, not localhost.
ERPNEXT_BROWSER_HOME = os.environ.get("ERPNEXT_BROWSER_HOME", "http://erpnext-frontend:8080/app/home")
# On-host paths for the log files. ./logs is bind-mounted into the container.
HOST_LOGS_DIR = HERE / "logs"
ACTIONS_LOG = HOST_LOGS_DIR / "actions.log"
TURNS_LOG   = HOST_LOGS_DIR / "turns.log"
TASKS_LOG   = HOST_LOGS_DIR / "tasks.log"

# Human-baseline runs: the annotator drives the browser, so there is nothing to
# auto-approve and the shared report must not be regenerated mid-session.
HUMAN_BRAINS = ("human",)


def _is_human_brain(label: str) -> bool:
    """Prefix match, so per-annotator labels like `human-annotator1` still work.

    Getting this wrong is a footgun: a label that fails the check would start
    the auto-approver, enforce the task limit from task creation instead of from
    the Start click (silently spending the annotator's allowance on reading),
    and skip the duration capture. No agent label begins with "human".
    """
    return (label or "").strip().lower().startswith("human")

# For human runs the runner's own wait must NOT enforce the task limit, and must
# NOT run at all until the annotator presses Start. The task is created before
# the annotator reads the prompt (or steps away for lunch, or leaves it
# overnight), so any clock that starts at task creation would silently burn the
# run while nobody is at the keyboard. Instead the runner waits indefinitely for
# Start; only AFTER Start does it arm a hang guard of timeout_seconds +
# HUMAN_IDLE_GRACE_S, as a backstop in case the panel's own auto-submit wedges.
# The panel still counts down from the Start click and auto-submits at zero.
HUMAN_IDLE_GRACE_S = 1800

POLL_INTERVAL = 0.5
RESULTS_ROOT = HERE / "results"

log = logging.getLogger("runner")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(message)s")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_delta_s(start_iso: str | None, end_iso: str | None) -> float | None:
    """Seconds between two ISO-8601 'Z' timestamps, or None if unparseable.

    Tolerates both the whole-second form the observer writes and the
    millisecond form the human-capture path writes.
    """
    try:
        a = datetime.fromisoformat((start_iso or "").replace("Z", "+00:00"))
        b = datetime.fromisoformat((end_iso or "").replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    return round((b - a).total_seconds(), 1)


# ── Task loading ───────────────────────────────────────────────────────
def load_task(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    # Basic schema sanity
    for required in ("id", "tier", "prompt", "graders"):
        if required not in spec:
            raise ValueError(f"task YAML {path} missing required field: {required}")
    return spec


def discover_suite(suite_arg: str) -> list[Path]:
    """Resolve a `--suite t1` or `--suite t1,t2` argument into a list of YAML paths."""
    tiers = [t.strip().lower() for t in suite_arg.split(",")]
    tier_dirs = {
        "t1": "t1_single_field",
        "t2": "t2_multi_field",
        "t3": "t3_multi_screen",
        "t4": "t4_long_horizon",
        "t5": "t5_trap",
    }
    paths = []
    for tier in tiers:
        if tier not in tier_dirs:
            raise ValueError(f"unknown tier: {tier}")
        d = HERE / "tasks" / tier_dirs[tier]
        if not d.is_dir():
            log.warning("tier dir does not exist: %s", d)
            continue
        for p in sorted(d.glob("*.yaml")):
            paths.append(p)
    return paths


# ── HTTP helpers ───────────────────────────────────────────────────────
def server_status() -> dict:
    return requests.get(f"{SERVER_URL}/status", timeout=5).json()


def human_started_ts() -> str | None:
    """The ISO timestamp at which the annotator pressed Start for the active
    task, or None if they have not pressed it yet. Read from /human/session,
    whose `active`/`started_ts` are set by the /human/start handler. Used so the
    runner's own wait clock begins at Start, never at task creation — an
    annotator can take an arbitrarily long break before pressing Start without
    the run being auto-recorded as timed out."""
    try:
        snap = requests.get(f"{SERVER_URL}/human/session", timeout=5).json()
    except Exception:  # noqa: BLE001 — treat an unreachable panel as "not started"
        return None
    # Require the Start to belong to the task that is active right now. This
    # rejects a stale session left behind by a previous task (e.g. if a hang
    # guard fired after Start without the panel calling /human/stop), which
    # would otherwise arm this task's guard before the annotator pressed Start.
    if not snap.get("active"):
        return None
    if snap.get("task_id") != snap.get("task_id_active"):
        return None
    return snap.get("started_ts")


def submit_chat(text: str, human_hint: str | None = None) -> dict:
    payload = {"text": text}
    # Optional per-task guidance shown ONLY in the human annotator panel (agents
    # never see it — it is not part of the prompt), sourced from the task yaml's
    # `human_hint` field. Harmless for agent runs: they don't render the panel.
    if human_hint:
        payload["human_hint"] = human_hint
    response = requests.post(f"{SERVER_URL}/chat", json=payload, timeout=10)
    response.raise_for_status()
    return response.json()


def force_stop() -> None:
    """Cleanup-on-failure hook — release the server from PROPOSED / TASK_ACTIVE."""
    try:
        requests.post(f"{SERVER_URL}/control",
                      json={"action": "stop"}, timeout=5)
    except Exception:  # noqa: BLE001
        pass


# ── Log slicing ────────────────────────────────────────────────────────
def file_offset(path: Path) -> int:
    """Return current size of a log file, 0 if it doesn't yet exist."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def slice_jsonl_by_task(path: Path, start_offset: int, task_id: str) -> list[dict]:
    """Read JSONL appended to `path` after `start_offset`, filter by task_id.

    The observer writes lines as they happen; we only consider lines added
    during this run. Filtering by task_id is belt-and-braces — runs are
    sequential, so the byte slice alone is normally sufficient.
    """
    entries = []
    if not path.exists():
        return entries
    with open(path, "rb") as f:
        f.seek(start_offset)
        raw = f.read().decode("utf-8", errors="replace")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if task_id is None or entry.get("task_id") == task_id:
            entries.append(entry)
    return entries


# ── Per-run task execution ────────────────────────────────────────────
def run_one_task(task_spec: dict, run_id: str, run_n: int,
                 results_root: Path, timeout_scale: float = 1.0) -> dict:
    """Execute one instance of one task. Returns the result dict (also persisted to disk)."""
    task_id = task_spec["id"]
    run_dir = results_root / "per_task"
    run_dir.mkdir(parents=True, exist_ok=True)
    decisions_log = run_dir / f"{task_id}_{run_n}.decisions.jsonl"

    # Build a unique-name suffix that's stable per (task, run_n) within a sweep
    # AND distinct ACROSS tasks. Without the task hash, `--suite t1 --runs 5`
    # collides because every task uses the same suffix set (RUN-xxx-0..4) and
    # each later task's pre-seed conflicts with the earlier task's customers.
    task_hash = hashlib.md5(task_id.encode(), usedforsecurity=False).hexdigest()[:4]
    instance_run_id = f"{run_id}-{task_hash}-{run_n}"
    substituted = substitute(task_spec, instance_run_id, run_index=run_n)
    timeout_seconds = int(substituted.get("timeout_seconds", 300) * timeout_scale)
    prompt = substituted["prompt"]

    result: dict = {
        "task_id":         task_id,
        "tier":            task_spec.get("tier"),
        "brain":           os.environ.get("BENCH_BRAIN_LABEL", "claude"),
        "run_n":           run_n,
        "run_id":          instance_run_id,
        "prompt":          prompt,
        "started_ts":      _now_iso(),
        "fixture":         {"seeded": []},
        "outcome":         None,
        "timed_out":       False,
        "success":         False,
        "score":           0.0,
        "grader_results":  [],
        "actions_count":   0,
        "turns_count":     0,
    }

    # ── 1. Health-check ────────────────────────────────────────────
    try:
        status = server_status()
    except Exception as exc:
        result["outcome"] = "server_unreachable"
        result["error"]   = str(exc)
        _persist(run_dir, task_id, run_n, result)
        return result
    if status.get("phase") != "IDLE":
        force_stop()
        time.sleep(1.0)
        try:
            status = server_status()
        except Exception as exc:
            result["outcome"] = "server_unreachable_after_stop"
            result["error"]   = str(exc)
            _persist(run_dir, task_id, run_n, result)
            return result
        if status.get("phase") != "IDLE":
            result["outcome"] = "server_busy"
            result["error"]   = f"server phase={status.get('phase')!r} before task start"
            _persist(run_dir, task_id, run_n, result)
            return result

    # ── 2. Apply fixtures ─────────────────────────────────────────
    fixture = substituted.get("fixture", {}) or {}
    try:
        result["fixture"]["seeded"] = apply_fixtures(
            fixture, run_id=instance_run_id, run_index=run_n,
            base_url=ERPNEXT_URL,
        )
    except Exception as exc:
        result["outcome"] = "fixture_failed"
        result["error"]   = str(exc)
        _persist(run_dir, task_id, run_n, result)
        return result

    # ── 3. Pre-navigate browser if task declares a start_url ─────
    start_url = substituted.get("start_url", "").strip()
    nav_assist_ok: bool | None = None  # None = not applicable (no start_url)

    def _try_navigate(url: str, retries: int = 6, retry_delay: float = 10.0) -> bool:
        """POST to /navigate, retrying up to `retries` times before giving up."""
        for attempt in range(retries):
            try:
                r = requests.post(f"{SERVER_URL}/navigate", json={"url": url}, timeout=40)
                if r.ok:
                    return True
            except Exception:  # noqa: BLE001
                pass
            if attempt < retries - 1:
                log.warning("nav-assist: attempt %d/%d failed — waiting %.0fs before retry",
                            attempt + 1, retries, retry_delay)
                time.sleep(retry_delay)
        return False

    def _restart_observer() -> bool:
        """Restart the observer Docker container from the host. Returns True if it came back."""
        compose_file = HERE / "docker-compose.yml"
        log.warning("nav-assist: restarting observer container to recover Chrome")
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "restart", "observer"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Poll /status until observer is back (up to 60 s)
        for _ in range(60):
            time.sleep(1.0)
            try:
                r = requests.get(f"{SERVER_URL}/status", timeout=3)
                if r.ok:
                    log.info("nav-assist: observer is back up")
                    time.sleep(5.0)  # let Chrome fully initialise CDP after restart
                    return True
            except Exception:  # noqa: BLE001
                pass
        log.error("nav-assist: observer did not come back within 60 s")
        return False

    def _session_check(url: str) -> None:
        """After nav-assist, verify browser isn't on the login page; re-auth if it is."""
        try:
            r = requests.post(
                f"{SERVER_URL}/session_check", json={"url": url}, timeout=30
            )
            data = r.json()
            if data.get("reauthed"):
                log.warning(
                    "session_check: browser was on login page — re-authenticated and "
                    "re-navigated to %s", url
                )
            elif data.get("logged_in") is False:
                log.error(
                    "session_check: agent browser is on the ERPNext login page and "
                    "re-login failed (%s). Check ERPNEXT_BROWSER_USR / "
                    "ERPNEXT_BROWSER_PWD in .env.", data.get("error")
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("session_check skipped (%s)", exc)

    # skip_nav_assist=True (T3 OSWorld-analog variants): the harness does
    # NOT hand-deliver the ERPNext page. Instead we reset the browser to
    # about:blank and the agent must type the URL themselves. nav_assist_ok
    # is forced False so partial_grader doesn't credit right_page from the
    # "harness landed you on the target" floor — the agent has to earn it.
    #
    # HUMANS ARE EXEMPT: self-navigation from a blank page is an agent-only
    # test condition. For a human annotator a white about:blank page with
    # "use the navigate_url action" instructions is unusable, and the human
    # baseline measures the workflow itself, not self-navigation. So for humans
    # we fall through to normal nav-assist (pre-navigate to start_url). This is
    # gated to humans only and leaves every agent run unchanged.
    skip_nav = bool(substituted.get("skip_nav_assist")) and not _is_human_brain(
        os.environ.get("BENCH_BRAIN_LABEL", ""))
    if skip_nav:
        # Pre-warm the ERPNext session before resetting to about:blank.
        # Without this the browser cookie may be expired: the agent navigates
        # to ERPNext and immediately hits the login page instead of the target.
        # We navigate to the ERPNext home page first (to refresh the cookie /
        # re-authenticate if needed), then reset to about:blank so the agent
        # starts from the OSWorld-analog blank-page state with a live session.
        home_url = ERPNEXT_BROWSER_HOME
        try:
            r = requests.post(f"{SERVER_URL}/navigate",
                              json={"url": home_url}, timeout=60)
            log.info("nav-assist: pre-warm: navigated to %s to refresh session", home_url)
            _session_check(home_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("nav-assist: pre-warm failed (%s) — continuing anyway", exc)
        # Now reset to about:blank for the OSWorld-analog start.
        # Widen timeout to 60s: the observer's /navigate handler always runs
        # clear_list_filters after the load, which hangs against about:blank
        # (no Frappe UI to clear). The 15s original timeout was too tight.
        try:
            requests.post(f"{SERVER_URL}/navigate",
                          json={"url": "about:blank"}, timeout=60)
            log.info("nav-assist: skipped (skip_nav_assist=true); "
                     "browser reset to about:blank")
        except Exception as exc:  # noqa: BLE001
            log.warning("nav-assist: skip_nav_assist blank-reset failed: %s "
                        "— agent will start from browser's current state", exc)
        nav_assist_ok = False
    elif start_url:
        if _try_navigate(start_url):
            nav_assist_ok = True
            log.info("nav-assist: navigated to %s", start_url)
            _session_check(start_url)
        else:
            log.warning("nav-assist: retries exhausted — restarting observer")
            if _restart_observer() and _try_navigate(start_url):
                nav_assist_ok = True
                log.info("nav-assist: navigated to %s (after restart)", start_url)
                _session_check(start_url)
            else:
                log.error("nav-assist: failed after observer restart — aborting run")
                result["outcome"] = "nav_assist_failed"
                result["error"]   = "nav-assist could not pre-navigate to start_url after observer restart"
                result["nav_assist_ok"] = False
                _persist(run_dir, task_id, run_n, result)
                _teardown_task(substituted, task_id, run_n)
                return result

    result["nav_assist_ok"] = nav_assist_ok

    # ── 4. Note log offsets for slicing ───────────────────────────
    actions_offset = file_offset(ACTIONS_LOG)
    turns_offset   = file_offset(TURNS_LOG)
    tasks_offset   = file_offset(TASKS_LOG)

    # ── 4. Start auto-approver ────────────────────────────────────
    # Skipped for human runs: a human clicks the real UI directly, so no action
    # is ever proposed and there is nothing for the approver to decide.
    is_human = _is_human_brain(os.environ.get("BENCH_BRAIN_LABEL", ""))
    stop_event = threading.Event()
    approver = threading.Thread(
        target=run_auto_approver,
        args=(SERVER_URL, stop_event, str(decisions_log)),
        daemon=True,
    )
    if not is_human:
        approver.start()

    # ── 5. Submit task ────────────────────────────────────────────
    server_task_id = None
    if is_human:
        # Tell the panel this task's real limit before the annotator sees it.
        try:
            requests.post(f"{SERVER_URL}/human/limit",
                          json={"seconds": int(timeout_seconds)}, timeout=10)
        except Exception as exc:  # noqa: BLE001 — panel falls back to its default
            log.warning("could not push human task limit (%s): %s", timeout_seconds, exc)
    try:
        # Humans see a plain-language, human-authored instruction (`human_prompt`)
        # instead of the agent prompt, which is written for a model (mentions the
        # harness, blank-page starts, the navigate_url action, etc.) and is
        # unusable by a person. Agents always get the original `prompt`; the
        # human_prompt field is ignored for them, so agent runs are unchanged.
        display_text = (
            substituted.get("human_prompt") if (is_human and substituted.get("human_prompt"))
            else prompt
        )
        submit_chat(display_text, human_hint=substituted.get("human_hint") if is_human else None)
    except Exception as exc:
        stop_event.set()
        result["outcome"] = "chat_failed"
        result["error"]   = str(exc)
        _persist(run_dir, task_id, run_n, result)
        _teardown_task(substituted, task_id, run_n)
        return result

    # ── 6. Wait for completion ────────────────────────────────────
    # Agents: the limit starts now, because the brain starts now, so the
    # deadline is armed immediately.
    # Humans: the runner waits indefinitely (deadline=None) until the annotator
    # presses Start. This lets them take an arbitrarily long break — lunch, or
    # overnight — before starting a task without the run being auto-recorded as
    # timed out. Only once Start is detected is the hang guard armed, counting
    # timeout_seconds + HUMAN_IDLE_GRACE_S from the Start press. The panel
    # enforces the real per-task limit and auto-submits at zero.
    deadline: float | None = None if is_human else time.time() + timeout_seconds
    last_phase = None
    while deadline is None or time.time() < deadline:
        if is_human and deadline is None and human_started_ts() is not None:
            deadline = time.time() + timeout_seconds + HUMAN_IDLE_GRACE_S
            log.info("human pressed Start — arming hang guard (%ss)",
                     timeout_seconds + HUMAN_IDLE_GRACE_S)
        try:
            status = server_status()
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        phase = status.get("phase")
        if phase != last_phase:
            log.info("phase: %s → %s", last_phase, phase)
            last_phase = phase
        # Capture the server's task_id as soon as we see one
        if server_task_id is None:
            ct = status.get("current_task")
            if isinstance(ct, dict) and ct.get("task_id"):
                server_task_id = ct["task_id"]
        if phase == "IDLE":
            outcome_card = status.get("last_task_outcome")
            if outcome_card is not None:
                result["outcome"] = outcome_card.get("outcome", "completed")
                result["outcome_card"] = outcome_card

                # Distinguish "gave up at turn cap" from real completion.
                # Both Claude (run_task) and Qwen (run_task) call
                # /complete_task with a "turn cap reached" summary when
                # they hit MAX_TURNS_PER_TASK. The server records that
                # as outcome="completed" — indistinguishable from real
                # success without this check. We override based on the
                # brain's own admission in the summary text.
                summary_text = (outcome_card.get("summary") or "").lower()
                if "turn cap" in summary_text or "max_turns" in summary_text:
                    result["outcome"] = "max_turns_hit"

                break
        time.sleep(POLL_INTERVAL)
    else:
        # timeout fell through
        result["timed_out"] = True
        result["outcome"]   = "timed_out"
        force_stop()
        time.sleep(1.0)

    # ── 7. Stop the auto-approver ────────────────────────────────
    stop_event.set()
    if approver.is_alive():          # never started for human runs
        approver.join(timeout=3.0)

    # ── 7b. Reset browser to home ─────────────────────────────────
    # Ensures UI state is clean before the next task regardless of brain.
    # Phase is IDLE here (either normal completion or force_stop above).
    try:
        requests.post(
            f"{SERVER_URL}/navigate", json={"url": ERPNEXT_BROWSER_HOME}, timeout=40
        )
        log.info("post-task: reset browser to %s", ERPNEXT_BROWSER_HOME)
    except Exception as exc:  # noqa: BLE001
        log.warning("post-task: /navigate home failed (%s) — continuing anyway", exc)
    time.sleep(3.0)   # let Chromium settle so next task's nav-assist CDP call doesn't time out

    # ── 8. Slice the logs ─────────────────────────────────────────
    actions_entries = slice_jsonl_by_task(ACTIONS_LOG, actions_offset, server_task_id)
    turns_entries   = slice_jsonl_by_task(TURNS_LOG,   turns_offset,   server_task_id)
    result["actions_count"] = len(actions_entries)
    result["turns_count"]   = len(turns_entries)
    result["server_task_id"] = server_task_id
    result["ended_ts"]      = _now_iso()

    # Human runs: the reportable duration is from the annotator pressing Start
    # to the task completing, NOT from when this runner created the task. The
    # gap between the two is reading time, which the agents never spend.
    if is_human:
        tasks_entries = slice_jsonl_by_task(TASKS_LOG, tasks_offset, server_task_id)
        human_start = next((e.get("ts") for e in tasks_entries
                            if e.get("event") == "human_start"), None)
        human_end = next((e.get("ts") for e in reversed(tasks_entries)
                          if e.get("event") in ("completed", "human_stop")), None)
        result["human_start_ts"] = human_start
        result["human_duration_s"] = (
            _iso_delta_s(human_start, human_end) if (human_start and human_end) else None
        )
        result["captured_events"] = next(
            (e.get("captured_events") for e in reversed(tasks_entries)
             if e.get("event") == "human_stop"), None
        )
        if result["human_duration_s"] is None:
            log.warning("human run %s/%s: no Start marker — did the annotator press Start?",
                        task_id, run_n)

    # Persist raw log slices alongside the result for downstream graders.
    raw_logs_dir = results_root / "raw_logs"
    raw_logs_dir.mkdir(parents=True, exist_ok=True)
    with open(raw_logs_dir / f"{task_id}_{run_n}.actions.jsonl", "w", encoding="utf-8") as f:
        for e in actions_entries:
            f.write(json.dumps(e) + "\n")
    with open(raw_logs_dir / f"{task_id}_{run_n}.turns.jsonl", "w", encoding="utf-8") as f:
        for e in turns_entries:
            f.write(json.dumps(e) + "\n")

    # ── 9. Grade ──────────────────────────────────────────────────
    grader_results = []
    for spec in substituted.get("graders", []):
        kind = spec.get("kind", "db")
        if kind == "db":
            grader_results.append(db_grader.grade(spec, base_url=ERPNEXT_URL))
        else:
            grader_results.append({
                "success": False, "score": 0.0,
                "details": {"reason": f"unknown grader kind: {kind}"},
            })
    result["grader_results"] = grader_results
    if grader_results:
        # Aggregate: success iff all sub-graders pass; score = mean.
        result["success"] = all(g.get("success") for g in grader_results)
        result["score"]   = sum(g.get("score", 0.0) for g in grader_results) / len(grader_results)

    # ── 10. Action-log analysis (independent of DB grader) ────────
    result["log_metrics"] = log_grader.analyze(
        actions_entries, turns_entries, brain=result["brain"],
        max_actions=substituted.get("max_actions"),
    )

    pg = partial_grader.grade(
        actions=actions_entries,
        turns=turns_entries,
        grader_specs=substituted.get("graders", []),
        db_correct=result["success"],
        start_url=substituted.get("start_url", ""),
        task_type=substituted.get("task_type"),
        nav_assist_ok=result.get("nav_assist_ok", False),
        db_score=result.get("score"),
        grader_results=grader_results,
    )
    result["task_type"]        = substituted.get("task_type")
    result["partial_score"]    = pg["partial_score"]
    result["partial_stages"]   = pg["partial_stages"]
    result["applicable_stages"] = pg["applicable_stages"]

    _persist(run_dir, task_id, run_n, result)

    # ── 11. Per-run teardown ──────────────────────────────────────
    # Delete the records this run seeded/created so the next task starts
    # with a clean ERPNext state. Grading has already happened (via the
    # DB grader above), and the result JSON is on disk — safe to delete.
    # Best-effort: errors are logged and swallowed.
    _teardown_task(substituted, task_id, run_n)

    return result


def _teardown_task(substituted_spec: dict, task_id: str, run_n: int) -> None:
    """Delete the ERPNext records created for one task-run so subsequent
    tasks don't see accumulating clutter in list views.

    Sources of records to delete:
      1. `fixture.pre_seed` entries — these were created by apply_fixtures().
      2. `graders[*].endpoint` (kind=db) — for create-type tasks, the
         grader endpoint identifies the target record Qwen was supposed
         to create.

    Silent on 404 (unique_names.delete_doc already handles that). Other
    errors log a warning and continue — teardown never fails the run.
    """
    import re as _re
    import urllib.parse as _urlparse

    fixture = substituted_spec.get("fixture", {}) or {}
    deleted: set[tuple[str, str]] = set()

    # (1) Pre-seed records.
    for entry in fixture.get("pre_seed", []) or []:
        doctype = entry.get("doctype")
        fields = entry.get("fields", {}) or {}
        # Frappe autoNames from the primary field of most Doctypes; try
        # the common ones in order.
        name = (
            fields.get("name")
            or fields.get("customer_name")
            or fields.get("item_name")
            or fields.get("lead_name")
            or fields.get("supplier_name")
            or fields.get("company_name")
            or fields.get("item_code")
        )
        if doctype and name:
            key = (doctype, str(name))
            if key not in deleted:
                try:
                    delete_doc(doctype, str(name))
                    log.info("teardown: deleted %s/%s (pre_seed)", doctype, name)
                except Exception as exc:
                    log.warning("teardown pre_seed delete %s/%s failed: %s",
                                doctype, name, exc)
                deleted.add(key)

    # (2) Create-task target records — infer from grader endpoints.
    for spec in substituted_spec.get("graders", []) or []:
        if spec.get("kind") != "db":
            continue
        endpoint = spec.get("endpoint", "") or ""
        # Match "/api/resource/{Doctype}/{Name}" (with optional query string).
        m = _re.match(r"^/api/resource/([^/?]+)/([^?/]+)", endpoint)
        if m:
            doctype = _urlparse.unquote(m.group(1))
            name = _urlparse.unquote(m.group(2))
        elif spec.get("fetch_first_from_list"):
            # T3 chained-workflow / auto-named docs: endpoint is a filter query
            # (e.g. /api/resource/Opportunity?filters=[["customer_name","=","..."]]).
            # Resolve the actual name via the same list-then-get path the grader
            # uses, then delete by that name.
            try:
                doc = db_grader._fetch_doc(
                    endpoint, base_url=ERPNEXT_URL, fetch_first_from_list=True,
                )
            except Exception as exc:
                log.debug("teardown filter-query lookup %s: %s", endpoint, exc)
                continue
            if not doc or not doc.get("name"):
                continue  # no matching doc — nothing to clean up
            doctype = db_grader._extract_doctype_from_endpoint(endpoint) or ""
            name = doc["name"]
            if not doctype:
                continue
        else:
            continue
        key = (doctype, name)
        if key in deleted:
            continue
        try:
            delete_doc(doctype, name)
            log.info("teardown: deleted %s/%s (grader endpoint)", doctype, name)
        except Exception as exc:
            # Frappe refuses to DELETE submitted docs (docstatus=1). Cancel
            # first, then re-DELETE. Best-effort — a raised exception here is
            # not fatal.
            msg = str(exc).lower()
            if "cannot delete" in msg or "submitted" in msg or "linked" in msg:
                try:
                    _cancel_doc(doctype, name)
                    delete_doc(doctype, name)
                    log.info("teardown: cancelled+deleted %s/%s", doctype, name)
                except Exception as exc2:
                    log.warning("teardown cancel+delete %s/%s failed: %s",
                                doctype, name, exc2)
            else:
                log.debug("teardown grader delete %s/%s: %s", doctype, name, exc)
        deleted.add(key)


def _cancel_doc(doctype: str, name: str) -> None:
    """POST /api/method/frappe.client.cancel to move a submitted doc
    (docstatus=1) to cancelled (docstatus=2), so a subsequent DELETE is
    allowed. Raises on non-2xx."""
    from erpnext_client import get_session
    session = get_session(ERPNEXT_URL)
    response = session.post(
        f"{ERPNEXT_URL}/api/method/frappe.client.cancel",
        data={"doctype": doctype, "name": name},
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError(
            f"cancel {doctype}/{name} failed: HTTP {response.status_code}"
        )


def _persist(run_dir: Path, task_id: str, run_n: int, result: dict) -> None:
    out_path = run_dir / f"{task_id}_{run_n}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="path to a single task YAML (relative to this folder)")
    parser.add_argument("--suite", help="comma-separated tiers, e.g. t1 or t1,t2")
    parser.add_argument("--brain", default="claude",
                        help="which brain is configured at the server side (label-only, no spawn)")
    parser.add_argument("--mode", default="autonomous", choices=["autonomous"],
                        help="autonomous-only for this slice")
    parser.add_argument("--runs", type=int, default=1,
                        help="number of repeated runs per task")
    parser.add_argument("--timeout-scale", type=float, default=1.0, dest="timeout_scale",
                        help="multiply every task's timeout_seconds by this factor "
                             "(e.g. 5 for UI-TARS which runs ~5x slower than Claude)")
    parser.add_argument("--start-from", default=None, dest="start_from",
                        help="skip all tasks that come before this task ID alphabetically "
                             "(e.g. --start-from change_website resumes from task 13)")
    parser.add_argument("--run-id", default=None, dest="run_id_override",
                        help="reuse an existing RUN-* directory instead of creating a new one "
                             "(use with --start-from to append results to a partial run)")
    args = parser.parse_args()

    if not args.task and not args.suite:
        parser.error("must provide --task or --suite")

    if args.task:
        task_paths = [HERE / args.task] if not Path(args.task).is_absolute() else [Path(args.task)]
    else:
        task_paths = discover_suite(args.suite)

    if args.start_from:
        task_paths = [p for p in task_paths if p.stem >= args.start_from]
        if not task_paths:
            log.error("--start-from %r matched no tasks", args.start_from)
            return 1
        log.info("--start-from %r: running %d tasks starting at %s",
                 args.start_from, len(task_paths), task_paths[0].stem)

    if not task_paths:
        log.error("no tasks resolved")
        return 1

    # Tag this run. Prefix with brain name so results/ dirs are
    # self-identifying: RUN-claude-xxxxxxxx, RUN-uitars-xxxxxxxx, etc.
    if args.run_id_override:
        run_id = args.run_id_override
    else:
        run_id = generate_run_id().replace("RUN-", f"RUN-{args.brain}-", 1)
    results_root = RESULTS_ROOT / run_id
    results_root.mkdir(parents=True, exist_ok=True)
    os.environ["BENCH_BRAIN_LABEL"] = args.brain

    # Quick sanity-check that the server is reachable before doing anything else.
    try:
        s = server_status()
    except Exception as exc:
        log.error("server unreachable at %s: %r", SERVER_URL, exc)
        return 2
    log.info("observer status: phase=%s mode=%s brain_provider=%s",
             s.get("phase"), s.get("mode"), s.get("brain_provider"))

    log.info("starting bench run_id=%s brain=%s tasks=%d runs/task=%d",
             run_id, args.brain, len(task_paths), args.runs)

    all_results = []
    for task_path in task_paths:
        try:
            spec = load_task(task_path)
        except Exception as exc:
            log.error("failed to load %s: %r", task_path, exc)
            continue
        # Outcomes that are harness failures (not brain failures) — retry these.
        _HARNESS_FAILURES = {"nav_assist_failed", "server_unreachable",
                             "server_unreachable_after_stop", "server_busy", "crash"}
        for run_n in range(args.runs):
            for _attempt in range(4):  # up to 3 retries
                log.info("running %s (run %d/%d)%s", spec["id"], run_n + 1, args.runs,
                         f" [retry {_attempt}]" if _attempt else "")
                try:
                    r = run_one_task(spec, run_id, run_n, results_root, args.timeout_scale)
                except Exception as exc:  # noqa: BLE001
                    log.exception("task crashed: %s run=%d: %r", spec["id"], run_n, exc)
                    force_stop()
                    r = {"task_id": spec["id"], "run_n": run_n,
                         "success": False, "score": 0.0,
                         "outcome": "crash", "error": str(exc)}
                if r.get("outcome") not in _HARNESS_FAILURES:
                    break
                log.warning("harness failure (%s) on %s run %d — retrying",
                            r.get("outcome"), spec["id"], run_n)
            all_results.append(r)
            log.info("  → success=%s score=%.2f outcome=%s actions=%d",
                     r.get("success"), r.get("score", 0.0),
                     r.get("outcome"), r.get("actions_count", 0))
            # Optional: regenerate a report after each task (REPORT_SCRIPT=path).
            report_script = os.environ.get("REPORT_SCRIPT")
            if report_script and Path(report_script).is_file() and args.brain != "qwen":
                try:
                    subprocess.run(
                        [sys.executable, report_script],
                        check=False, timeout=30,
                    )
                except Exception:
                    pass

    # Write the manifest.
    manifest = {
        "run_id":   run_id,
        "brain":    args.brain,
        "mode":     args.mode,
        "tasks":    [str(p.relative_to(HERE)) for p in task_paths],
        "runs":     args.runs,
        "started_ts": _now_iso(),
        "n_runs":   len(all_results),
        "n_success": sum(1 for r in all_results if r.get("success")),
    }
    with open(results_root / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    log.info("DONE — results at %s (%d/%d succeeded)",
             results_root, manifest["n_success"], manifest["n_runs"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        force_stop()
        sys.exit(130)
