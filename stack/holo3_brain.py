"""Holo3-35B-A3B brain for ERPBench.

Polls the Flask observer for an active task. For each task, runs a loop:

  1. Take a screenshot.
  2. Fetch action + chat history from /status.
  3. Call Holo3 via an OpenAI-compatible /chat/completions endpoint.
  4. Strip <think>...</think> reasoning block, parse JSON action.
  5. Map Holo3 native tool_call schema → CUA bench propose_action schema.
  6. POST /propose_action; poll /status until phase leaves PROPOSED.
  7. On tool_name "answer" → POST /complete_task and break.

Coordinate convention: Holo3 documents x/y on a 0-1000 grid, but inconsistently
emits absolute pixel values (0-1280 / 0-720). _smart_coord() detects values > 1000
as absolute pixels (clamps to screen); values ≤ 1000 are treated as normalized.

Environment variables:
  HOLO3_API_KEY        required — API key for the endpoint below
  HOLO3_API_BASE_URL   required (OpenAI-compatible endpoint serving Holo3)
  HOLO3_API_MODEL      default holo3-35b-a3b
  SYSTEM_PROMPT_PATH   default /app/observer/system_prompt_holo3.md
  OBSERVER_URL         default http://localhost:5051
  MAX_TURNS_PER_TASK   default 50
  POLL_INTERVAL        default 0.5
  TASK_POLL_INTERVAL   default 1.0
"""

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import inspector

log = logging.getLogger("holo3_brain")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ── Config ───────────────────────────────────────────────────────────
HOLO3_API_KEY = os.environ.get("HOLO3_API_KEY", "")
HOLO3_API_BASE_URL = os.environ.get("HOLO3_API_BASE_URL") or ""
HOLO3_API_MODEL = os.environ.get("HOLO3_API_MODEL", "holo3-35b-a3b")
OBSERVER_URL = os.environ.get("OBSERVER_URL", "http://localhost:5051")
MAX_TURNS = int(os.environ.get("MAX_TURNS_PER_TASK", "50"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "0.5"))
TASK_POLL_INTERVAL = float(os.environ.get("TASK_POLL_INTERVAL", "1.0"))
SYSTEM_PROMPT_PATH = os.environ.get(
    "SYSTEM_PROMPT_PATH", "/app/observer/system_prompt_holo3.md"
)

SCREEN_W = 1280
SCREEN_H = 720
NORM_GRID = 1000   # documented Holo3 coordinate space
MAX_OUTPUT_TOKENS = 2048
HISTORY_TURNS = 15
SAME_ACTION_LIMIT = 5
SCROLL_STREAK_LIMIT = 6


# ── Utilities ────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _http_json(method: str, url: str, body=None, timeout: float = 10.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = None
        return exc.code, payload


def fetch_state(server_url: str):
    code, body = _http_json("GET", f"{server_url}/status", timeout=5.0)
    if code != 200 or not body:
        return None
    return body


def screenshot_b64() -> str | None:
    """Capture current screen. Returns base64-encoded PNG string or None."""
    try:
        result = inspector.execute({"type": "screenshot"})
        if result.get("executed"):
            return result.get("post_screenshot_b64")
    except Exception as exc:
        log.warning("screenshot failed: %r", exc)
    return None


