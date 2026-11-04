"""ERPBench evaluation runtime — executes agent actions and writes the audit logs.

The runner gives a task statement. The brain proposes one action at a time;
the auto-approver (evaluation/auto_approver.py) approves each proposal, and the
server executes it via inspector.py.

State machine (single threading.Lock protects everything in `_state`):

    IDLE
      └─ POST /chat (task) ──► TASK_ACTIVE
    TASK_ACTIVE
      ├─ POST /propose_action ──► PROPOSED
      ├─ POST /ask_human       ──► WAITING_HUMAN
      └─ POST /complete_task   ──► IDLE
    PROPOSED
      ├─ POST /decide approve ──► EXECUTING ──► TASK_ACTIVE
      └─ POST /control stop   ──► STOPPED ──► IDLE
    WAITING_HUMAN
      └─ POST /chat            ──► TASK_ACTIVE
    (any) POST /control stop  ──► STOPPED ──► IDLE

Audit log files (append-only JSONL in /app/logs/):
  - tasks.log         task lifecycle events
  - actions.log       every proposal + decision + execution
  - chats.log         every chat turn
  - observations.log  brain's final summaries
  - turns.log         per-LLM-call telemetry
"""

import hashlib
import json
import logging
import os
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import Flask, jsonify, render_template, request

import inspector
import human_capture


