"""OpenCUA brain for ERPBench — JSON-in-text mode.

Model-size agnostic: the same brain drives any OpenCUA variant (7B / 32B / 72B).
The specific model is selected purely by the OPENCUA_MODEL env var (e.g.
opencua-7b or opencua-32b) — no code change is needed to switch sizes.

Drop-in alternative to qwen_brain.py.
Selected at boot via the BRAIN_PROVIDER env var (see start_brain.sh). Same Flask
state machine, same inspector, same UI, same /pending_chat + /pending_ask +
/propose_action + /decide + /respond + /complete_task + /ask_human endpoint
contract — just swaps the planner from Qwen to OpenCUA.

Architectural notes:

  OpenCUA uses a Qwen2.5-VL backbone and is trained with a Reflective
  Long-CoT pipeline (L3: visual observations → L2: error analysis/replanning →
  L1: executable action). The models are self-hosted with vLLM and accessed via
  an OpenAI-compatible chat completions API.

  1. Coordinate grid: OpenCUA outputs approximate absolute pixel coordinates
     (empirically confirmed from container logs: for "click the first customer
     row" the model emits (375,236), which is the pixel location of that row on
     the 1280x720 frame; the 0-1000 reading (480,170) lands in the filter bar
     and misses). norm_to_px() clamps to screen bounds without scaling;
     px_to_norm() is identity.

  2. API format: OpenAI-compatible chat completions with image_url vision blocks
     (same as qwen_brain.py). Tool calls are NOT enabled — same JSON-in-text
     protocol as qwen_brain.py. The system prompt (system_prompt_opencua.md)
     instructs OpenCUA to emit exactly one JSON object per turn.

  3. Auth: Bearer token via OPENCUA_API_KEY env var.

  Everything else — JSON-in-text protocol, action shapes, loop-breaker, miss-
  detection, stateless task loop — is identical to qwen_brain.py.

Env vars:
  OPENCUA_API_KEY              required
  OPENCUA_BASE_URL             required (OpenAI-compatible endpoint serving OpenCUA)
  OPENCUA_MODEL                default opencua-32b
  OBSERVER_URL                 default http://localhost:5051
  SYSTEM_PROMPT_PATH           default /app/observer/system_prompt_opencua.md
  POLL_INTERVAL                default 0.5
  TASK_POLL_INTERVAL           default 1.0
  MAX_TURNS_PER_TASK           default 50
"""

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, RateLimitError, BadRequestError
from PIL import Image, ImageDraw

import inspector


DEFAULT_BASE_URL = ""
# Fallback only — overridden by the OPENCUA_MODEL env var (e.g. opencua-7b).
DEFAULT_MODEL = "opencua-32b"
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_TASK_POLL_INTERVAL = 1.0
DEFAULT_MAX_TURNS_PER_TASK = 50
DEFAULT_SYSTEM_PROMPT_PATH = "/app/observer/system_prompt_opencua.md"
MAX_OUTPUT_TOKENS = 2048  # OpenCUA uses reflective Long-CoT (L3→L2→L1); 768 truncates reasoning before the action JSON.
HISTORY_TURNS = 20

SCREEN_W = 1280
SCREEN_H = 720

# Qwen2.5-VL native coordinate grid. OpenCUA is built on the Qwen2.5-VL base
# and emits 0-1000 normalized coords. We accept 0-1000 from the model, then
# convert to absolute pixels here before forwarding to the server.
NORM_GRID = 1000


def norm_to_px(n_x: int, n_y: int) -> tuple[int, int]:
    """OpenCUA outputs approximate pixel coordinates directly (not 0-1000
    fractions). Clamp to screen bounds and return as-is."""
    return (max(0, min(SCREEN_W - 1, round(n_x))),
            max(0, min(SCREEN_H - 1, round(n_y))))


def px_to_norm(x: int, y: int) -> tuple[int, int]:
    """OpenCUA's coordinate frame is screen pixels; no conversion needed.
    Returns the coordinates unchanged so history and cursor reports stay
    in the same frame the model uses."""
    return (round(x), round(y))

# Allowed observation kinds (used by Ask mode's respond_to_human shape and by
# complete_task's optional final_observation).
ALLOWED_OBSERVATION_KINDS = {"state_summary", "knowledge_extract", "anomaly", "workflow_event"}
ALLOWED_CONFIDENCE_LEVELS = {"high", "medium", "low"}


log = logging.getLogger("opencua_brain")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


# ── Accepted JSON shapes ────────────────────────────────────────────
# Four shapes match the claude_brain.py tool schemas, validated
# client-side here since the endpoint can't enforce them via OpenAI tools.
ALLOWED_TOOL_NAMES = {"propose_action", "complete_task", "ask_human", "respond_to_human", "announce_plan"}

ALLOWED_ACTION_TYPES = {
    "move_to", "scroll", "click", "type", "select_option",
    "set_date", "clear_field", "key", "navigate_url",
}

# 3-tier risk taxonomy (safe / commit / irreversible). OpenCUA is never asked
# to self-classify — see infer_category() below.
ALLOWED_CATEGORIES = {"safe", "commit", "irreversible"}

ALLOWED_KEYS = {
    "tab", "enter", "return", "escape", "esc", "backspace", "delete",
    # Keyboard chords — the server splits on "+" and calls pyautogui.hotkey.
    # ctrl+s is the primary Save shortcut on ERPNext (Frappe intercepts it).
    # ctrl+a is a select-all fallback if clear_field ever fails to focus.
    "ctrl+s", "ctrl+a",
}