def _smart_coord(x_raw, y_raw) -> tuple[int, int]:
    """Convert Holo3 coordinates to absolute screen pixels.

    Holo3 documents 0-1000 normalized but inconsistently emits absolute pixels
    (0-1280 / 0-720) instead. Per-axis heuristic:
      - value > NORM_GRID (1000) → cannot be valid normalized → treat as absolute
        pixel, clamp to screen bounds.
      - value ≤ NORM_GRID → treat as normalized, apply standard conversion, clamp.
    A warning is logged whenever out-of-range values are detected.
    """
    oob_x = x_raw > NORM_GRID
    oob_y = y_raw > NORM_GRID
    if oob_x or oob_y:
        log.warning(
            "coord out-of-range: raw=(%s,%s) — treating %s as absolute pixel",
            x_raw, y_raw,
            "x" if oob_x and oob_y else ("x" if oob_x else "y"),
        )

    if oob_x:
        abs_x = max(0, min(int(x_raw), SCREEN_W - 1))
    else:
        abs_x = max(0, min(int(round(x_raw / NORM_GRID * SCREEN_W)), SCREEN_W - 1))

    if oob_y:
        abs_y = max(0, min(int(y_raw), SCREEN_H - 1))
    else:
        abs_y = max(0, min(int(round(y_raw / NORM_GRID * SCREEN_H)), SCREEN_H - 1))

    return abs_x, abs_y


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks that Holo3 prepends as reasoning traces."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def parse_holo3_response(raw_text: str) -> dict | None:
    """Strip the reasoning trace, then extract the JSON action object."""
    cleaned = strip_think(raw_text)
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(cleaned[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def format_action_history(action_log: list) -> str:
    if not action_log:
        return "(no actions yet)"
    lines = []
    for a in action_log[-HISTORY_TURNS:]:
        step = a.get("step_n", "?")
        atype = a.get("type", "?")
        decision = a.get("decision", "?")
        mark = "✓" if (decision == "approve" and a.get("executed")) else (
            "⚠" if decision == "approve" else "✗"
        )
        coords = ""
        if a.get("x") is not None and a.get("y") is not None:
            coords = f" @({a['x']},{a['y']})"
        text_part = f' text="{a["text"][:40]}"' if a.get("text") else ""
        rationale = (a.get("rationale") or "")[:80]
        line = f"  Step {step} {mark} {atype}{coords}{text_part}"
        if rationale:
            line += f"   ({rationale})"
        if decision == "reject":
            line += "  ← REJECTED"
        lines.append(line)
    return "\n".join(lines)


def format_chat_history(chat_log: list) -> str:
    if not chat_log:
        return ""
    lines = []
    for turn in chat_log[-HISTORY_TURNS:]:
        speaker = "Agent" if turn.get("role") == "agent" else "Human"
        text = (turn.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


# ── Decision wait ────────────────────────────────────────────────────
def wait_for_decision(server_url: str, poll_interval: float, timeout: float = 600.0):
    """Poll /status until phase leaves PROPOSED. Returns (state, last_action_or_None)."""
    deadline = time.time() + timeout
    last_alog_len = -1
    while time.time() < deadline:
        state = fetch_state(server_url)
        if state is None:
            time.sleep(poll_interval)
            continue
        phase = state.get("phase")
        if phase == "IDLE":
            return state, None
        if phase in ("TASK_ACTIVE", "WAITING_HUMAN"):
            alog = state.get("action_log") or []
            if alog and len(alog) > last_alog_len:
                return state, alog[-1]
            return state, None
        time.sleep(poll_interval)
    raise TimeoutError("decision wait timed out")


# ── Holo3 API call ───────────────────────────────────────────────────
def call_holo3(messages: list, system_prompt: str) -> dict:
    """POST to /chat/completions on the configured endpoint. Returns the full response dict."""
    url = f"{HOLO3_API_BASE_URL}/chat/completions"
    body = {
        "model": HOLO3_API_MODEL,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.7,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {HOLO3_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Action mapping ───────────────────────────────────────────────────
_COMMIT_SIGNALS = frozenset({"save", "submit", "update", "ctrl+s"})
_IRREVERSIBLE_SIGNALS = frozenset({"delete", "cancel", "discard", "amend"})


def _infer_category(tool_name: str, thought: str, key_val: str = "") -> str:
    """Infer action category from context (safe / commit / irreversible)."""
    text = (thought + " " + key_val).lower()
    if any(s in text for s in _IRREVERSIBLE_SIGNALS):
        return "irreversible"
    if any(s in text for s in _COMMIT_SIGNALS):
        return "commit"
    # key ctrl+s is always a commit (save shortcut)
    if key_val in ("ctrl+s",):
        return "commit"
    return "safe"


def map_action(tool_call: dict, thought: str = "") -> dict:
    """Map Holo3 tool_call → CUA bench propose_action payload (with absolute coords)."""
    tool_name = tool_call.get("tool_name", "click")
    x_norm = tool_call.get("x")
    y_norm = tool_call.get("y")

    action: dict = {"type": "click_field", "rationale": thought or f"holo3:{tool_name}"}

    if x_norm is not None and y_norm is not None:
        abs_x, abs_y = _smart_coord(x_norm, y_norm)
        action["x"] = abs_x
        action["y"] = abs_y

    key_val = ""

    if tool_name == "click":
        action["type"] = "click_field"

    elif tool_name == "write":
        action["type"] = "type"
        action["text"] = (
            tool_call.get("text")
            or tool_call.get("content")
            or tool_call.get("value")
            or ""
        )

    elif tool_name == "scroll":
        action["type"] = "scroll"
        direction = (tool_call.get("direction") or "down").lower()
        amount = tool_call.get("amount", 3)
        try:
            amount = int(abs(amount))
        except (TypeError, ValueError):
            amount = 3
        action["amount"] = amount if direction == "up" else -amount

    elif tool_name == "navigate_url":
        action["type"] = "navigate_url"
        action["url"] = tool_call.get("url", "")

    elif tool_name == "key":
        key_val = tool_call.get("key") or tool_call.get("value") or "tab"
        action["type"] = "key"
        action["key"] = key_val

    elif tool_name == "select_option":
        action["type"] = "select_option"
        action["text"] = (
            tool_call.get("text")
            or tool_call.get("option")
            or tool_call.get("value")
            or ""
        )

    elif tool_name == "set_date":
        action["type"] = "set_date"
        action["value"] = (
            tool_call.get("value")
            or tool_call.get("date")
            or ""
        )

    elif tool_name == "clear_field":
        action["type"] = "clear_field"

    action["category"] = _infer_category(tool_name, thought, key_val)
    return action


# ── Per-task loop ────────────────────────────────────────────────────
def run_task(
    task: dict,
    server_url: str,
    max_turns: int,
    poll_interval: float,
    system_prompt: str,
) -> None:
    task_id = task["task_id"]
    task_text = task["text"]
    log.info("task started: id=%s text=%r", task_id, task_text[:80])

    same_action_count: dict[str, int] = {}
    scroll_streak = 0
    last_action_type = ""
    conversation_history: list[dict] = []
    pending_no_tool_reminder = False

    for turn in range(max_turns):
        # Check external stop / pause
        state = fetch_state(server_url) or {}
        if state.get("phase") in ("IDLE", "STOPPED"):
            log.info("task ended externally; phase=%s", state.get("phase"))
            return
        if state.get("paused"):
            log.info("paused — waiting for resume")
            while True:
                time.sleep(poll_interval)
                state = fetch_state(server_url) or {}
                if not state.get("paused"):
                    break

        # Screenshot
        ss_b64 = screenshot_b64()
        if not ss_b64:
            log.warning("no screenshot available; sleeping 1s")
            time.sleep(1.0)
            continue

        # Build context text.
        # Turn 1: full task description + action history (model has no prior context).
        # Turn N+: only the screenshot + turn counter + new chat messages.
        # The model's own prior reasoning is already in conversation_history.
        action_hist = format_action_history(state.get("action_log", []))
        chat_hist = format_chat_history(state.get("chat_log", []))
        if turn == 0:
            context = (
                f"TASK: {task_text}\n\n"
                f"Turn: {turn + 1} of {max_turns}\n\n"
                f"Action history:\n{action_hist}\n"
            )
            if chat_hist:
                context += f"\nChat history:\n{chat_hist}\n"
        else:
            context = f"Turn: {turn + 1} of {max_turns}."
            if pending_no_tool_reminder:
                context += (
                    "\n\nNOTICE: Your previous response did not contain a valid tool_call "
                    "(tool_call was null or missing). You MUST include a non-null tool_call "
                    "with a specific action in your response this turn."
                )
            if chat_hist:
                context += f"\n\nChat history:\n{chat_hist}\n"

        user_msg = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{ss_b64}"},
                },
                {"type": "text", "text": context},
            ],
        }
        conversation_history.append(user_msg)

        # Build the messages list sent to the API: strip images from all past
        # user turns, keeping only their text. Old screenshots are stale and
        # accumulating them in context causes 10× inference slowdown with no
        # benefit. Only the current turn's screenshot (last user message) is sent.
        def _strip_images(msg: dict) -> dict:
            if msg["role"] != "user":
                return msg
            text_parts = [p for p in msg["content"] if p.get("type") == "text"]
            return {"role": "user", "content": text_parts} if text_parts else msg

        api_messages = (
            [_strip_images(m) for m in conversation_history[:-1]]
            + [conversation_history[-1]]  # current turn keeps its screenshot
        )

        # Call API with text-only history + current screenshot
        api_start_ts = _now_iso()
        try:
            resp = call_holo3(api_messages, system_prompt)
            api_end_ts = _now_iso()
        except Exception as exc:
            log.error("Holo3 API error: %r", exc)
            time.sleep(2.0)
            continue

        # Telemetry
        usage = resp.get("usage", {})
        try:
            _http_json("POST", f"{server_url}/log_turn", {
                "ts": api_start_ts,
                "task_id": task_id,
                "step_n": turn,
                "api_call_start_ts": api_start_ts,
                "api_call_end_ts": api_end_ts,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "brain": f"holo3-api-{HOLO3_API_MODEL}",
            })
        except Exception:
            pass

        # Parse
        raw_text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not raw_text:
            log.warning("turn=%d: empty response from Holo3", turn)
            continue

        parsed = parse_holo3_response(raw_text)
        if parsed is None:
            log.warning("turn=%d: unparseable response: %r", turn, raw_text[:200])
            # Still add the raw response to history so the model knows it emitted
            # something unparseable and can self-correct next turn.
            conversation_history.append({"role": "assistant", "content": raw_text})
            continue

        # Add assistant response to conversation history before acting on it.
        conversation_history.append({"role": "assistant", "content": raw_text})

        tool_call = parsed.get("tool_call") or {}
        tool_name = (tool_call.get("tool_name") or "").strip()
        note = (parsed.get("note") or "")
        thought = (parsed.get("thought") or "")
        log.info("turn=%d tool=%s note=%r", turn, tool_name, note[:80])

        if not tool_name:
            log.warning("turn=%d: tool_call missing or has no tool_name — skipping", turn)
            no_tool_count = same_action_count.get("no_tool_name", 0) + 1
            same_action_count["no_tool_name"] = no_tool_count
            pending_no_tool_reminder = True
            if no_tool_count >= SAME_ACTION_LIMIT:
                log.warning("loop-breaker: no tool_name repeated %d times — stopping", SAME_ACTION_LIMIT)
                _http_json("POST", f"{server_url}/complete_task", {
                    "summary": (
                        f"Stopped: model produced {SAME_ACTION_LIMIT} consecutive responses "
                        "without a tool call — cannot proceed."
                    ),
                    "final_observation": {"kind": "anomaly", "details": {"repeated_action": "no_tool_name"}},
                })
                return
            continue

        # Valid tool_call received — clear the no-tool streak and reminder.
        pending_no_tool_reminder = False
        same_action_count.pop("no_tool_name", None)

        # Completion
        if tool_name in ("answer", "complete_task"):
            summary = (
                tool_call.get("summary")
                or tool_call.get("answer")
                or thought
                or note
                or "Task completed."
            )
            log.info("complete_task: %r", summary[:80])
            _http_json("POST", f"{server_url}/complete_task", {
                "summary": summary,
                "final_observation": {"kind": "state_summary", "details": {"note": note}},
            })
            return

        # Ask human
        if tool_name == "ask_human":
            question = tool_call.get("question") or thought or "Need clarification."
            ctx = tool_call.get("context") or note or ""
            log.info("ask_human: %r", question[:80])
            _http_json("POST", f"{server_url}/ask_human", {
                "question": question,
                "context": ctx,
            })
            deadline = time.time() + 1800.0
            while time.time() < deadline:
                time.sleep(poll_interval)
                new_state = fetch_state(server_url)
                if new_state and new_state.get("phase") in ("TASK_ACTIVE", "IDLE"):
                    break
            continue

        # Map to CUA action
        action_payload = map_action(tool_call, thought=thought or note)

        # Fix 1: discard coordinate-required actions that have no coords.
        # Sending click_field(None, None) to the executor burns a loop-breaker
        # slot without doing anything useful. Skipping keeps the turn budget for
        # a valid action on the next pass.
        _COORD_ACTIONS = {"click_field", "click_button", "click_link", "click_dropdown", "move_to"}
        if action_payload.get("type") in _COORD_ACTIONS and (
            action_payload.get("x") is None or action_payload.get("y") is None
        ):
            log.warning(
                "turn=%d: %s with null coords (model couldn't locate target) — skipping turn",
                turn, action_payload.get("type"),
            )
            # Count null-coord skips so the loop-breaker can fire if the model
            # repeatedly fails to locate the same target (prevents turn-budget burn).
            null_key = f"null_coord:{action_payload.get('type', '')}"
            same_action_count[null_key] = same_action_count.get(null_key, 0) + 1
            if same_action_count[null_key] >= SAME_ACTION_LIMIT:
                log.warning(
                    "loop-breaker: null-coord action repeated %d times — stopping",
                    SAME_ACTION_LIMIT,
                )
                _http_json("POST", f"{server_url}/complete_task", {
                    "summary": (
                        f"Stopped: null-coord {action_payload.get('type', '')} repeated "
                        f"{SAME_ACTION_LIMIT} times — model cannot locate the target element."
                    ),
                    "final_observation": {"kind": "anomaly", "details": {"repeated_action": null_key}},
                })
                return
            continue

        # Loop breaker: identical action 3× → abort
        action_key = "{type}:{x}:{y}:{text}".format(
            type=action_payload.get("type", ""),
            x=action_payload.get("x", ""),
            y=action_payload.get("y", ""),
            text=action_payload.get("text", ""),
        )
        same_action_count[action_key] = same_action_count.get(action_key, 0) + 1
        if same_action_count[action_key] >= SAME_ACTION_LIMIT:
            log.warning("loop-breaker: action repeated %d times — stopping", SAME_ACTION_LIMIT)
            _http_json("POST", f"{server_url}/complete_task", {
                "summary": f"Stopped: same action repeated {SAME_ACTION_LIMIT} times without progress.",
                "final_observation": {"kind": "anomaly", "details": {"repeated_action": action_key}},
            })
            return

        # Scroll spiral guard
        cur_type = action_payload.get("type", "")
        if cur_type == "scroll" and last_action_type == "scroll":
            scroll_streak += 1
        elif cur_type == "scroll":
            scroll_streak = 1
        else:
            scroll_streak = 0
        last_action_type = cur_type

        if scroll_streak >= SCROLL_STREAK_LIMIT:
            log.warning("scroll spiral: %d consecutive scrolls — stopping", scroll_streak)
            _http_json("POST", f"{server_url}/complete_task", {
                "summary": f"Stopped: scroll spiral ({scroll_streak} consecutive scrolls).",
                "final_observation": {"kind": "anomaly", "details": {"scroll_streak": scroll_streak}},
            })
            return

        # Propose action
        code, propose_resp = _http_json(
            "POST", f"{server_url}/propose_action",
            {"action": action_payload, "screenshot_b64": ss_b64},
            timeout=10.0,
        )
        if code == 429:
            msg = (propose_resp or {}).get("message", "policy violation")
            log.warning("server policy_violation: %s", msg)
            continue
        if code != 200:
            err = (propose_resp or {}).get("error", f"server returned {code}")
            log.warning("propose_action rejected: %s", err)
            continue

        # Wait for human decision
        try:
            final_state, _ = wait_for_decision(server_url, poll_interval)
        except TimeoutError:
            log.warning("decision wait timed out")
            continue

        if final_state.get("phase") == "IDLE":
            log.info("task stopped during decision wait")
            return

    # Hard turn cap
    log.warning("hit max_turns=%d", max_turns)
    _http_json("POST", f"{server_url}/complete_task", {
        "summary": f"Reached per-task turn cap ({max_turns}) without finishing.",
        "final_observation": {"kind": "anomaly", "details": {"reason": "max_turns"}},
    })


# ── Polling loop ─────────────────────────────────────────────────────
def load_system_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def main() -> int:
    if not HOLO3_API_KEY:
        print("ERROR: HOLO3_API_KEY not set", file=sys.stderr)
        return 2
    if not HOLO3_API_BASE_URL:
        print("ERROR: HOLO3_API_BASE_URL not set", file=sys.stderr)
        return 2

    try:
        system_prompt = load_system_prompt(SYSTEM_PROMPT_PATH)
    except OSError as exc:
        print(f"ERROR: cannot read system prompt at {SYSTEM_PROMPT_PATH}: {exc}", file=sys.stderr)
        return 2

    print(
        f"\nHolo3 brain → {OBSERVER_URL}  model={HOLO3_API_MODEL}  max_turns={MAX_TURNS}",
        flush=True,
    )
    print(f"System prompt: {len(system_prompt)} chars from {SYSTEM_PROMPT_PATH}\n", flush=True)

    while True:
        try:
            code, body = _http_json("GET", f"{OBSERVER_URL}/pending_chat", timeout=3.0)
        except urllib.error.URLError as exc:
            print(f"  poll failed: {exc} — retrying", file=sys.stderr, flush=True)
            time.sleep(TASK_POLL_INTERVAL)
            continue

        if code == 200 and body is not None and not body.get("claimed"):
            try:
                run_task(body, OBSERVER_URL, MAX_TURNS, POLL_INTERVAL, system_prompt)
            except Exception as exc:
                print(f"  task loop crashed: {exc!r}", file=sys.stderr, flush=True)
                try:
                    _http_json("POST", f"{OBSERVER_URL}/complete_task", {
                        "summary": f"Internal error: {exc}. Task aborted.",
                        "final_observation": {
                            "kind": "anomaly",
                            "details": {"error": str(exc), "type": type(exc).__name__},
                        },
                    })
                except Exception:
                    pass
            continue

        time.sleep(TASK_POLL_INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
