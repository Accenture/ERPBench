"""Claude brain for ERPBench — task executor.

Polls the Flask server for an active task. For each task, runs a loop:

  1. Take screenshot.
  2. Read cursor coords.
  3. Fetch chat + action history from /status.
  4. Call Claude with three tools:
       - propose_action   : propose ONE action; waits for human approve/reject
       - complete_task    : declare task done with summary
       - ask_human        : pause and ask a clarifying question
  5. Parse the tool_use and act:
       - propose_action  → POST /propose_action; poll /status until phase
                           leaves PROPOSED; on approve, the executor result
                           contains the post-action screenshot; on reject,
                           the brain proposes an alternative next round.
       - complete_task   → POST /complete_task; loop ends.
       - ask_human       → POST /ask_human; poll until phase leaves WAITING_HUMAN.
  6. Respect the `paused` flag: sleep and re-check.
  7. Detect STOPPED phase or task disappearing: break out of loop.

Environment variables:
  ANTHROPIC_API_KEY            required
  CLAUDE_MODEL                 default claude-sonnet-4-6
  OBSERVER_URL                 default http://localhost:5051
  SYSTEM_PROMPT_PATH           default /app/observer/system_prompt_claude.md
  POLL_INTERVAL                default 0.5 (seconds between /status polls)
  TASK_POLL_INTERVAL           default 1.0 (seconds between /pending_chat polls)
  MAX_TURNS_PER_TASK           default 50 (hard cap on Claude calls per task)
"""

import argparse
import base64
import io
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from anthropic import Anthropic
from PIL import Image, ImageDraw

import inspector


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 form, second precision. Matches observer_server."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_current_url() -> str:
    """Query Chromium's remote debug port for the active tab URL."""
    try:
        _, tabs = _http_json("GET", "http://localhost:9222/json/list", timeout=3.0)
        if isinstance(tabs, list):
            for tab in tabs:
                if isinstance(tab, dict) and tab.get("type") == "page":
                    return tab.get("url", "")
    except Exception:  # noqa: BLE001
        pass
    return ""


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_TASK_POLL_INTERVAL = 1.0
DEFAULT_MAX_TURNS_PER_TASK = 50
DEFAULT_SYSTEM_PROMPT_PATH = "/app/observer/system_prompt_claude.md"
MAX_OUTPUT_TOKENS = 1024
HISTORY_TURNS = 20


log = logging.getLogger("claude_brain")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


# ── Tool schemas ────────────────────────────────────────────────────
ANNOUNCE_PLAN_TOOL = {
    "name": "announce_plan",
    "description": (
        "Call this ONCE as the very first tool of every new task, before any "
        "propose_action. State your plan before you touch anything. After "
        "announcing, go straight to your first propose_action. Steps "
        "must be COARSE milestones (one per record / document / sub-goal), "
        "never individual clicks."
    ),
    "input_schema": {
        "type": "object",
        "required": ["summary", "steps"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "One short line: the overall goal and any shared properties.",
            },
            "steps": {
                "type": "array",
                "description": "Ordered coarse milestones, 3–7 items. NOT individual clicks.",
                "items": {"type": "string"},
            },
        },
    },
}