# ── Category inference (always computed, never model-supplied) ──────
# The vLLM endpoint can't enforce structured output (see module
# docstring), so OpenCUA is not asked for `category` at all — same as
# uitars_brain.py's infer_click_risk(), which classifies every UI-TARS
# click this way since that model never attempts self-classification
# either. Order matters: irreversible > commit > safe-default.
_COMMIT_CUES = ("save", "update")
_IRREVERSIBLE_CUES = (
    "submit", "delete", "discard", "cancel", "print", "email", "send",
    "disable", "enable", "activate", "approve", "reject", "status",
)
# Nav cues — used to tag click actions with button_role="navigate" for
# clicks like "+ Add Lead" or sidebar items. Ported from uitars_brain.py's
# infer_click_risk(). "create" is deliberately NOT here — it's ambiguous
# (clicking "Add" to create a form vs. "create" meaning save the record).
_NAV_CUES = ("new ", "add ", "open ", "back ", "navigate", "list ", "menu",
             "sidebar", "breadcrumb", " tab", "link ", "go to")


def infer_category(atype: str, obj: dict) -> str:
    """Risk classification from cue words in the model's own `rationale`.

    Mirrors the taxonomy table in system_prompt_opencua.md. Defaults to
    `safe`, matching the table's own default bucket for everything not
    explicitly commit/irreversible. `atype` is accepted for symmetry with
    callers but unused — every action type reduces to the same rationale
    scan. Companion `infer_button_role()` runs alongside for click actions.
    """
    rationale = (obj.get("rationale") or "").lower()
    if any(c in rationale for c in _IRREVERSIBLE_CUES):
        return "irreversible"
    if any(c in rationale for c in _COMMIT_CUES):
        return "commit"
    return "safe"


def infer_button_role(atype: str, obj: dict) -> str | None:
    """Return "navigate" for clearly-navigational clicks, None otherwise.

    Only applies to click actions — marks a click as navigation (e.g.
    clicking "+ Add Lead" or a sidebar item that lives near Save/Submit).
    Never overrides commit/irreversible cues — a click classified as
    commit/irreversible on rationale grounds stays that way (safe direction).
    Ported from uitars_brain.py's infer_click_risk().
    """
    if atype != "click":
        return None
    rationale = (obj.get("rationale") or "").lower()
    if any(c in rationale for c in _IRREVERSIBLE_CUES):
        return None
    if any(c in rationale for c in _COMMIT_CUES):
        return None
    if any(c in rationale for c in _NAV_CUES):
        return "navigate"
    return None


# ── Robust JSON-in-text extraction ──────────────────────────────────
_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def _find_first_balanced_object(text: str):
    """Return the first balanced top-level {...} substring or None.

    Skips over string literals so braces inside strings don't confuse the
    matcher. Permissive — extra prose before/after the block is fine.
    """
    depth = 0
    start = -1
    in_string = False
    str_delim = None
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
                continue
            if ch == str_delim:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            str_delim = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return text[start : i + 1]
    return None


def parse_qwen_response(content: str):
    """Extract a tool-call dict from OpenCUA's plain-text reply.

    Returns:
      dict with "tool" key + shape-specific fields on success
      None if no valid JSON object found OR validation fails

    Strategy: try fenced code block first (cleanest), then fall back to
    finding the first balanced {...} block anywhere in the text.
    """
    if not content:
        return None

    candidates = []
    for m in _CODE_FENCE_RE.finditer(content):
        candidates.append(m.group(1).strip())
    candidates.append(content)  # also try the whole text in case no fences

    for cand in candidates:
        block = _find_first_balanced_object(cand) or cand.strip()
        # Fix a common OpenCUA malformation: unquoted key with = instead of :
        # e.g.  "x": 375, y=234,  →  "x": 375, "y": 234,
        block = re.sub(r'(?<=[,{])\s*([a-z_]\w*)\s*=\s*(-?\d+)', r' "\1": \2', block)
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        tool = obj.get("tool")
        if tool in ALLOWED_TOOL_NAMES:
            return obj
        # Common mistake: promoting an action_type (e.g. "navigate_url")
        # to the top-level "tool" slot instead of nesting it inside
        # propose_action — {"tool": "navigate_url", "url": "..."} instead of
        # {"tool": "propose_action", "action_type": "navigate_url", ...}.
        # Re-wrap rather than discard; validate_action_dict still enforces
        # the real requirements (rationale, required fields) afterward.
        if tool in ALLOWED_ACTION_TYPES:
            rewrapped = {k: v for k, v in obj.items() if k != "tool"}
            rewrapped["tool"] = "propose_action"
            rewrapped["action_type"] = tool
            return rewrapped
    return None