app = Flask(__name__)
log = logging.getLogger("observer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ── Logs ────────────────────────────────────────────────────────────
TASKS_LOG = "/app/logs/tasks.log"
ACTIONS_LOG = "/app/logs/actions.log"
CHATS_LOG = "/app/logs/chats.log"
OBSERVATIONS_LOG = "/app/logs/observations.log"
# Per-LLM-call audit: token counts + api-call wall-clock. Written by brains
# via POST /log_turn. Used by the benchmark harness for cost/latency metrics.
TURNS_LOG = "/app/logs/turns.log"

# ── Constants ───────────────────────────────────────────────────────
CHAT_LOG_CAP = 200
ACTION_LOG_CAP = 100
OBS_CAP = 50

# Hard cap on actions per task (prevents runaway loops).
DEFAULT_TASK_ACTION_CEILING = 30

# Phases — the explicit values are part of the server's JSON contract.
PHASE_IDLE = "IDLE"
PHASE_TASK_ACTIVE = "TASK_ACTIVE"
PHASE_PROPOSED = "PROPOSED"
PHASE_EXECUTING = "EXECUTING"
PHASE_WAITING_HUMAN = "WAITING_HUMAN"
PHASE_STOPPED = "STOPPED"

MODE_DO = "do"


_lock = threading.Lock()
_state = {
    "phase": PHASE_IDLE,
    "current_task": None,              # {task_id, ts, text, step_count, plan, plan_step}
    "pending_action": None,            # the action dict awaiting decision
    "pending_action_token": None,      # uuid bound to the action for log correlation
    "pending_action_screenshot_hash": None,  # hash of screenshot at propose time
    "claimed": False,                  # brain has picked up the current task
    "last_task_outcome": None,         # snapshot of the most recently completed task
    "chat_log": deque(maxlen=CHAT_LOG_CAP),
    "action_log": deque(maxlen=ACTION_LOG_CAP),
    "observations": deque(maxlen=OBS_CAP),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_diff_s(start_iso: str, end_iso: str) -> int:
    """Return whole seconds between two ISO-8601 'Z' timestamps. 0 on parse error."""
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return max(0, int((e - s).total_seconds()))
    except Exception:
        return 0


def _hash_b64(b64: str) -> str:
    if not b64:
        return ""
    return hashlib.sha256(b64.encode("utf-8")).hexdigest()[:16]


def _append_jsonl(path: str, entry: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("failed to append to %s: %s", path, exc)


def _new_task_id() -> str:
    return str(uuid.uuid4())


# ── Routes ──────────────────────────────────────────────────────────
@app.get("/")
def index():
    return jsonify({"service": "erpbench-observer", "phase": _state["phase"]})


@app.get("/status")
def status():
    # mode / paused / pending_ask / consecutive_rejections are fixed values kept
    # so the brains and runner read the same response shape as the paper runs.
    with _lock:
        return jsonify({
            "phase": _state["phase"],
            "mode": MODE_DO,
            "paused": False,
            "current_task": _state["current_task"],
            "pending_action": _state["pending_action"],
            "pending_action_token": _state["pending_action_token"],
            "pending_ask": None,
            "last_task_outcome": _state["last_task_outcome"],
            "chat_log": list(_state["chat_log"]),
            "action_log": list(_state["action_log"]),
            "observations": list(_state["observations"]),
            "consecutive_rejections": 0,
        })


@app.post("/chat")
def chat():
    """Runner submits a task, or a reply while the brain waits on ask_human.

      - phase=IDLE → start a new task (transition to TASK_ACTIVE)
      - phase=WAITING_HUMAN → reply clears the wait (back to TASK_ACTIVE)
      - phase in {TASK_ACTIVE, PROPOSED} → logged as a mid-task clarification
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "missing 'text' field"}), 400
    # Optional per-task guidance shown only in the human annotator panel.
    # Agents never receive this — it is not appended to the prompt/text.
    human_hint = (body.get("human_hint") or "").strip()

    ts = _now_iso()

    with _lock:
        phase = _state["phase"]

        if phase == PHASE_STOPPED:
            return jsonify({"error": "current task is stopped — wait for cleanup"}), 409

        if phase == PHASE_IDLE:
            task_id = _new_task_id()
            _state["current_task"] = {
                "task_id": task_id,
                "ts": ts,
                "text": text,
                "human_hint": human_hint,   # panel-only; empty for agent runs
                "step_count": 0,
                "plan": None,          # {summary, steps:[...]} once the brain calls /announce_plan
                "plan_step": 0,        # 1-based index of the milestone currently in progress
            }
            _state["phase"] = PHASE_TASK_ACTIVE
            _state["claimed"] = False
            _state["last_task_outcome"] = None
            # Scope per-turn context to THIS task. action_log / chat_log /
            # observations are deques that otherwise accumulate across every
            # task in a run; the brain feeds the last N entries to the model as
            # "your memory of every step", so without this reset a task inherits
            # the previous tasks' clicks, contaminating the model's reasoning.
            _state["action_log"].clear()
            _state["chat_log"].clear()
            _state["observations"].clear()
            _append_jsonl(TASKS_LOG, {
                "ts": ts, "task_id": task_id, "event": "started", "text": text,
            })
            log.info("task started: id=%s text=%r", task_id, text)
        elif phase == PHASE_WAITING_HUMAN:
            _state["phase"] = PHASE_TASK_ACTIVE
            log.info("WAITING_HUMAN cleared by chat")

        human_turn = {"ts": ts, "role": "human", "text": text}
        _state["chat_log"].append(human_turn)

    _append_jsonl(CHATS_LOG, human_turn)
    return jsonify({"accepted": True, "ts": ts}), 200


@app.get("/pending_ask")
def pending_ask():
    """Brains poll this; the benchmark never has a pending question."""
    return jsonify(None)


@app.get("/pending_chat")
def pending_chat():
    """Brain polls. Returns the active task statement when ready to work.

    Returns null if no task is active.
    """
    with _lock:
        if _state["phase"] not in (PHASE_TASK_ACTIVE, PHASE_WAITING_HUMAN):
            return jsonify(None)
        task = _state["current_task"]
        if task is None:
            return jsonify(None)
        # Pass the task statement plus current chat log so the brain can read
        # mid-task clarifications.
        if _state["claimed"]:
            # Sticky-claim only on first hand-off. Brain may re-poll for
            # mid-task chat updates — we always return current state.
            return jsonify({
                "task_id": task["task_id"],
                "text": task["text"],
                "step_count": task["step_count"],
                "chat_log": list(_state["chat_log"]),
                "action_log": list(_state["action_log"]),
                "claimed": True,
            })
        _state["claimed"] = True
        return jsonify({
            "task_id": task["task_id"],
            "text": task["text"],
            "step_count": task["step_count"],
            "chat_log": list(_state["chat_log"]),
            "action_log": list(_state["action_log"]),
            "claimed": False,
        })


@app.post("/propose_action")
def propose_action():
    """Brain proposes one action; it executes once POST /decide approves it.

    Body: {action: {...}, screenshot_b64: optional, ...}
    The action dict must include `type`, `category`, `rationale`, plus
    action-type-specific fields (x/y/text/etc.).
    """
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    screenshot_b64 = body.get("screenshot_b64") or ""

    if not isinstance(action, dict):
        return jsonify({"error": "missing 'action' object"}), 400
    if "type" not in action and "action_type" not in action:
        return jsonify({"error": "action must include 'type' or 'action_type'"}), 400
    if "category" not in action:
        return jsonify({"error": "action must include 'category'"}), 400
    if "rationale" not in action:
        return jsonify({"error": "action must include 'rationale'"}), 400

    # Normalise type key.
    if "type" not in action and "action_type" in action:
        action["type"] = action["action_type"]

    with _lock:
        if _state["phase"] not in (PHASE_TASK_ACTIVE,):
            return jsonify({
                "error": f"cannot propose in phase {_state['phase']!r}",
            }), 409

        # The graders read both fields (log_grader, partial_grader).
        action["category_original"] = action.get("category")

        token = str(uuid.uuid4())
        screenshot_hash = _hash_b64(screenshot_b64)

        task = _state["current_task"]
        task["step_count"] += 1
        step_n = task["step_count"]
        task_id = task["task_id"]

        # Advance the plan-progress pointer if the brain tagged this action with
        # the milestone it belongs to (1-based). Never moves backwards.
        ps = action.get("plan_step")
        if isinstance(ps, int) and ps > task.get("plan_step", 0):
            task["plan_step"] = ps

        _state["pending_action"] = action
        _state["pending_action_token"] = token
        _state["pending_action_screenshot_hash"] = screenshot_hash
        _state["phase"] = PHASE_PROPOSED

        # Action ceiling.
        if step_n > DEFAULT_TASK_ACTION_CEILING:
            log.warning("task %s exceeded action ceiling (%d)", task_id, step_n)
            action["ceiling_hint"] = (
                f"This task has now used {step_n} actions. Consider calling "
                "ask_human or complete_task soon."
            )

    log.info(
        "proposal: task=%s step=%d type=%s category=%s",
        task_id, step_n, action["type"], action.get("category"),
    )
    return jsonify({
        "accepted": True,
        "token": token,
        "step": step_n,
        "category_final": action.get("category"),
        "server_upgraded": False,
    }), 200


@app.post("/decide")
def decide():
    """Approve and execute the pending action.

    Body: {decision: "approve", token?: str}
    """
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    submitted_token = body.get("token")

    if decision != "approve":
        return jsonify({"error": "decision must be 'approve'"}), 400

    ts = _now_iso()

    with _lock:
        if _state["phase"] != PHASE_PROPOSED or _state["pending_action"] is None:
            return jsonify({"error": "no pending action"}), 400

        if submitted_token and submitted_token != _state["pending_action_token"]:
            return jsonify({"error": "stale token; refresh and try again"}), 409

        action = _state["pending_action"]
        token = _state["pending_action_token"]
        propose_hash = _state["pending_action_screenshot_hash"]
        task = _state["current_task"]
        task_id = task["task_id"] if task else None
        step_n = task["step_count"] if task else None
        category = action.get("category", "safe")

        _state["phase"] = PHASE_EXECUTING
        # Execute (under lock — actions are brief enough to tolerate)
        executor_result = inspector.execute(action)
        execute_hash = _hash_b64(executor_result.get("post_screenshot_b64", ""))

        log_entry = {
            "ts": ts, "task_id": task_id, "step_n": step_n, "token": token,
            "action": _action_for_log(action), "decision": "approve",
            "decision_ts": ts,
            "executor_result": {
                "executed": executor_result.get("executed"),
                "action": executor_result.get("action"),
                "error": executor_result.get("error"),
            },
            "screenshot_hash_propose": propose_hash,
            "screenshot_hash_execute": execute_hash,
            "server_upgraded": False,
        }
        _state["action_log"].append({
            "ts": ts, "task_id": task_id, "step_n": step_n,
            "type": action["type"], "category": category,
            "category_original": action.get("category_original"),
            "decision": "approve",
            "executed": executor_result.get("executed"),
            "action_str": executor_result.get("action"),
            "error": executor_result.get("error"),
            "rationale": action.get("rationale"),
            "label": action.get("button_label") or action.get("field_label") or action.get("link_label"),
            "x": action.get("x"), "y": action.get("y"),
            "text": action.get("text"),
        })
        _state["pending_action"] = None
        _state["pending_action_token"] = None
        _state["pending_action_screenshot_hash"] = None
        _state["phase"] = PHASE_TASK_ACTIVE

        _append_jsonl(ACTIONS_LOG, log_entry)
        log.info(
            "decided: approve step=%d type=%s executed=%s",
            step_n, action["type"], executor_result.get("executed"),
        )
        return jsonify({
            "result": "executed" if executor_result.get("executed") else "execution_failed",
            "executor_result": executor_result,  # includes post_screenshot_b64
        }), 200


def _action_for_log(action: dict) -> dict:
    """Keep the audit log self-contained but slim — drop screenshot data."""
    return {k: v for k, v in action.items()
            if k not in ("post_screenshot_b64",)}


@app.post("/control")
def control():
    """Stop the current task. Body: {action: "stop"}"""
    body = request.get_json(silent=True) or {}
    cmd = body.get("action")
    ts = _now_iso()

    if cmd != "stop":
        return jsonify({"error": "action must be 'stop'"}), 400

    with _lock:
        task = _state["current_task"]
        task_id = task["task_id"] if task else None

        _state["phase"] = PHASE_STOPPED
        # Any pending action is implicitly dropped.
        if _state["pending_action"] is not None:
            _append_jsonl(ACTIONS_LOG, {
                "ts": ts, "task_id": task_id,
                "step_n": task["step_count"] if task else None,
                "token": _state["pending_action_token"],
                "action": _action_for_log(_state["pending_action"]),
                "decision": "stopped",
                "decision_ts": ts,
            })
            _state["pending_action"] = None
            _state["pending_action_token"] = None
            _state["pending_action_screenshot_hash"] = None

        if task_id:
            _append_jsonl(TASKS_LOG, {
                "ts": ts, "task_id": task_id, "event": "stopped",
            })
        log.info("stopped task=%s", task_id)

        if task:
            started_ts = task.get("ts")
            _state["last_task_outcome"] = {
                "task_text": task.get("text", ""),
                "summary": "Task stopped by user.",
                "step_count": task.get("step_count", 0),
                "auto_approved_count": 0,
                "started_ts": started_ts,
                "completed_ts": ts,
                "duration_s": _iso_diff_s(started_ts, ts) if started_ts else 0,
                "outcome": "stopped",
            }

        # The brain detects the stop via phase changing from TASK_ACTIVE to IDLE.
        _state["current_task"] = None
        _state["claimed"] = False
        _state["phase"] = PHASE_IDLE
        return jsonify({"stopped": True}), 200


@app.post("/complete_task")
def complete_task():
    """Brain declares the task done.

    Body: {summary: str, final_observation: optional {kind, details}}
    """
    body = request.get_json(silent=True) or {}
    summary = (body.get("summary") or "").strip()
    final_obs = body.get("final_observation")
    ts = _now_iso()

    if not summary:
        return jsonify({"error": "missing 'summary'"}), 400

    with _lock:
        if _state["phase"] not in (PHASE_TASK_ACTIVE, PHASE_WAITING_HUMAN):
            return jsonify({"error": f"cannot complete in phase {_state['phase']!r}"}), 409

        task = _state["current_task"]
        task_id = task["task_id"] if task else None
        step_count = task["step_count"] if task else 0

        agent_turn = {"ts": ts, "role": "agent", "text": summary}
        _state["chat_log"].append(agent_turn)

        if final_obs and isinstance(final_obs, dict):
            obs_entry = {
                "ts": ts,
                "task_id": task_id,
                "kind": final_obs.get("kind", "state_summary"),
                "summary": summary,
                "details": final_obs.get("details"),
                "confidence": "high",
                "step_count": step_count,
            }
            _state["observations"].append(obs_entry)
            _append_jsonl(OBSERVATIONS_LOG, obs_entry)

        _append_jsonl(TASKS_LOG, {
            "ts": ts, "task_id": task_id, "event": "completed",
            "summary": summary, "step_count": step_count,
        })
        _append_jsonl(CHATS_LOG, agent_turn)

        started_ts = task.get("ts") if task else None
        _state["last_task_outcome"] = {
            "task_text": task["text"] if task else "",
            "summary": summary,
            "step_count": step_count,
            "auto_approved_count": 0,
            "started_ts": started_ts,
            "completed_ts": ts,
            "duration_s": _iso_diff_s(started_ts, ts) if started_ts else 0,
            "outcome": "completed",
        }

        _state["phase"] = PHASE_IDLE
        _state["current_task"] = None
        _state["pending_action"] = None
        _state["pending_action_token"] = None
        _state["pending_action_screenshot_hash"] = None
        _state["claimed"] = False

    log.info("task %s completed in %d steps", task_id, step_count)
    return jsonify({"accepted": True}), 200


@app.post("/ask_human")
def ask_human():
    """Brain asks a clarifying question; task pauses until POST /chat replies."""
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    context = (body.get("context") or "").strip()
    ts = _now_iso()

    if not question:
        return jsonify({"error": "missing 'question'"}), 400

    with _lock:
        if _state["phase"] not in (PHASE_TASK_ACTIVE,):
            return jsonify({"error": f"cannot ask_human in phase {_state['phase']!r}"}), 409

        agent_text = question if not context else f"{question}\n\n(Context: {context})"
        agent_turn = {"ts": ts, "role": "agent", "text": agent_text}
        _state["chat_log"].append(agent_turn)
        _state["phase"] = PHASE_WAITING_HUMAN

    _append_jsonl(CHATS_LOG, agent_turn)
    log.info("ask_human: %r", question[:80])
    return jsonify({"accepted": True}), 200


@app.post("/announce_plan")
def announce_plan():
    """Brain states its plan once, at the start of a task. Logged only.

    Body: {summary: str, steps: [str, ...]}
    Does not gate execution — the brain proceeds straight to its first
    propose_action.
    """
    body = request.get_json(silent=True) or {}
    summary = (body.get("summary") or "").strip()
    steps = body.get("steps") or []
    if not isinstance(steps, list):
        steps = []
    steps = [str(s).strip() for s in steps if str(s).strip()]
    ts = _now_iso()

    if not summary and not steps:
        return jsonify({"error": "missing 'summary'/'steps'"}), 400

    with _lock:
        task = _state["current_task"]
        if _state["phase"] != PHASE_TASK_ACTIVE or task is None:
            return jsonify({"error": f"cannot announce_plan in phase {_state['phase']!r}"}), 409
        task["plan"] = {"summary": summary, "steps": steps}
        task["plan_step"] = 1 if steps else 0
        task_id = task["task_id"]
        _append_jsonl(TASKS_LOG, {
            "ts": ts, "task_id": task_id, "event": "plan",
            "summary": summary, "steps": steps,
        })

    log.info("announce_plan: %r (%d steps)", summary[:80], len(steps))
    return jsonify({"accepted": True}), 200


@app.post("/log_turn")
def log_turn():
    """Brain posts per-LLM-call telemetry for the benchmark harness.

    Body: {ts, task_id, step_n, api_call_start_ts, api_call_end_ts,
           input_tokens, output_tokens, brain}

    Fire-and-forget. Always returns {"accepted": True}. Server appends
    the payload (plus a server-side receive timestamp) to TURNS_LOG.
    Used downstream by the benchmark harness to compute cost-per-task,
    cost-per-success, and brain-thinking-time latency.
    """
    body = request.get_json(silent=True) or {}
    entry = {
        "ts_received": _now_iso(),
        "ts": body.get("ts"),
        "task_id": body.get("task_id"),
        "step_n": body.get("step_n"),
        "api_call_start_ts": body.get("api_call_start_ts"),
        "api_call_end_ts": body.get("api_call_end_ts"),
        "input_tokens": body.get("input_tokens"),
        "output_tokens": body.get("output_tokens"),
        "brain": body.get("brain"),
        "current_url": body.get("current_url"),
    }
    _append_jsonl(TURNS_LOG, entry)
    return jsonify({"accepted": True}), 200


@app.post("/navigate")
def navigate():
    """Navigate the browser to a URL before a task starts (nav-assist mode).

    Only allowed when phase=IDLE. Executes navigate_url directly — caller is
    the benchmark runner, not the brain. Used to pre-position the browser for
    nav-assisted runs.

    Body: {"url": "http://..."}
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    with _lock:
        if _state["phase"] != PHASE_IDLE:
            return jsonify({"error": f"cannot navigate while phase={_state['phase']}"}), 409
    # clear_list_filters: nav-assist is a per-task reset, so drop any persisted
    # list filters Frappe would otherwise restore from a previous task.
    result = inspector.execute({"type": "navigate_url", "url": url,
                                "clear_list_filters": True})
    log.info("navigate: url=%s executed=%s", url, result.get("executed"))
    return jsonify(result), 200 if result.get("executed") else 500


@app.route("/session_check", methods=["POST"])
def session_check():
    """Detect ERPNext session loss: if Chrome is on the login page, re-auth and re-navigate.

    Body: {"url": "http://..."} — URL to navigate to after successful re-auth.
    Only callable when phase=IDLE (same constraint as /navigate).
    Returns: {"logged_in": bool, "reauthed": bool, "current_url": str}
    """
    import json as _json
    import os as _os
    import urllib.request as _req
    import websocket as _wsmod

    body = request.get_json(silent=True) or {}
    intended_url = (body.get("url") or "").strip()

    with _lock:
        if _state["phase"] != PHASE_IDLE:
            return jsonify({"error": f"cannot session_check while phase={_state['phase']}"}), 409

    try:
        with _req.urlopen("http://localhost:9222/json/list", timeout=5) as r:
            tabs = _json.load(r)
        page_tab = next((t for t in tabs if t.get("type") == "page"), None)
        if not page_tab:
            return jsonify({"logged_in": True, "reauthed": False, "current_url": ""}), 200

        ws = _wsmod.create_connection(page_tab["webSocketDebuggerUrl"], timeout=10)
        ws.settimeout(10)
        ws.send(_json.dumps({"id": 1, "method": "Runtime.evaluate",
                              "params": {"expression": "document.location.href"}}))
        resp = _json.loads(ws.recv())
        ws.close()
        current_url = (resp.get("result", {}).get("result", {}).get("value") or "")

        if "/login" not in current_url and "#login" not in current_url:
            return jsonify({"logged_in": True, "reauthed": False, "current_url": current_url}), 200

        log.warning("session_check: browser on login page (%s) — re-authenticating", current_url)

        # Defaults match the site created by docker-compose.yml
        # (bench new-site --admin-password=admin).
        erpnext_usr = _os.environ.get("ERPNEXT_BROWSER_USR", "Administrator")
        erpnext_pwd = _os.environ.get("ERPNEXT_BROWSER_PWD", "admin")
        # URL-encode the form body, then embed it as a JSON string literal so
        # credentials containing quotes, '&' or '=' cannot break the request.
        form_body = _json.dumps(urlencode({"usr": erpnext_usr, "pwd": erpnext_pwd}))
        auth_js = (
            "fetch('/api/method/login',{"
            "method:'POST',"
            "headers:{'Content-Type':'application/x-www-form-urlencoded',"
            "'X-Frappe-CSRF-Token':'fetch'},"
            f"body:{form_body}"
            "}).then(r=>({status:r.status}))"
        )
        ws2 = _wsmod.create_connection(page_tab["webSocketDebuggerUrl"], timeout=10)
        ws2.settimeout(15)
        ws2.send(_json.dumps({"id": 1, "method": "Runtime.evaluate",
                               "params": {"expression": auth_js, "awaitPromise": True,
                                          "returnByValue": True}}))
        auth_resp = _json.loads(ws2.recv())
        ws2.close()
        login_status = ((auth_resp.get("result", {}).get("result", {}) or {})
                        .get("value") or {}).get("status")
        if login_status != 200:
            log.error("session_check: ERPNext login as %r failed (HTTP %s) — check "
                      "ERPNEXT_BROWSER_USR / ERPNEXT_BROWSER_PWD", erpnext_usr, login_status)
            return jsonify({"logged_in": False, "reauthed": False, "current_url": current_url,
                            "error": f"login failed (HTTP {login_status})"}), 200

        if intended_url:
            inspector.execute({"type": "navigate_url", "url": intended_url})
            log.info("session_check: re-navigated to %s after re-auth", intended_url)

        return jsonify({"logged_in": False, "reauthed": True, "current_url": current_url}), 200

    except Exception as exc:  # noqa: BLE001
        log.warning("session_check error: %s", exc)
        return jsonify({"logged_in": True, "reauthed": False, "error": str(exc)}), 200


# ══════════════════════════════════════════════════════════════════════
# Human baseline mode
# ══════════════════════════════════════════════════════════════════════
# A human annotator drives the real browser instead of a brain. Nothing is
# proposed, so nothing lands in actions.log and the graders have no evidence
# for the Navigate / Interact / Commit stages. These routes synthesise that
# evidence in the exact schema the graders already read (see human_capture.py).
#
# Deliberately additive: the task still starts via POST /chat from the runner
# and still ends via POST /complete_task from the panel, so the existing task
# lifecycle is untouched.

_human_lock = threading.Lock()
_human = {
    "active": False,
    "task_id": None,
    "started_ts": None,
    "step_n": 0,
    "events": 0,
    "last_url": None,
    "poller": None,
    # Per-task limit in seconds, pushed by the runner from the task YAML. The
    # panel counts down from the Start click, so the annotator's limit begins
    # when they begin, not when the runner created the task.
    "limit_s": 300,
}


def _human_url_poller() -> None:
    """While a human session is active, mirror the browser URL into turns.log.

    partial_grader's right_page check reads `current_url` from turns.log, which
    the brains populate per API call. A human makes no API calls, so we sample
    the URL instead. One entry per distinct URL keeps the log small while still
    proving which pages were reached.
    """
    import time as _time

    while True:
        _time.sleep(1.5)
        with _human_lock:
            if not _human["active"]:
                return
            task_id = _human["task_id"]
            last = _human["last_url"]

        url = _human_current_url()
        if not url or url == last:
            continue

        with _human_lock:
            if not _human["active"]:
                return
            _human["last_url"] = url

        _append_jsonl(TURNS_LOG, {
            "ts_received": _now_iso(),
            "ts": _now_iso(),
            "task_id": task_id,
            "step_n": None,
            "api_call_start_ts": None,
            "api_call_end_ts": None,
            "input_tokens": None,
            "output_tokens": None,
            "brain": "human",
            "current_url": url,
            "source": "human_url_poll",
        })


def _human_current_url() -> str:
    """Active tab URL via the CDP HTTP endpoint. "" on failure."""
    import urllib.request as _req

    try:
        with _req.urlopen("http://localhost:9222/json/list", timeout=4) as r:
            tabs = json.loads(r.read().decode("utf-8"))
        for tab in tabs if isinstance(tabs, list) else []:
            if isinstance(tab, dict) and tab.get("type") == "page":
                return tab.get("url", "") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


@app.get("/human")
def human_panel():
    """The annotator's panel: task text, Start / Done / I'm stuck, timer.

    novnc_port is the port as seen from the ANNOTATOR'S browser, i.e. the host
    side of the compose mapping ("3002:3000"), not the container port. Override
    with NOVNC_PUBLIC_PORT if the mapping changes.
    """
    import os as _os
    return render_template(
        "human.html",
        novnc_port=_os.environ.get("NOVNC_PUBLIC_PORT", "3002"),
    )


@app.post("/human/start")
def human_start():
    """Annotator pressed Start. Begins timing and installs the page agent."""
    ts = _now_iso()
    with _lock:
        if _state["phase"] != PHASE_TASK_ACTIVE or not _state["current_task"]:
            return jsonify({"error": f"no active task (phase={_state['phase']})"}), 409
        task_id = _state["current_task"]["task_id"]
        task_text = _state["current_task"]["text"]

    ok, detail = human_capture.inject_into_page(
        cdp_http="http://localhost:9222",
        observer_url="http://localhost:5051",
    )

    with _human_lock:
        already = _human["active"] and _human["task_id"] == task_id
        _human.update({
            "active": True,
            "task_id": task_id,
            "started_ts": ts,
            "step_n": 0 if not already else _human["step_n"],
            "events": 0 if not already else _human["events"],
            "last_url": None,
        })
        poller = _human["poller"]
        if poller is None or not poller.is_alive():
            t = threading.Thread(target=_human_url_poller, daemon=True,
                                 name="human-url-poller")
            _human["poller"] = t
            t.start()

    # Marker the runner reads to compute the human's own elapsed time, which
    # starts when Start is pressed, not when the runner created the task.
    _append_jsonl(TASKS_LOG, {
        "ts": ts, "task_id": task_id, "event": "human_start", "text": task_text,
    })
    log.info("human session started for task %s (inject ok=%s: %s)", task_id, ok, detail)
    return jsonify({"accepted": True, "injected": ok, "detail": detail,
                    "task_id": task_id, "started_ts": ts}), 200


@app.post("/human/event")
def human_event():
    """One captured browser event from the injected page agent.

    The body arrives as text/plain (a CORS "simple request", so the page needs
    no preflight and we need no CORS config). Always 200: this is telemetry and
    must never surface an error into the annotator's browser.
    """
    try:
        raw = request.get_data(as_text=True) or ""
        ev = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return ("", 204)
    if not isinstance(ev, dict):
        return ("", 204)

    with _human_lock:
        if not _human["active"] or not _human["task_id"]:
            return ("", 204)
        task_id = _human["task_id"]
        _human["step_n"] += 1
        step_n = _human["step_n"]

    entry = human_capture.map_event(ev, task_id, step_n)
    if entry is None:
        with _human_lock:
            _human["step_n"] -= 1     # ignored event, don't burn a step number
        return ("", 204)

    _append_jsonl(ACTIONS_LOG, entry)
    with _lock:
        _state["action_log"].append(entry)
    with _human_lock:
        _human["events"] += 1
    return ("", 204)


@app.post("/human/stop")
def human_stop():
    """End the capture session. Does NOT complete the task.

    The panel calls this and then the existing POST /complete_task, so the
    task lifecycle stays exactly as it is for the agents.
    """
    ts = _now_iso()
    with _human_lock:
        task_id = _human["task_id"]
        started = _human["started_ts"]
        events = _human["events"]
        _human.update({"active": False, "task_id": None, "started_ts": None,
                       "last_url": None})

    if task_id:
        _append_jsonl(TASKS_LOG, {
            "ts": ts, "task_id": task_id, "event": "human_stop",
            "started_ts": started, "captured_events": events,
        })
    log.info("human session stopped for task %s (%s events captured)", task_id, events)
    return jsonify({"accepted": True, "captured_events": events,
                    "started_ts": started, "stopped_ts": ts}), 200


@app.post("/human/limit")
def human_limit():
    """Runner pushes the current task's timeout_seconds from its YAML.

    The panel cannot infer this: a T1 create task allows 300s and a T2 create
    task allows 600s, and both prompts say "create".
    """
    body = request.get_json(silent=True) or {}
    try:
        secs = int(body.get("seconds"))
    except Exception:  # noqa: BLE001
        return jsonify({"error": "missing or non-integer 'seconds'"}), 400
    if secs <= 0:
        return jsonify({"error": "'seconds' must be positive"}), 400
    with _human_lock:
        _human["limit_s"] = secs
    return jsonify({"accepted": True, "limit_s": secs}), 200


@app.get("/human/session")
def human_session():
    """Current capture state, for the panel's status line."""
    with _human_lock:
        snap = dict(_human)
    snap.pop("poller", None)
    with _lock:
        snap["phase"] = _state["phase"]
        snap["task_text"] = (_state["current_task"] or {}).get("text", "")
        snap["task_hint"] = (_state["current_task"] or {}).get("human_hint", "")
        snap["task_id_active"] = (_state["current_task"] or {}).get("task_id")
    return jsonify(snap)


if __name__ == "__main__":
    app.run(host=os.environ.get("OBSERVER_BIND", "127.0.0.1"), port=5051, debug=False)