PROPOSE_TOOL = {
    "name": "propose_action",
    "description": (
        "Propose ONE action against the screen. The action is executed and "
        "you receive the outcome plus a fresh screenshot. Use this for every "
        "step of a task — never batch actions."
    ),
    "input_schema": {
        "type": "object",
        "required": ["action_type", "category", "rationale"],
        "properties": {
            "action_type": {
                "type": "string",
                "enum": [
                    "move_to", "scroll", "click_field", "click_dropdown",
                    "click_button", "click_link", "type", "select_option",
                    "set_date", "clear_field", "key", "navigate_url",
                ],
            },
            "category": {
                "type": "string",
                "description": "safe | commit | irreversible — see system prompt for the rules.",
                "enum": ["safe", "commit", "irreversible"],
            },
            "rationale": {"type": "string", "description": "ONE sentence: why this step now?"},
            "plan_step": {
                "type": "integer",
                "description": "1-based index of the announce_plan milestone this action belongs to. Lets the UI track progress.",
            },
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "text": {"type": "string"},
            "value": {"type": "string"},
            "amount": {"type": "integer"},
            "key": {
                "type": "string",
                "enum": ["tab", "enter", "return", "escape", "esc", "backspace", "delete"],
            },
            "button_role": {
                "type": "string",
                "enum": [
                    "safe", "save", "submit", "delete", "discard",
                    "print", "email", "send", "status_change", "navigate",
                ],
            },
            "url": {"type": "string", "description": "URL to navigate to. Used only with action_type=navigate_url."},
            "button_label": {"type": "string", "description": "Visible text on the button, recorded in the action log."},
            "link_label": {"type": "string"},
            "field_label": {"type": "string", "description": "Visible label of the field, recorded in the action log."},
        },
    },
}

COMPLETE_TOOL = {
    "name": "complete_task",
    "description": (
        "Call when the task is finished. The form is filled, the record is "
        "created, or the goal is achieved. Emit a final summary."
    ),
    "input_schema": {
        "type": "object",
        "required": ["summary"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "≤4 sentences. What did you do? What's the final state on screen?",
            },
            "final_observation": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["state_summary", "workflow_event", "knowledge_extract", "anomaly"],
                    },
                    "details": {"type": "object"},
                },
            },
        },
    },
}

RESPOND_TOOL = {
    "name": "respond_to_human",
    "description": (
        "Used in ASK MODE only. Provide your natural-language answer to the "
        "user's screen question AND a structured observation. Call this when "
        "you have enough information from the current screenshot to answer. "
        "Do NOT call this in Do mode — propose_action / complete_task / "
        "ask_human are for Do mode."
    ),
    "input_schema": {
        "type": "object",
        "required": ["chat_reply", "observation"],
        "properties": {
            "chat_reply": {
                "type": "string",
                "description": "Plain-language answer for the user. ≤4 sentences, no markdown.",
            },
            "observation": {
                "type": "object",
                "required": ["kind", "summary", "confidence"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["state_summary", "knowledge_extract", "anomaly", "workflow_event"],
                    },
                    "summary": {"type": "string", "description": "One-sentence description of what you saw."},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "details": {"type": "object"},
                },
            },
        },
    },
}

ASK_HUMAN_TOOL = {
    "name": "ask_human",
    "description": (
        "Pause and ask the human a clarifying question when the task is "
        "ambiguous or you can't proceed. Task remains active; the human's "
        "next chat resumes you."
    ),
    "input_schema": {
        "type": "object",
        "required": ["question", "context"],
        "properties": {
            "question": {"type": "string"},
            "context": {"type": "string", "description": "≤2 sentences explaining why you can't proceed."},
        },
    },
}

ALL_TOOLS = [ANNOUNCE_PLAN_TOOL, PROPOSE_TOOL, COMPLETE_TOOL, ASK_HUMAN_TOOL]


# ── HTTP helper ─────────────────────────────────────────────────────
def _http_json(method: str, url: str, body=None, timeout: float = 10.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = None
        return e.code, payload


# ── Screenshot + cursor overlay ─────────────────────────────────────
def take_annotated_screenshot():
    """Capture screen + overlay red crosshair at cursor. Returns (b64_png, (x, y))."""
    cursor_x, cursor_y = inspector.cursor_position()
    result = inspector.execute({"type": "screenshot"})
    if not result.get("executed"):
        log.warning("screenshot capture failed: %s", result.get("action"))
        return None, (cursor_x, cursor_y)

    raw_b64 = result.get("post_screenshot_b64")
    if not raw_b64:
        log.warning("screenshot result missing image: %s",
                    result.get("post_screenshot_error") or result.get("action"))
        return None, (cursor_x, cursor_y)
    img = Image.open(io.BytesIO(base64.b64decode(raw_b64))).convert("RGB")
    draw = ImageDraw.Draw(img)
    r = 12
    draw.ellipse(
        (cursor_x - r, cursor_y - r, cursor_x + r, cursor_y + r),
        outline="red", width=3,
    )
    draw.line((cursor_x - r * 2, cursor_y, cursor_x + r * 2, cursor_y), fill="red", width=2)
    draw.line((cursor_x, cursor_y - r * 2, cursor_x, cursor_y + r * 2), fill="red", width=2)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), (cursor_x, cursor_y)