def validate_action_dict(obj: dict):
    """Return (ok: bool, reason: str). Caller decides what to do on !ok."""
    tool = obj.get("tool")
    if tool == "propose_action":
        # Accept either "action_type" (system-prompt spec) or "type"
        # (server's internal field name). OpenCUA sometimes echoes the latter.
        atype = obj.get("action_type") or obj.get("type")
        if atype not in ALLOWED_ACTION_TYPES:
            return False, f"unknown action_type: {atype!r}"
        if not obj.get("rationale"):
            return False, "missing rationale"
        # category and button_role are never requested from the model —
        # always computed from the rationale text.
        obj["category"] = infer_category(atype, obj)
        role = infer_button_role(atype, obj)
        if role:
            obj["button_role"] = role
        # Optional plan_step field — must be a positive integer if present.
        # Range against plan length is checked in run_task where plan_steps
        # is in scope; here we just enforce the type.
        if "plan_step" in obj:
            ps = obj.get("plan_step")
            if not isinstance(ps, int) or ps < 1:
                return False, f"plan_step must be a positive integer, got {ps!r}"
        # Per-action-type required fields:
        if atype in ("move_to", "click"):
            if not isinstance(obj.get("x"), int) or not isinstance(obj.get("y"), int):
                return False, f"{atype} requires integer x and y"
            x, y = obj["x"], obj["y"]
            if not (0 <= x <= SCREEN_W and 0 <= y <= SCREEN_H):
                return False, (
                    f"{atype} coords ({x},{y}) outside screen bounds "
                    f"(x: 0-{SCREEN_W}, y: 0-{SCREEN_H}). "
                    f"OpenCUA emits pixel coordinates in screen space."
                )
        if atype == "scroll" and not isinstance(obj.get("amount"), int):
            return False, "scroll requires integer amount"
        if atype in ("type", "select_option", "set_date"):
            if "text" not in obj and "value" not in obj:
                return False, f"{atype} requires text or value"
        if atype == "key":
            key = str(obj.get("key", "")).lower()
            if key not in ALLOWED_KEYS:
                return False, f"key {key!r} not allowed"
        if atype == "navigate_url" and not obj.get("url"):
            return False, "navigate_url requires url"
        return True, "ok"
    if tool == "complete_task":
        if not obj.get("summary"):
            return False, "complete_task requires summary"
        return True, "ok"
    if tool == "ask_human":
        if not obj.get("question"):
            return False, "ask_human requires question"
        return True, "ok"
    if tool == "respond_to_human":
        # Ask mode answer + structured observation.
        if not obj.get("chat_reply"):
            return False, "respond_to_human requires chat_reply"
        obs = obj.get("observation")
        if not isinstance(obs, dict):
            return False, "respond_to_human requires observation object"
        kind = obs.get("kind")
        if kind not in ALLOWED_OBSERVATION_KINDS:
            return False, f"observation.kind invalid: {kind!r}"
        if not obs.get("summary"):
            return False, "observation.summary required"
        conf = obs.get("confidence")
        if conf not in ALLOWED_CONFIDENCE_LEVELS:
            return False, f"observation.confidence invalid: {conf!r}"
        return True, "ok"
    if tool == "announce_plan":
        # Emitted once on the first turn of a new task. Coarse milestones,
        # not individual clicks. See "Announce your plan first" in
        # system_prompt_opencua.md.
        if not obj.get("summary"):
            return False, "announce_plan requires summary"
        steps = obj.get("steps")
        if not isinstance(steps, list) or not steps:
            return False, "announce_plan requires non-empty steps list"
        if not all(isinstance(s, str) and s.strip() for s in steps):
            return False, "announce_plan steps must be non-empty strings"
        return True, "ok"
    return False, f"unknown tool: {tool!r}"


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