def annotate_screenshot_b64(raw_b64: str, cursor_x: int, cursor_y: int) -> str:
    """Re-overlay crosshair on a screenshot returned from inspector.execute()."""
    img = Image.open(io.BytesIO(base64.b64decode(raw_b64))).convert("RGB")
    draw = ImageDraw.Draw(img)
    r = 12
    draw.ellipse(
        (cursor_x - r, cursor_y - r, cursor_x + r, cursor_y + r),
        outline="red", width=3,
    )
    draw.line((cursor_x - r * 2, cursor_y, cursor_x + r * 2, cursor_y), fill="red", width=2)
    draw.line((cursor_x, cursor_y - r * 2, cursor_x, cursor_y + r * 2), fill="red", width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def make_image_block(b64_png: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64_png},
    }


# ── Chat + history rendering ────────────────────────────────────────
def fetch_state(server_url: str):
    code, body = _http_json("GET", f"{server_url}/status", timeout=5.0)
    if code != 200 or not body:
        return None
    return body


def format_chat_history(chat_log) -> str:
    """Render the recent chat history as plain text for inclusion in the user turn."""
    if not chat_log:
        return ""
    recent = chat_log[-HISTORY_TURNS:]
    lines = []
    for turn in recent:
        speaker = "Agent" if turn.get("role") == "agent" else "Human"
        text = (turn.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def format_action_history(action_log) -> str:
    """Render the action history compactly for the user turn."""
    if not action_log:
        return "(no actions yet)"
    recent = action_log[-HISTORY_TURNS:]
    lines = []
    for a in recent:
        step = a.get("step_n", "?")
        atype = a.get("type", "?")
        cat = a.get("category", "?")
        decision = a.get("decision", "?")
        mark = "✓" if (decision == "approve" and a.get("executed")) else (
            "⚠" if decision == "approve" else "✗"
        )
        target = a.get("label") or ""
        coords = ""
        if a.get("x") is not None and a.get("y") is not None:
            coords = f" @({a['x']}, {a['y']})"
        text_part = ""
        if a.get("text"):
            text_part = f' text="{a["text"][:40]}"'
        rationale = a.get("rationale") or ""
        line = f"  Step {step} {mark} [{cat}] {atype}{coords}{text_part}"
        if target:
            line += f" → {target}"
        if rationale:
            line += f"   ({rationale[:80]})"
        if decision == "reject":
            line += "  ← REJECTED by human"
        lines.append(line)
    return "\n".join(lines)


# ── Wait helpers ────────────────────────────────────────────────────
def wait_for_decision(server_url: str, poll_interval: float, timeout: float = 600.0):
    """After /propose_action, wait until the phase leaves PROPOSED.

    Returns (final_state, executor_result_or_none).
    On approve: final_state.phase == TASK_ACTIVE; executor_result contains post_screenshot_b64.
    On reject: final_state.phase == TASK_ACTIVE; executor_result is None.
    On stop: final_state.phase == IDLE.
    """
    deadline = time.time() + timeout
    last_action_log_len = -1
    while time.time() < deadline:
        state = fetch_state(server_url)
        if state is None:
            time.sleep(poll_interval)
            continue
        phase = state.get("phase")
        if phase == "IDLE":
            return state, None
        if phase in ("TASK_ACTIVE", "WAITING_HUMAN"):
            # Look at the most-recent action_log entry to find the executor result.
            alog = state.get("action_log") or []
            if alog and len(alog) > last_action_log_len:
                # The latest entry is the decided one
                return state, alog[-1]
            return state, None
        time.sleep(poll_interval)
    raise TimeoutError("decision wait timed out")


def wait_for_chat(server_url: str, poll_interval: float, timeout: float = 1800.0):
    """After /ask_human, wait until phase leaves WAITING_HUMAN (human chatted)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = fetch_state(server_url)
        if state is None:
            time.sleep(poll_interval)
            continue
        phase = state.get("phase")
        if phase in ("TASK_ACTIVE", "IDLE"):
            return state
        time.sleep(poll_interval)
    raise TimeoutError("chat wait timed out")


def wait_unpaused(server_url: str, poll_interval: float):
    while True:
        state = fetch_state(server_url)
        if state is None:
            time.sleep(poll_interval)
            continue
        if not state.get("paused"):
            return state
        time.sleep(poll_interval)


# ── Task loop ───────────────────────────────────────────────────────
def run_task(client, model, system_prompt, server_url, task, max_turns: int, poll_interval: float):
    """Main per-task loop. Returns when task completes / stops / errors."""
    task_id = task["task_id"]
    task_text = task["text"]
    log.info("task loop started: id=%s text=%r", task_id, task_text)

    # Conversation messages list (Anthropic-format).
    # We carry it across turns so Claude has the running history of its own
    # proposals + tool_results without us replaying the whole story textually.
    messages = []

    # Open with the task statement + initial screenshot.
    init_b64, init_cursor = take_annotated_screenshot()
    state = fetch_state(server_url) or {}
    chat_history = format_chat_history(state.get("chat_log", []))
    action_history = format_action_history(state.get("action_log", []))

    opening_text = (
        f"TASK: {task_text}\n\n"
        f"User cursor at {init_cursor} (red crosshair on the screenshot).\n\n"
        f"Chat history:\n{chat_history or '(only this task statement so far)'}\n\n"
        f"Action history so far:\n{action_history}"
    )
    user_content = []
    if init_b64:
        user_content.append(make_image_block(init_b64))
    user_content.append({"type": "text", "text": opening_text})
    messages.append({"role": "user", "content": user_content})

    for turn in range(max_turns):
        # Honour pause.
        state = fetch_state(server_url) or {}
        if state.get("paused"):
            log.info("paused — waiting for resume")
            state = wait_unpaused(server_url, poll_interval)
        if state.get("phase") in ("IDLE", "STOPPED"):
            # Task gone (stopped externally) → break.
            log.info("task ended externally; phase=%s", state.get("phase"))
            return

        # Call Claude. Capture per-turn timing + token telemetry for the
        # benchmark harness (fire-and-forget POST to /log_turn; never blocks
        # the main flow if the server is busy).
        current_url = get_current_url()
        api_call_start_ts = _now_iso()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=ALL_TOOLS,
                tool_choice={"type": "any"},
                messages=messages,
            )
            api_call_end_ts = _now_iso()
            try:
                _http_json("POST", f"{server_url}/log_turn", {
                    "ts": api_call_start_ts,
                    "task_id": task_id,
                    "step_n": turn,
                    "api_call_start_ts": api_call_start_ts,
                    "api_call_end_ts": api_call_end_ts,
                    "input_tokens":  getattr(response.usage, "input_tokens", None),
                    "output_tokens": getattr(response.usage, "output_tokens", None),
                    "brain": "claude",
                    "current_url": current_url,
                })
            except Exception:  # noqa: BLE001 — never let logging break the brain
                pass
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            log.error("Claude API call failed: %r", exc)
            _http_json("POST", f"{server_url}/ask_human", {
                "question": "I hit an API error and can't continue. Please advise.",
                "context": str(exc)[:200],
            })
            new_state = wait_for_chat(server_url, poll_interval)
            chat_history = format_chat_history(new_state.get("chat_log", []))
            messages.append({"role": "user", "content": [
                {"type": "text", "text": f"Human replied. Updated chat:\n{chat_history}"}
            ]})
            continue

        tool_use = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"),
            None,
        )

        # Append the full assistant turn back to messages.
        messages.append({
            "role": "assistant",
            "content": [_block_to_dict(b) for b in response.content],
        })

        if tool_use is None:
            text = "".join(getattr(b, "text", "") for b in response.content)
            log.warning("Claude returned text without tool call; forcing complete_task")
            _http_json("POST", f"{server_url}/complete_task", {
                "summary": text or "Task ended without a clear summary.",
                "final_observation": {"kind": "state_summary", "details": {"reason": "model returned text"}},
            })
            return

        name = tool_use.name
        args = dict(tool_use.input)

        if name == "announce_plan":
            log.info("announce_plan: %r (%d steps)",
                     args.get("summary", "")[:80], len(args.get("steps", []) or []))
            _http_json("POST", f"{server_url}/announce_plan", args)
            # Feed the result back so Claude proceeds to its first action.
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": "Plan recorded and shown to the human. Now propose your first action.",
                }],
            })
            continue

        if name == "complete_task":
            log.info("complete_task: %r", args.get("summary", "")[:80])
            _http_json("POST", f"{server_url}/complete_task", args)
            return

        if name == "ask_human":
            log.info("ask_human: %r", args.get("question", "")[:80])
            _http_json("POST", f"{server_url}/ask_human", args)
            try:
                new_state = wait_for_chat(server_url, poll_interval)
            except TimeoutError:
                log.warning("ask_human wait timed out; ending task")
                return
            if new_state.get("phase") == "IDLE":
                log.info("task ended while waiting on human; phase=IDLE")
                return
            # Feed the human's response back via a tool_result.
            chat_history = format_chat_history(new_state.get("chat_log", []))
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": f"Human responded. Updated chat:\n{chat_history}",
                }],
            })
            continue

        if name == "propose_action":
            # Map action_type → type for the server.
            action_payload = dict(args)
            action_payload["type"] = action_payload.pop("action_type")

            # Take a fresh screenshot for the audit hash. (We just took one for
            # the Claude call; we don't need another — but we do need its b64
            # available. Re-render in case the screen drifted.)
            ss_b64, _cursor = take_annotated_screenshot()

            code, propose_resp = _http_json(
                "POST", f"{server_url}/propose_action",
                {"action": action_payload, "screenshot_b64": ss_b64},
                timeout=10.0,
            )
            if code == 429:
                # Rate-limit (too many identical rejections).
                msg = (propose_resp or {}).get("message", "policy violation")
                log.warning("server policy_violation: %s", msg)
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": (
                            f"Server rejected your proposal: {msg}\n"
                            "You MUST call ask_human (clarify) or complete_task (abort) next. "
                            "Do not propose the same action again."
                        ),
                        "is_error": True,
                    }],
                })
                continue
            if code != 200:
                err = (propose_resp or {}).get("error", f"server returned {code}")
                log.warning("propose_action rejected by server: %s", err)
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": f"Server rejected proposal: {err}",
                        "is_error": True,
                    }],
                })
                continue

            server_upgraded = propose_resp.get("server_upgraded", False)
            if server_upgraded:
                log.info("server upgraded category to %s", propose_resp.get("category_final"))

            # Wait for human decision.
            try:
                final_state, last_action = wait_for_decision(
                    server_url, poll_interval, timeout=600.0,
                )
            except TimeoutError:
                log.warning("decision wait timed out — ending task")
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": "Decision wait timed out (10 min). Try ask_human.",
                        "is_error": True,
                    }],
                })
                continue

            if final_state.get("phase") == "IDLE":
                # Stopped externally.
                log.info("task stopped during decision wait")
                return

            decision = (last_action or {}).get("decision", "unknown")
            if decision == "reject":
                rationale = "Human rejected this proposal."
                consec = final_state.get("consecutive_rejections", 0)
                if consec >= 2:
                    rationale += f" That's {consec} consecutive rejection(s). Try a DIFFERENT approach (different coordinates, different action type, or ask_human)."
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": rationale,
                    }],
                })
                continue

            # approve — feed back the executor result + fresh post-action screenshot.
            executor_result = (last_action or {})
            new_b64, new_cursor = take_annotated_screenshot()
            text_summary = (
                f"Action executed.\n"
                f"Result: {executor_result.get('action_str') or 'executed'}\n"
                f"Executed: {executor_result.get('executed')}\n"
                f"Cursor now at {new_cursor}."
            )
            tool_result_content = [{"type": "text", "text": text_summary}]
            if new_b64:
                tool_result_content.append(make_image_block(new_b64))
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": tool_result_content,
                }],
            })

            # Append a fresh action-history snapshot as a separate user message so
            # Claude can scan progress without parsing the tool_result chain.
            updated_state = fetch_state(server_url) or {}
            action_history = format_action_history(updated_state.get("action_log", []))
            chat_history = format_chat_history(updated_state.get("chat_log", []))
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        f"Task progress so far:\n{action_history}\n\n"
                        f"Recent chat:\n{chat_history}"
                    ),
                }],
            })
            continue

        # Unknown tool — shouldn't happen given the schema.
        log.warning("unknown tool: %s", name)
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": f"Unknown tool {name}",
                "is_error": True,
            }],
        })

    # Hit the hard turn cap.
    log.warning("hit max_turns (%d) — forcing complete_task", max_turns)
    _http_json("POST", f"{server_url}/complete_task", {
        "summary": (
            "I've reached the per-task turn cap without finishing. "
            "Stopping here for safety."
        ),
        "final_observation": {
            "kind": "anomaly",
            "details": {"reason": "max_turns hit", "turns": max_turns},
        },
    })


def _block_to_dict(block) -> dict:
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}


# ── Ask-mode loop (single-round Q&A) ────────────────────────────────
def run_ask(client, model, system_prompt, server_url, ask):
    """Answer one Ask-mode question. Single round: screenshot → Claude → /respond."""
    question = ask.get("text", "")
    chat_log = ask.get("chat_log") or []
    log.info("ask: %r", question[:80])

    screenshot_b64, cursor_xy = take_annotated_screenshot()
    user_content = []
    if screenshot_b64:
        user_content.append(make_image_block(screenshot_b64))
    history_note = format_chat_history(chat_log) if chat_log else ""
    user_content.append({
        "type": "text",
        "text": (
            f"User cursor is at {cursor_xy} (marked with a red crosshair on the screenshot).\n"
            f"User question: {question}\n\n"
            f"{history_note}\n\n"
            "MODE: ASK. Call `respond_to_human` with chat_reply + observation. "
            "Do NOT propose actions; this is a look-only question-answer turn."
        ),
    })

    api_kwargs = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_content}],
        "tools": [RESPOND_TOOL],
        "tool_choice": {"type": "tool", "name": "respond_to_human"},
    }
    api_call_start_ts = _now_iso()
    response = client.messages.create(**api_kwargs)
    api_call_end_ts = _now_iso()
    try:
        _http_json("POST", f"{server_url}/log_turn", {
            "ts": api_call_start_ts,
            "task_id": None,           # Ask mode is one-shot; no task_id
            "step_n": 0,
            "api_call_start_ts": api_call_start_ts,
            "api_call_end_ts": api_call_end_ts,
            "input_tokens":  getattr(response.usage, "input_tokens", None),
            "output_tokens": getattr(response.usage, "output_tokens", None),
            "brain": "claude",
            "current_url": get_current_url(),
        })
    except Exception:  # noqa: BLE001 — never let logging break the brain
        pass

    tool_use = next((b for b in response.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        # Fallback: model returned only text. Wrap as a best-effort respond.
        text = "".join(getattr(b, "text", "") for b in response.content) or "(no answer produced)"
        payload = {
            "chat_reply": text,
            "observation": {
                "kind": "state_summary",
                "summary": "Model returned text without calling respond_to_human",
                "confidence": "low",
            },
        }
    else:
        payload = dict(tool_use.input)

    _http_json("POST", f"{server_url}/respond", payload)


# ── Main loop ───────────────────────────────────────────────────────
def load_system_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("OBSERVER_URL", "http://localhost:5051"))
    parser.add_argument("--poll", type=float,
                        default=float(os.environ.get("POLL_INTERVAL", DEFAULT_POLL_INTERVAL)))
    parser.add_argument("--task-poll", type=float,
                        default=float(os.environ.get("TASK_POLL_INTERVAL", DEFAULT_TASK_POLL_INTERVAL)))
    parser.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-turns", type=int,
                        default=int(os.environ.get("MAX_TURNS_PER_TASK", DEFAULT_MAX_TURNS_PER_TASK)))
    parser.add_argument("--system-prompt",
                        default=os.environ.get("SYSTEM_PROMPT_PATH", DEFAULT_SYSTEM_PROMPT_PATH))
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    try:
        system_prompt = load_system_prompt(args.system_prompt)
    except OSError as exc:
        print(f"ERROR: cannot read system prompt at {args.system_prompt}: {exc}", file=sys.stderr)
        return 2

    client = Anthropic(api_key=api_key)
    print(
        f"\nClaude brain → {args.url}  model={args.model}  max_turns={args.max_turns}",
        flush=True,
    )
    print(
        f"System prompt: {len(system_prompt)} chars from {args.system_prompt}\n",
        flush=True,
    )

    while True:
        # Poll for a Do-mode task first.
        try:
            code, body = _http_json("GET", f"{args.url}/pending_chat", timeout=3.0)
        except urllib.error.URLError as exc:
            print(f"  poll failed: {exc} — retrying", file=sys.stderr, flush=True)
            time.sleep(args.task_poll)
            continue

        if code == 200 and body is not None and not body.get("claimed"):
            # Got a fresh Do task.
            try:
                run_task(
                    client=client,
                    model=args.model,
                    system_prompt=system_prompt,
                    server_url=args.url,
                    task=body,
                    max_turns=args.max_turns,
                    poll_interval=args.poll,
                )
            except Exception as exc:  # noqa: BLE001 — never crash the outer loop
                print(f"  task loop crashed: {exc!r}", file=sys.stderr, flush=True)
                try:
                    _http_json("POST", f"{args.url}/complete_task", {
                        "summary": f"Internal error: {exc}. Task aborted.",
                        "final_observation": {
                            "kind": "anomaly",
                            "details": {"error": str(exc), "type": type(exc).__name__},
                        },
                    })
                except Exception:  # noqa: BLE001
                    pass
            continue

        # No Do task — check for an Ask-mode question.
        try:
            code, ask_body = _http_json("GET", f"{args.url}/pending_ask", timeout=3.0)
        except urllib.error.URLError as exc:
            time.sleep(args.task_poll)
            continue

        if code == 200 and ask_body is not None:
            try:
                run_ask(
                    client=client,
                    model=args.model,
                    system_prompt=system_prompt,
                    server_url=args.url,
                    ask=ask_body,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ask loop crashed: {exc!r}", file=sys.stderr, flush=True)
                try:
                    _http_json("POST", f"{args.url}/respond", {
                        "chat_reply": f"Sorry — internal error while answering: {exc}",
                        "observation": {
                            "kind": "anomaly",
                            "summary": "brain crash during /pending_ask handling",
                            "confidence": "low",
                            "details": {"error": str(exc), "type": type(exc).__name__},
                        },
                    })
                except Exception:  # noqa: BLE001
                    pass
            continue

        time.sleep(args.task_poll)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