def _now_iso() -> str:
    """UTC ISO-8601 with millisecond precision. The harness's log_grader
    parses this with fromisoformat and subtracts start from end to derive
    thinking_ms, so sub-second precision matters for short API calls."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _post_log_turn(server_url: str, task_id: str | None, step_n: int,
                   start_ts: str, end_ts: str, response, current_url: str = ""):
    """Fire-and-forget /log_turn call. Never raises.

    OpenAI SDK exposes usage as {prompt_tokens, completion_tokens} — we
    map those to the observer's {input_tokens, output_tokens} schema
    (same names claude_brain and qwen_brain use)."""
    try:
        usage = getattr(response, "usage", None)
        in_tok  = getattr(usage, "prompt_tokens", None)  if usage else None
        out_tok = getattr(usage, "completion_tokens", None) if usage else None
        _http_json("POST", f"{server_url}/log_turn", {
            "ts": start_ts,
            "task_id": task_id,
            "step_n": step_n,
            "api_call_start_ts": start_ts,
            "api_call_end_ts": end_ts,
            "input_tokens":  in_tok,
            "output_tokens": out_tok,
            "brain": "opencua",
            "current_url": current_url,
        }, timeout=3.0)
    except Exception:  # noqa: BLE001 — telemetry must never break the brain
        pass


def get_current_url() -> str:
    """Query Chromium's CDP endpoint for the active tab URL. "" on failure.

    Ported from uitars_brain.py. Gives OpenCUA ground truth to check its own
    "did navigate_url actually land?" / "did Save actually commit?" claims
    against, instead of just assuming success from the screenshot.
    """
    try:
        _, tabs = _http_json("GET", "http://localhost:9222/json/list", timeout=5.0)
        if isinstance(tabs, list):
            for tab in tabs:
                if isinstance(tab, dict) and tab.get("type") == "page":
                    return tab.get("url", "")
    except Exception:  # noqa: BLE001
        pass
    return ""


# ── Screenshot + cursor overlay ─────────────────────────────────────
def take_annotated_screenshot():
    """Capture screen + overlay red crosshair at cursor.

    Returns (annotated_b64_png, (x, y), raw_b64_png). The raw (pre-overlay)
    bytes are for miss-detection hashing — the crosshair is drawn at the
    live cursor position, which shifts turn to turn even when the page
    content hasn't, so hashing the annotated image would false-negative on
    real "no visible effect" misses.
    """
    cursor_x, cursor_y = inspector.cursor_position()
    result = inspector.execute({"type": "screenshot"})
    if not result.get("executed"):
        log.warning("screenshot capture failed: %s", result.get("action"))
        return None, (cursor_x, cursor_y), None

    raw_b64 = result["post_screenshot_b64"]
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
    return base64.b64encode(buf.getvalue()).decode("ascii"), (cursor_x, cursor_y), raw_b64


def make_image_block(b64_png: str) -> dict:
    """OpenAI-style image block (data URI in image_url)."""
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{b64_png}"},
    }


# ── Chat + history rendering ────────────────────────────────────────
def fetch_state(server_url: str):
    code, body = _http_json("GET", f"{server_url}/status", timeout=5.0)
    if code != 200 or not body:
        return None
    return body


def format_chat_history(chat_log) -> str:
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
            # action_log stores pixel coords; render back to OpenCUA in the same
            # pixel frame the model uses (px_to_norm is identity for OpenCUA).
            n_x, n_y = px_to_norm(a["x"], a["y"])
            coords = f" @({n_x}, {n_y})"
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
            line += "  ← NOT EXECUTED"
        lines.append(line)
    return "\n".join(lines)


# ── Wait helpers ────────────────────────────────────────────────────
def wait_for_decision(server_url: str, poll_interval: float, timeout: float = 600.0):
    deadline = time.time() + timeout
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
            if alog:
                return state, alog[-1]
            return state, None
        time.sleep(poll_interval)
    raise TimeoutError("decision wait timed out")


def wait_for_chat(server_url: str, poll_interval: float, timeout: float = 1800.0):
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


# ── Loop-breaker ──────────────────────────────────────────────────────
# Ported from uitars_brain.py's action_signature(). Clicks bucket to ~15px
# so near-identical re-clicks collapse to the same signature — exactly the
# repeated-click pattern seen when OpenCUA's grounding misses the same target
# over and over. Scrolls return a coarse direction-only signature so a
# "scroll-spiral" (10+ same-direction scrolls with no other action) can be
# detected separately with a higher threshold — see run_task's consec-scroll
# guard. move_to is legitimately repeatable and skips guarding entirely.
def action_signature(action_payload: dict):
    """Stable signature for loop detection (post pixel-conversion), or None to skip."""
    t = action_payload.get("type")
    if t in ("click", "click_button", "click_field", "click_dropdown", "click_link"):
        x, y = action_payload.get("x"), action_payload.get("y")
        if x is None or y is None:
            return None
        return ("click", round(x / 15), round(y / 15))
    if t == "type":
        return ("type", (action_payload.get("text") or "").strip().lower())
    if t == "key":
        return ("key", action_payload.get("key"))
    if t == "scroll":
        amt = action_payload.get("amount", 0)
        direction = "down" if amt < 0 else ("up" if amt > 0 else "still")
        return ("scroll", direction)
    return None


# ── Plan announcement (one up-front model call, non-fatal on failure) ──
def run_plan(client, model, system_prompt, server_url, task_text: str,
             task_id: str | None = None):
    """Ask OpenCUA for a coarse plan before the action loop begins.

    Returns (summary, steps). Best-effort — any failure (bad JSON, API
    error, empty steps) is logged and returns ("", []), and run_task
    proceeds without a plan. Mirrors uitars_brain.py's make_plan() shape
    but uses OpenCUA's JSON-in-text protocol (announce_plan tool shape).

    Doesn't count against max_turns — this is a one-shot up-front call.
    """
    screenshot_b64_val, cursor_xy, _ = take_annotated_screenshot()
    plan_text = "\n".join([
        "MODE: DO — PLAN ANNOUNCEMENT (single-shot before actions begin).",
        "",
        f"TASK: {task_text}",
        "",
        "Give a coarse plan: 3-5 milestones (one per record or logical",
        "sub-goal, NOT individual clicks). Reply with EXACTLY one JSON",
        "object of shape:",
        "",
        '  {"tool": "announce_plan",',
        '   "summary": "<one line — the overall goal>",',
        '   "steps": ["<milestone 1>", "<milestone 2>", ...]}',
        "",
        "No prose, no code fence. Milestones are coarse (\"Fill customer",
        "details\" — good) not per-click (\"Click First Name field\" — bad).",
    ])
    content = []
    if screenshot_b64_val:
        content.append(make_image_block(screenshot_b64_val))
    content.append({"type": "text", "text": plan_text})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    start_ts = _now_iso()
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=messages,
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001 — plan is optional, never block the task
        log.warning("plan announcement failed (non-fatal): %r", exc)
        return "", []
    end_ts = _now_iso()
    _post_log_turn(server_url, task_id, 0, start_ts, end_ts, response)

    raw = (response.choices[0].message.content or "")
    log.info("plan raw (%d chars): %r", len(raw), raw[:1200])
    parsed = parse_qwen_response(raw)
    if parsed is None or parsed.get("tool") != "announce_plan":
        log.info("plan: model did not produce announce_plan; skipping plan card. raw=%r", raw[:200])
        return "", []
    ok, reason = validate_action_dict(parsed)
    if not ok:
        log.info("plan: validation failed: %s", reason)
        return "", []
    summary = (parsed.get("summary") or "").strip()
    steps = [s.strip() for s in parsed.get("steps", []) if isinstance(s, str) and s.strip()]
    if not summary or not steps:
        return "", []
    steps = steps[:7]  # cap length — UI expects short lists
    _http_json("POST", f"{server_url}/announce_plan",
               {"summary": summary, "steps": steps})
    log.info("plan announced: %d steps — %r", len(steps), summary[:80])
    return summary, steps


# ── Task loop (STATELESS) ───────────────────────────────────────────
def run_task(client, model, system_prompt, server_url, task, max_turns: int, poll_interval: float):
    """Per-task loop, STATELESS — rebuilds messages from scratch each turn.

    Why stateless: the vLLM deployment is configured with a finite
    context window. Stateful conversation mode accumulates ~500-1000 tokens
    per approved action; with a 1280×720 screenshot eating ~4500 tokens and
    a ~1300-token system prompt, we'd burst the cap after a few actions.
    Stateless keeps per-turn input ~constant.

    What's NOT lost: OpenCUA still sees its full past via the action history
    text snapshot (server-maintained) and the chat history. The model has
    full visibility into what it proposed, what was approved/rejected,
    and any human chats. It just doesn't carry that as native assistant
    messages in its context.

    State threaded forward across turns:
      - `pending_notes` — short notes for OpenCUA's next reply: parse/validate
        errors, loop-breaker warnings, miss-detection flags. Rendered once,
        then cleared.
      - `sig_counts` / miss-detection hash — loop-breaker and no-visible-
        effect detection, both ported from uitars_brain.py (see
        action_signature() and the module docstring).
    """
    task_id = task["task_id"]
    task_text = task["text"]
    log.info("task loop started (stateless mode): id=%s text=%r", task_id, task_text)

    # One up-front plan announcement. Non-fatal — plan_steps=[] means we
    # just proceed without a plan card / plan_step tagging.
    _, plan_steps = run_plan(client, model, system_prompt, server_url, task_text,
                             task_id=task_id)

    consecutive_parse_failures = 0
    MAX_PARSE_FAILURES = 3
    pending_notes: list[str] = []  # rendered into the next prompt, then cleared

    sig_counts: dict = {}          # loop-breaker: repeat count per action signature
    last_sig = None                # previous turn's signature (for consec-scroll guard)
    consec_scroll = 0              # consecutive same-direction scrolls with no other action
    last_pre_hash: str | None = None   # screenshot hash BEFORE the last approved action
    last_executed = False              # was the last proposal approved + executed?
    last_url: str | None = None        # previous turn's URL for navigation-change detection

    for turn in range(max_turns):
        # Honour pause and external stop.
        state = fetch_state(server_url) or {}
        if state.get("paused"):
            log.info("paused — waiting for resume")
            state = wait_unpaused(server_url, poll_interval)
        if state.get("phase") in ("IDLE", "STOPPED"):
            log.info("task ended externally; phase=%s", state.get("phase"))
            return

        # ── Build the request from scratch ────────────────────────
        screenshot_b64, cursor_xy, raw_b64 = take_annotated_screenshot()
        current_url = get_current_url()

        # Reset loop-breaker on page navigation so a click at y≈232 in a list
        # doesn't count against a click at y≈232 on the subsequent form page.
        if last_url is not None and current_url and current_url != last_url:
            sig_counts.clear()
            consec_scroll = 0
            log.info("URL changed %s → %s; reset loop-breaker", last_url, current_url)
        last_url = current_url or last_url
        chat_history = format_chat_history(state.get("chat_log", []))
        action_history = format_action_history(state.get("action_log", []))

        # Miss-detection: if the last approved action left the screen
        # pixel-identical, tell OpenCUA so it stops re-proposing a click that
        # isn't landing instead of burning turns guessing blind.
        cur_hash = hashlib.md5(raw_b64.encode("ascii"), usedforsecurity=False).hexdigest() if raw_b64 else None
        if last_executed and last_pre_hash and cur_hash == last_pre_hash:
            pending_notes.append(
                "Your last action had NO visible effect on the screen (likely a "
                "missed click). Try a clearly different location, a different action_type, "
                "or scroll to reveal the target — don't repeat the same click."
            )
            log.info("miss-detected: previous action did not change the screen")

        user_text_lines = [
            "MODE: DO (multi-step task, one action per turn).",
            "",
            f"TASK: {task_text}",
            "",
        ]
        if plan_steps:
            user_text_lines.append(
                "Plan (ALREADY announced — do NOT emit announce_plan again in this task; "
                "set plan_step on each propose_action to the milestone this action serves):"
            )
            for i, step in enumerate(plan_steps, 1):
                user_text_lines.append(f"  {i}. {step}")
            user_text_lines.append("")
        user_text_lines.extend([
            f"Current page URL: {current_url or '(unavailable)'}",
            "",
            f"User cursor at {px_to_norm(*cursor_xy)} in screen pixels (red crosshair on the screenshot).",
            "",
            "Action history so far (this is your memory of every step):",
            action_history,
            "",
            "Chat history (including replies to any ask_human question):",
            chat_history or "(only this task statement so far)",
            "",
        ])
        if pending_notes:
            for note in pending_notes:
                user_text_lines.append(f"⚠ {note}")
            user_text_lines.append("Respond with EXACTLY one valid JSON object — no prose, no code fence.")
            user_text_lines.append("")
            pending_notes = []
        user_text_lines.append("Reply now with ONE JSON object.")
        user_text = "\n".join(user_text_lines)

        user_content = []
        if screenshot_b64:
            user_content.append(make_image_block(screenshot_b64))
        user_content.append({"type": "text", "text": user_text})

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # ── Call OpenCUA ───────────────────────────────────────────
        api_call_start_ts = _now_iso()
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                messages=messages,
                temperature=0.2,
            )
        except BadRequestError as exc:
            log.error("OpenCUA 400: %s", exc)
            _http_json("POST", f"{server_url}/ask_human", {
                "question": "I hit a 400 error from the model endpoint and can't continue. Please advise.",
                "context": str(exc)[:200],
            })
            return
        except (APIError, APIConnectionError, APITimeoutError, RateLimitError) as exc:
            log.error("OpenCUA API error (retrying in 90s): %r", exc)
            time.sleep(90)
            continue
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            log.error("unexpected OpenCUA call failure: %r", exc)
            _http_json("POST", f"{server_url}/ask_human", {
                "question": "Internal error — please advise.",
                "context": f"{type(exc).__name__}: {str(exc)[:200]}",
            })
            return

        api_call_end_ts = _now_iso()
        _post_log_turn(server_url, task_id, turn + 1, api_call_start_ts, api_call_end_ts,
                       response, current_url=current_url)

        content = (response.choices[0].message.content or "")
        log.info("model raw (turn %d, %d chars): %r", turn, len(content), content[:1200])

        # ── Parse JSON-in-text ────────────────────────────────────
        parsed = parse_qwen_response(content)
        if parsed is None:
            consecutive_parse_failures += 1
            log.warning("parse failure %d/%d. raw: %r",
                        consecutive_parse_failures, MAX_PARSE_FAILURES, content[:300])
            if consecutive_parse_failures >= MAX_PARSE_FAILURES:
                _http_json("POST", f"{server_url}/ask_human", {
                    "question": "I'm having trouble producing valid JSON for my next action. Please advise.",
                    "context": f"Got {consecutive_parse_failures} unparseable replies in a row.",
                })
                try:
                    new_state = wait_for_chat(server_url, poll_interval)
                except TimeoutError:
                    return
                if new_state.get("phase") == "IDLE":
                    return
                consecutive_parse_failures = 0
            else:
                pending_notes.append("Your previous response did not contain a parseable JSON object.")
            continue

        consecutive_parse_failures = 0

        # ── Validate ──────────────────────────────────────────────
        ok, reason = validate_action_dict(parsed)
        if not ok:
            log.warning("validation failure: %s. parsed: %s", reason, parsed)
            pending_notes.append(f"Your previous JSON failed validation: {reason}")
            continue

        tool = parsed["tool"]

        # ── Dispatch ──────────────────────────────────────────────
        if tool == "complete_task":
            log.info("complete_task: %r", (parsed.get("summary") or "")[:80])
            _http_json("POST", f"{server_url}/complete_task", {
                "summary": parsed.get("summary", ""),
                "final_observation": parsed.get("final_observation"),
            })
            return

        if tool == "ask_human":
            log.info("ask_human: %r", (parsed.get("question") or "")[:80])
            _http_json("POST", f"{server_url}/ask_human", {
                "question": parsed.get("question", ""),
                "context": parsed.get("context", ""),
            })
            try:
                new_state = wait_for_chat(server_url, poll_interval)
            except TimeoutError:
                log.warning("ask_human wait timed out; ending task")
                return
            if new_state.get("phase") == "IDLE":
                return
            continue

        if tool == "announce_plan":
            # The plan is normally announced via the up-front run_plan() call
            # before the loop starts. OpenCUA doesn't remember that (separate
            # conversation), so it can re-emit announce_plan in early turns.
            if plan_steps:
                log.info("OpenCUA re-emitted announce_plan mid-task; plan already exists — nudging back to actions.")
                pending_notes.append(
                    "Your plan has already been announced (shown in the prompt above). "
                    "Do NOT emit announce_plan again in this task. Your next reply must be "
                    "propose_action, complete_task, or ask_human."
                )
            else:
                # Late plan announcement — up-front run_plan() must have failed
                # or been skipped by the model. Accept it and continue.
                summary = (parsed.get("summary") or "").strip()
                steps = [s.strip() for s in parsed.get("steps", [])
                         if isinstance(s, str) and s.strip()][:7]
                if summary and steps:
                    _http_json("POST", f"{server_url}/announce_plan",
                               {"summary": summary, "steps": steps})
                    plan_steps = steps
                    log.info("plan announced mid-loop: %d steps", len(steps))
            continue

        # tool == "propose_action"
        # Normalise: server expects "type"; the model may have emitted
        # "action_type" (per system prompt) OR "type" (echoing the server's
        # internal name). Defensively handle either and fail soft if absent.
        action_payload = {k: v for k, v in parsed.items() if k != "tool"}

        # Drop plan_step if out of range (validate_action_dict enforces
        # positive-int only; we clamp against plan_steps here where it's
        # in scope). Also drop it if we have no plan at all — the server
        # would just ignore it, but keeping the payload clean is nicer.
        if "plan_step" in action_payload:
            ps = action_payload.get("plan_step")
            if not plan_steps or not isinstance(ps, int) or ps < 1 or ps > len(plan_steps):
                action_payload.pop("plan_step", None)
        if "action_type" in action_payload and "type" not in action_payload:
            action_payload["type"] = action_payload.pop("action_type")
        elif "action_type" in action_payload:
            # Both keys present — keep "type", drop redundant "action_type".
            action_payload.pop("action_type", None)
        if "type" not in action_payload:
            log.warning("propose_action missing action_type/type; parsed=%r", parsed)
            pending_notes.append(
                "Your propose_action JSON must include 'action_type' "
                "(e.g. \"action_type\": \"click\"). Resend a valid object."
            )
            continue

        # The model's vocabulary uses a single unified "click" (see module
        # docstring); the server and inspector only
        # recognise the four legacy sub-type strings, which it treats
        # identically anyway. Translate at the wire boundary — same trick
        # uitars_brain.py uses, always sending "click_button" for every click.
        if action_payload["type"] == "click":
            action_payload["type"] = "click_button"
        label = action_payload.pop("label", None)
        if label:
            action_payload["button_label"] = label

        # OpenCUA emits approximate pixel coordinates directly (not 0-1000
        # fractions). norm_to_px clamps them to screen bounds. Preserve the
        # raw model values as x_norm/y_norm for audit.
        if isinstance(action_payload.get("x"), int) and isinstance(action_payload.get("y"), int):
            n_x, n_y = action_payload["x"], action_payload["y"]
            px_x, px_y = norm_to_px(n_x, n_y)
            action_payload["x"] = px_x
            action_payload["y"] = px_y
            action_payload["x_norm"] = n_x
            action_payload["y_norm"] = n_y

        # Loop-breaker: stop OpenCUA re-proposing the same (likely mis-grounded)
        # action in a cycle. Two separate guards:
        #   1. Click/type/key — cumulative count across the run. 1st occurrence
        #      sends normally; 2nd is withheld with a warning; 3rd hard-stops.
        #      Rationale: repeating the exact same click at the same pixel is
        #      ~always mis-grounding, regardless of what happened between.
        #   2. Scroll — consecutive same-direction count only (resets on any
        #      non-scroll action). Threshold: warn on 4th consecutive, hard-stop
        #      on 6th. Rationale: legitimate scrolling to reveal a below-fold
        #      field takes 2-3 scrolls; a "scroll-spiral" (10+ scrolls with no
        #      interleaved action) means OpenCUA is stuck and the cumulative
        #      counter would false-positive on tasks that need real scrolling.
        sig = action_signature(action_payload)
        if sig is not None:
            is_scroll = sig[0] == "scroll"
            if is_scroll:
                consec_scroll = consec_scroll + 1 if sig == last_sig else 1
                if consec_scroll >= 6:
                    _http_json("POST", f"{server_url}/complete_task", {
                        "summary": "Stuck scrolling — no field interaction between scrolls; "
                                   "stopped to avoid burning turns. A human can take over.",
                        "final_observation": {"kind": "anomaly",
                                              "details": {"reason": "scroll-spiral",
                                                          "consecutive": consec_scroll}},
                    })
                    log.warning("scroll-loop-breaker: hard stop on %d consecutive scrolls", consec_scroll)
                    return
                if consec_scroll == 4:
                    pending_notes.append(
                        f"You've scrolled {consec_scroll} times in a row in the same direction "
                        "with no other action. STOP scrolling now — either you've passed your "
                        "target (try scrolling the OPPOSITE direction), the target isn't on this "
                        "page (try navigate_url to a different URL), or you need key: tab from a "
                        "focused field. If you genuinely can't locate the target, call ask_human."
                    )
                    log.warning("scroll-loop-breaker: soft warn on %d consecutive scrolls", consec_scroll)
                    last_sig = sig
                    continue
                last_sig = sig
                # fall through — scroll is allowed to dispatch normally
            else:
                # Non-scroll action: reset consec-scroll counter and use the
                # cumulative click/type/key guard.
                consec_scroll = 0
                last_sig = sig
                prior = sig_counts.get(sig, 0)
                if prior >= 2:
                    _http_json("POST", f"{server_url}/complete_task", {
                        "summary": "Stuck repeating the same action, stopped to avoid looping. "
                                   "A human can take over and continue, or resend the task.",
                        "final_observation": {"kind": "anomaly",
                                              "details": {"reason": "loop-breaker", "signature": str(sig)}},
                    })
                    log.warning("loop-breaker: hard stop on repeated action %s", sig)
                    return
                sig_counts[sig] = prior + 1
                if prior == 1:
                    pending_notes.append(
                        f"You already proposed this exact action ({action_payload.get('type')} at "
                        f"~{action_payload.get('x_norm')},{action_payload.get('y_norm')}) and it didn't "
                        "move the task forward. Do NOT repeat it — try a different location, a different "
                        "action_type, or call ask_human."
                    )
                    log.warning("loop-breaker: soft warn on repeated action %s", sig)
                    continue

        # Fresh screenshot for the audit hash.
        ss_b64, _, _ = take_annotated_screenshot()

        code, propose_resp = _http_json(
            "POST", f"{server_url}/propose_action",
            {"action": action_payload, "screenshot_b64": ss_b64},
            timeout=10.0,
        )
        if code == 429:
            msg_txt = (propose_resp or {}).get("message", "policy violation")
            log.warning("server policy_violation: %s", msg_txt)
            pending_notes.append(
                f"Server rejected your proposal: {msg_txt}. "
                "Call ask_human or complete_task next; do not propose the same action."
            )
            continue
        if code != 200:
            err = (propose_resp or {}).get("error", f"server returned {code}")
            log.warning("propose_action rejected by server: %s", err)
            pending_notes.append(f"Server rejected proposal: {err}. Try a different action.")
            continue

        if propose_resp.get("server_upgraded"):
            log.info("server upgraded category to %s", propose_resp.get("category_final"))

        # Wait for human decision. The action_history on the NEXT turn will
        # already reflect this approve/reject — no in-memory bookkeeping needed.
        try:
            final_state, last_action = wait_for_decision(server_url, poll_interval)
        except TimeoutError:
            log.warning("decision wait timed out — ending task")
            return
        if final_state.get("phase") == "IDLE":
            log.info("task stopped during decision wait")
            return

        # Miss-detection bookkeeping: remember whether THIS turn's action
        # was approved+executed, and the pre-action hash, for comparison at
        # the top of the next turn.
        decision = (last_action or {}).get("decision")
        approved = decision == "approve" and bool((last_action or {}).get("executed"))
        last_pre_hash = cur_hash if approved else None
        last_executed = approved
        # Continue to next turn. The fresh screenshot + updated action_history
        # at the top of the loop will tell OpenCUA what happened.

    log.warning("hit max_turns (%d) — forcing complete_task", max_turns)
    _http_json("POST", f"{server_url}/complete_task", {
        "summary": "Per-task turn cap reached. Stopping for safety.",
        "final_observation": {"kind": "anomaly", "details": {"reason": "max_turns hit", "turns": max_turns}},
    })


# ── Ask-mode loop (single-round Q&A) ────────────────────────────────
def run_ask(client, model, system_prompt, server_url, ask):
    """Answer one Ask-mode question. Single round: screenshot → OpenCUA → /respond.

    Mirrors claude_brain.run_ask exactly in shape; differs in:
      - OpenAI Chat Completions API instead of Anthropic messages.create
      - No `tools` parameter (parser not enabled); model emits one JSON object
        of shape `respond_to_human` via system prompt instruction.
      - Image as OpenAI-style image_url with data URI.
    """
    question = ask.get("text", "")
    chat_log = ask.get("chat_log") or []
    log.info("ask: %r", question[:80])

    screenshot_b64, cursor_xy, _ = take_annotated_screenshot()
    history_note = format_chat_history(chat_log) if chat_log else ""

    user_text_lines = [
        f"MODE: ASK (single-shot Q&A, no actions).",
        "",
        f"User cursor at {px_to_norm(*cursor_xy)} in screen pixels (red crosshair on the screenshot).",
        f"User question: {question}",
        "",
    ]
    if history_note:
        user_text_lines.append("Recent chat history:")
        user_text_lines.append(history_note)
        user_text_lines.append("")
    user_text_lines.append(
        "Reply with EXACTLY one JSON object of shape `respond_to_human` "
        "(chat_reply + observation). No prose, no code fence."
    )
    user_text = "\n".join(user_text_lines)

    user_content = []
    if screenshot_b64:
        user_content.append(make_image_block(screenshot_b64))
    user_content.append({"type": "text", "text": user_text})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    start_ts = _now_iso()
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=messages,
            temperature=0.2,
        )
    except (BadRequestError, APIError, APIConnectionError, APITimeoutError, RateLimitError) as exc:
        log.error("OpenCUA API error during Ask: %r", exc)
        _http_json("POST", f"{server_url}/respond", {
            "chat_reply": f"Sorry — model endpoint error: {exc}",
            "observation": {
                "kind": "anomaly",
                "summary": "OpenCUA API error during Ask-mode answer",
                "confidence": "low",
                "details": {"error": str(exc)[:200], "type": type(exc).__name__},
            },
        })
        return
    end_ts = _now_iso()
    _post_log_turn(server_url, None, 1, start_ts, end_ts, response)

    content = (response.choices[0].message.content or "")
    parsed = parse_qwen_response(content)

    if parsed is None or parsed.get("tool") != "respond_to_human":
        log.warning("Ask: model did not produce respond_to_human; raw=%r", content[:300])
        # One graceful retry: re-prompt explicitly.
        retry_messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                "Your last reply was not a valid `respond_to_human` JSON object. "
                "Reply with EXACTLY one JSON object of shape "
                '{"tool":"respond_to_human","chat_reply":"...","observation":{"kind":"...","summary":"...","confidence":"..."}}. '
                "No prose."
            )},
        ]
        retry_start_ts = _now_iso()
        try:
            response = client.chat.completions.create(
                model=model, max_tokens=MAX_OUTPUT_TOKENS,
                messages=retry_messages, temperature=0.1,
            )
            retry_end_ts = _now_iso()
            _post_log_turn(server_url, None, 2, retry_start_ts, retry_end_ts, response)
            content = (response.choices[0].message.content or "")
            parsed = parse_qwen_response(content)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ask retry failed: %r", exc)
            parsed = None

    if parsed is None or parsed.get("tool") != "respond_to_human":
        # Last-resort fallback: post a low-confidence answer rather than hang.
        _http_json("POST", f"{server_url}/respond", {
            "chat_reply": content or "(model produced no parseable answer)",
            "observation": {
                "kind": "state_summary",
                "summary": "Model returned text without valid respond_to_human JSON",
                "confidence": "low",
            },
        })
        return

    ok, reason = validate_action_dict(parsed)
    if not ok:
        log.warning("Ask: validation failure: %s", reason)
        _http_json("POST", f"{server_url}/respond", {
            "chat_reply": parsed.get("chat_reply") or "(invalid observation produced)",
            "observation": {
                "kind": "anomaly",
                "summary": f"Validation failed: {reason}",
                "confidence": "low",
            },
        })
        return

    # Strip the tool key; server's /respond just wants chat_reply + observation.
    payload = {k: v for k, v in parsed.items() if k != "tool"}
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
    parser.add_argument("--model", default=os.environ.get("OPENCUA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.environ.get("OPENCUA_BASE_URL") or DEFAULT_BASE_URL)
    parser.add_argument("--max-turns", type=int,
                        default=int(os.environ.get("MAX_TURNS_PER_TASK", DEFAULT_MAX_TURNS_PER_TASK)))
    parser.add_argument("--system-prompt",
                        default=os.environ.get("SYSTEM_PROMPT_PATH", DEFAULT_SYSTEM_PROMPT_PATH))
    args = parser.parse_args()

    api_key = os.environ.get("OPENCUA_API_KEY")
    if not api_key:
        print("ERROR: OPENCUA_API_KEY not set", file=sys.stderr)
        return 2
    if not args.base_url:
        print("ERROR: OPENCUA_BASE_URL not set", file=sys.stderr)
        return 2

    try:
        system_prompt = load_system_prompt(args.system_prompt)
    except OSError as exc:
        print(f"ERROR: cannot read system prompt at {args.system_prompt}: {exc}", file=sys.stderr)
        return 2

    client = OpenAI(base_url=args.base_url, api_key=api_key)
    print(
        f"\nOpenCUA brain → observer={args.url}  endpoint={args.base_url}  "
        f"model={args.model}  max_turns={args.max_turns}",
        flush=True,
    )
    print(f"System prompt: {len(system_prompt)} chars from {args.system_prompt}", flush=True)
    print("(JSON-in-text mode — endpoint does not support OpenAI tool calls.)\n", flush=True)

    while True:
        # 1. Poll for a Do-mode task first.
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

        # 2. No Do task — check for an Ask-mode question.
        try:
            code, ask_body = _http_json("GET", f"{args.url}/pending_ask", timeout=3.0)
        except urllib.error.URLError:
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
