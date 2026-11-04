"""UI-TARS brain for ERPBench.

A drop-in alternative to claude_brain.py / qwen_brain.py. Runs INSIDE the
observer container (needs the Xvfb display to screenshot and localhost to reach
the Flask server), but offloads the actual model inference to an external
UI-TARS endpoint (hosted API, or a self-hosted model server). Flow per turn:

    screenshot (1280x720)  ->  model endpoint (prompt + image)
                           ->  parse UI-TARS "Thought / Action"
                           ->  map to the app's action dict
                           ->  POST /propose_action  (auto-approver approves in bench)
                           ->  wait for the decision, repeat

Behaviour notes:
- UI-TARS 1.5 emits ABSOLUTE pixel coordinates (no 0-1000 scaling).
- The action's risk category is inferred from the model's reasoning text
  (Save -> commit, Delete/Submit -> irreversible); clearly navigational clicks
  are tagged as navigation.
- A coarse plan is announced at task start and the model is asked to report the
  current milestone; the reported index is forwarded when present.
- Per-turn wall-clock timing is posted to /log_turn for bench metrics.
  In hosted API mode token counts come from the response usage field. A
  self-hosted /generate server reports none; they are logged as None.

Two inference backends — selected by whether UITARS_API_KEY is set:

  Hosted API (set UITARS_API_KEY to activate; used for the paper runs):
    Calls an OpenAI-compatible /chat/completions endpoint instead of the local
    server. UITARS_HOST is ignored. Token counts come from the response usage
    field and are logged normally.

  Self-hosted server (used when UITARS_API_KEY is unset):
    UITARS_HOST must serve GET /health and POST /generate. No such server is
    included in this repository. Token counts are logged as None.

Env:
  SERVER_URL            Flask server in-container (default http://localhost:5051)
  UITARS_HOST           self-hosted /generate server (default http://host.docker.internal:8077)
  MAX_TURNS_PER_TASK    per-task cap (default 50)

  UITARS_API_KEY        set to enable hosted API mode (overrides UITARS_HOST)
  UITARS_API_BASE_URL   hosted endpoint base (required in API mode)
  UITARS_API_MODEL      model name, e.g. ui-tars-72b-dpo or ui-tars-1-5-7b
  UITARS_API_FACTOR     smart-resize tile factor (default 28, Qwen2.5-VL)
  UITARS_API_MAX_PIXELS smart-resize pixel cap (default 1280*28*28 = 1 003 520)
"""
import base64
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

os.environ.setdefault("DISPLAY", ":1")
import pyautogui  # noqa: E402 — DISPLAY must be set first

SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:5051")
UITARS_HOST = os.environ.get("UITARS_HOST", "http://host.docker.internal:8077")
MAX_TURNS = int(os.environ.get("MAX_TURNS_PER_TASK", "50"))
POLL = float(os.environ.get("POLL_INTERVAL", "1.0"))
HISTORY_LINES = 15

# ── Hosted API mode ───────────────────────────────────────────────────────
# Setting UITARS_API_KEY switches from the self-hosted server to a hosted
# OpenAI-compatible endpoint. UITARS_HOST is ignored when this is set.
UITARS_API_KEY      = os.environ.get("UITARS_API_KEY", "")
UITARS_API_BASE_URL = os.environ.get("UITARS_API_BASE_URL") or ""
UITARS_API_MODEL    = os.environ.get("UITARS_API_MODEL", "ui-tars-72b-dpo")
# Qwen2.5-VL smart-resize params: the hosted model resizes internally;
# we replicate the formula to invert coords back to true screenshot pixels.
_API_FACTOR     = int(os.environ.get("UITARS_API_FACTOR",     "28"))
_API_MAX_PIXELS = int(os.environ.get("UITARS_API_MAX_PIXELS", str(1280 * 28 * 28)))
_API_MIN_PIXELS = int(os.environ.get("UITARS_API_MIN_PIXELS", str(56 * 56)))


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
_log = logging.getLogger("uitars_brain")


def log(*a):
    _log.info(" ".join(str(x) for x in a))


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 form, second precision. Matches observer_server."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── HTTP helpers ────────────────────────────────────────────────────
def http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.load(r)
    except Exception as exc:  # noqa: BLE001
        return None, {"error": str(exc)}


def http_post(url, body, timeout=180, extra_headers=None):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except Exception:  # noqa: BLE001
            return exc.code, {"error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return None, {"error": str(exc)}


def fetch_state():
    _, st = http_get(f"{SERVER_URL}/status")
    return st if isinstance(st, dict) else {}


def get_current_url() -> str:
    """Query Chromium's remote debug port for the active tab URL. Returns empty string on failure."""
    try:
        _, tabs = http_get("http://localhost:9222/json/list")
        if isinstance(tabs, list):
            for tab in tabs:
                if isinstance(tab, dict) and tab.get("type") == "page":
                    return tab.get("url", "")
    except Exception:  # noqa: BLE001
        pass
    return ""


# ── Screenshot (raw, 1280x720, no cursor annotation) ────────────────
def screenshot_b64(retries=4):
    """Capture the Xvfb screen as base64 PNG.

    pyautogui shells out to gnome-screenshot, which intermittently writes a
    corrupt/empty file (PIL.UnidentifiedImageError). Retry a few times, then
    fall back to Pillow's native X11 grab. Returns None if all attempts fail.
    """
    last_err = None
    for _ in range(retries):
        try:
            img = pyautogui.screenshot()
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:  # noqa: BLE001 — gnome-screenshot flakiness
            last_err = exc
            time.sleep(0.4)
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY", ":1"))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        log(f"screenshot failed after {retries} retries + X11 fallback: {last_err} / {exc}")
        return None


# ── UI-TARS prompt ──────────────────────────────────────────────────
ACTION_SPACE = """## Action Space
click(start_box='(x,y)')            click at absolute pixel x,y
type(content='text')                type text into the currently focused field
select_option(option='text')        select a dropdown option or tick a checkbox by its visible label
scroll(start_box='(x,y)', direction='down'|'up')
hotkey(key='enter'|'tab'|'escape'|'backspace'|'delete'|'ctrl+a')
navigate(url='http://...')          navigate the browser directly to a URL
wait()                              the screen is still loading; re-check
finished(content='summary')         the task is complete"""


def build_prompt(task, history, chat_log, plan_steps=None, current_url=""):
    hist = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(history[-HISTORY_LINES:])) or "None yet."
    url_line = f"\n## Current page URL\n{current_url}\n" if current_url else ""
    notes = ""
    if chat_log:
        humans = [t.get("text", "").strip() for t in chat_log if t.get("role") == "human"]
        humans = [h for h in humans if h and h != task]
        if humans:
            notes = "\n## User notes (follow these)\n" + "\n".join(f"- {h}" for h in humans[-5:])
    plan_block = ""
    step_line = ""
    if plan_steps:
        numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan_steps))
        plan_block = f"\n## Plan (your milestones)\n{numbered}\n"
        step_line = "Step: <the plan milestone number above that this action belongs to>\n"
    return f"""You are a GUI agent operating an ERPNext web application. Look at the \
screenshot and decide the SINGLE next action that progresses the task. Coordinates \
are ABSOLUTE PIXELS in the 1280x720 screenshot. Always click a field before typing \
into it. Do exactly one action per turn.
{url_line}

## ERPNext navigation
Screen types you may see:
- Login page: email + password fields. If you see this, type email=Administrator then password=admin and submit.
- Home/Desk: left sidebar with module names, module tiles in the centre.
- List view: a grid of records for one DocType (e.g. Customer list).
- Form view: a single record's fields. Save button is in the TOP-RIGHT action bar.

## PREFERRED way to open a specific record (use this first)
Use navigate() to go directly to the list page, then click the record:
- Customer tasks:  navigate(url='http://erpnext-frontend:8080/app/customer')
- Lead tasks:      navigate(url='http://erpnext-frontend:8080/app/lead')
- Item tasks:      navigate(url='http://erpnext-frontend:8080/app/item')
After navigating to the list, find the record name in the list and click it to open the form.

Only use sidebar clicks if navigate() is unavailable.

## Creating new records
Navigate DIRECTLY to the new-record URL — do NOT click the New button in the list view:
- New Customer:  navigate(url='http://erpnext-frontend:8080/app/customer/new-customer-1')
- New Lead:      navigate(url='http://erpnext-frontend:8080/app/lead/new-lead-1')
- New Item:      navigate(url='http://erpnext-frontend:8080/app/item/new-item-1')
- New Supplier:  navigate(url='http://erpnext-frontend:8080/app/supplier/new-supplier-1')
Fill all required fields (marked *), click Save, then verify the URL no longer contains 'new-' before calling finished().

## Navigation / sidebar tasks
If the task only asks you to open a module or a list — stay on the LIST VIEW.
Do NOT click into any individual record row.

## ERPNext Select fields and Checkboxes
A Select field has a single ▼ arrow and NO × icon (different from Link fields).
A Checkbox appears as a square box (☐ unchecked, ☑ checked).
To change a Select field: click it once to open the dropdown, then select_option(option='<exact value>').
To toggle a Checkbox: click it once. Verify the state changed before saving.
Examples:
  Industry → Manufacturing:       click the Industry field, then select_option(option='Manufacturing')
  Customer Group → Government:    click the field, then select_option(option='Government')
  Disabled checkbox (unchecked):  click the checkbox once to enable it.

## ERPNext Link fields (autocomplete/search fields)
A Link field is active when it shows × and → icons next to it. To change its value:
1. Click the field once to activate it (cursor appears, × and → icons appear).
2. Press hotkey(key='ctrl+a') to select all existing text.
3. type(content='<new value>') to type the replacement text.
4. Wait — an autocomplete dropdown will appear BELOW the field within 1-2 seconds.
5. Use hotkey(key='tab') to accept the first suggestion, or select_option(option='<value>') if a dropdown is visible.
NEVER keep clicking inside an already-active Link field — always type after step 1.

## Filling multiple fields — prefer Tab over per-field clicks

After clicking and filling the FIRST field, use hotkey(key='tab') to move focus to the next \
form field instead of clicking each one individually. Tab is guaranteed to focus the next \
field regardless of scroll position; clicking requires grounding to a small target every time.

Preferred sequence:
1. click the first field to fill.
2. type its value.
3. hotkey(key='tab') — moves focus to the next field in tab order.
4. Check the next screenshot: which field now has the cursor ▌?
5. If it's a field you need to fill → type the value. If it's one to skip → hotkey(key='tab') again.
6. Repeat until all named fields are filled, then save.

Fall back to click-per-field only when Tab lands somewhere unexpected (a modal, a dropdown \
that opens, or a non-input element gets focused).

## Rules
- **Cursor visible = type NOW.** If you can see a blinking text cursor (▌) inside a field, that field IS focused. Your very next action MUST be type(content='...') with the value. Do NOT click a field that already shows a cursor.
- To focus an empty field that has NO cursor: click it once. Then on the next turn, type into it.
- To edit a text field with EXISTING content: click it, then hotkey(key='ctrl+a') to select all, then type(content='new value'). Never start typing without clearing first.
- Clicking a tab header does NOT focus any field inside it. After switching tabs, you must still click the specific input box.
- If a dropdown needs to be opened, click its arrow or the field itself.
- If nothing is happening after an action, try scrolling or looking for the element elsewhere on the page.

## Scope — do what the task asks, nothing extra
- Fill the fields the task asks for, plus any required field (red asterisk *).
- If the task names specific fields, fill those — don't go hunting for extra optional fields. Once the task's fields are set, move on to saving.

## Before clicking Save — mandatory checks
1. Look at every field you edited and confirm it shows the NEW value, not the original.
2. If a field still shows the original value, the edit did not register — redo the edit before saving.
3. Only click Save after all edited fields show the correct new values.

## When you are done
- The task is complete once the named fields are filled AND the record is saved (a save confirmation or the saved record is visible).
- For newly created records: verify the URL changed from new-...-1 to a saved record ID (e.g. CUST-0001) before calling finished().
- The moment the record is saved, output `finished(content='<one-line summary>')` and stop. Do not keep acting.
- If the named fields are already filled and saved when you look, output `finished()` immediately.

## Do NOT repeat yourself
- Read "Actions so far" below. If an action is listed as done, it is DONE — never propose it again.
- If a field already shows the value the task wants, move on; do not re-click or re-type it.
- If unsure what's left, compare the task's named fields against what is already filled on screen, and act only on the ones still missing.

## Output Format
Thought: <brief reasoning: what is still missing, and the single next action>
{step_line}Action: <one action from the action space>

{ACTION_SPACE}
{plan_block}
## Task
{task}
{notes}

## Actions so far
{hist}

Output the next Thought and Action now."""


def build_plan_prompt(task):
    """Ask UI-TARS for a coarse, display-only plan before it starts acting."""
    return f"""You are about to operate an ERPNext web application to complete a task. \
Before acting, give a short plan: 3-5 COARSE milestones (one per record or sub-goal, \
NOT individual clicks).

## Output Format
Summary: <one line — the overall goal>
Plan:
1. <milestone>
2. <milestone>
3. <milestone>

## Task
{task}

Output the Summary and Plan now."""


def parse_plan(text):
    """Pull (summary, steps[]) out of the model's plan output. Best-effort."""
    summary = ""
    m = re.search(r"Summary:\s*(.+)", text)
    if m:
        summary = m.group(1).strip()
    steps = re.findall(r"^\s*\d+[.)]\s*(.+?)\s*$", text, re.M)
    if not steps:  # fall back to bullet lines
        steps = re.findall(r"^\s*[-*]\s*(.+?)\s*$", text, re.M)
    steps = [s.strip() for s in steps if s.strip()][:7]
    return summary, steps


def make_plan(task, img_b64):
    """One up-front model call → POST /announce_plan so the UI shows a plan card.
    Returns the parsed steps (so the per-turn loop can offer them for plan_step
    tagging). Best-effort: any failure is logged and skipped, never fatal."""
    out, _, _, _, _ = call_uitars(build_plan_prompt(task), img_b64)
    if not out:
        log("plan: no model output; skipping plan card")
        return []
    summary, steps = parse_plan(out)
    if not summary and not steps:
        log("plan: nothing parseable; skipping plan card")
        return []
    http_post(f"{SERVER_URL}/announce_plan",
              {"summary": summary or task[:120], "steps": steps})
    log(f"plan announced: {len(steps)} steps")
    return steps


def _smart_resize_api(h, w):
    """Qwen2.5-VL smart_resize — (h_bar, w_bar) the hosted model sees internally."""
    h_bar = max(_API_FACTOR, round(h / _API_FACTOR) * _API_FACTOR)
    w_bar = max(_API_FACTOR, round(w / _API_FACTOR) * _API_FACTOR)
    if h_bar * w_bar > _API_MAX_PIXELS:
        beta = math.sqrt((h * w) / _API_MAX_PIXELS)
        h_bar = max(_API_FACTOR, math.floor(h / beta / _API_FACTOR) * _API_FACTOR)
        w_bar = max(_API_FACTOR, math.floor(w / beta / _API_FACTOR) * _API_FACTOR)
    elif h_bar * w_bar < _API_MIN_PIXELS:
        beta = math.sqrt(_API_MIN_PIXELS / (h * w))
        h_bar = math.ceil(h * beta / _API_FACTOR) * _API_FACTOR
        w_bar = math.ceil(w * beta / _API_FACTOR) * _API_FACTOR
    return int(h_bar), int(w_bar)


def _call_uitars_hosted(prompt, img_b64):
    """Hosted OpenAI-compatible endpoint. Returns same 5-tuple as call_uitars."""
    from PIL import Image as _Image  # PIL already available in the container
    raw = base64.b64decode(img_b64)
    img_obj = _Image.open(io.BytesIO(raw))
    img_w, img_h = img_obj.size

    rh, rw = _smart_resize_api(img_h, img_w)
    sx = img_w / rw if rw else 1.0
    sy = img_h / rh if rh else 1.0
    log(f"[coord-space] screenshot={img_w}x{img_h}  assumed_model_space={rw}x{rh}  sx={sx:.6f} sy={sy:.6f}")

    body = {
        "model": UITARS_API_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": 1024,
    }
    url = UITARS_API_BASE_URL.rstrip("/") + "/chat/completions"
    code, resp = http_post(url, body, timeout=180,
                           extra_headers={"Authorization": f"Bearer {UITARS_API_KEY}"})
    if code != 200:
        log("hosted API error:", code, resp)
        return None, 1.0, 1.0, None, None
    try:
        text = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log("unexpected API response shape:", str(resp)[:200])
        return None, 1.0, 1.0, None, None
    usage = resp.get("usage") or {}
    in_tok  = usage.get("prompt_tokens") or None
    out_tok = usage.get("completion_tokens") or None
    return text, sx, sy, in_tok, out_tok


def call_uitars(prompt, img_b64):
    """Returns (output_text, sx, sy, input_tokens, output_tokens).

    sx/sy rescale model-emitted coords (smart-resized space) back to true pixels.
    Local mode: tokens are None (local model has no cost).
    API mode:   tokens come from the response usage field.
    """
    if UITARS_API_KEY:
        return _call_uitars_hosted(prompt, img_b64)
    # ── Self-hosted /generate server ─────────────────────────────────
    code, resp = http_post(
        f"{UITARS_HOST}/generate",
        {"prompt": prompt, "image_b64": img_b64, "max_tokens": 1024},
        timeout=180,
    )
    if code != 200:
        log("model server error:", code, resp)
        return None, 1.0, 1.0, None, None
    out = resp.get("output", "")
    space = resp.get("coord_space")   # [w, h] the model emits coords in
    size = resp.get("image_size")     # [w, h] of the real screenshot
    sx = sy = 1.0
    if space and size and space[0] and space[1]:
        sx = size[0] / space[0]
        sy = size[1] / space[1]
    return out, sx, sy, None, None


# ── Parse UI-TARS output ────────────────────────────────────────────
def parse_output(text):
    """Return (thought, action_dict). action_dict has 'kind' plus fields."""
    thought = ""
    tm = re.search(r"Thought:\s*(.+?)(?:\n\s*(?:Step|Action):|\Z)", text, re.S)
    if tm:
        thought = tm.group(1).strip()
    am = re.search(r"Action:\s*(.+)", text, re.S)
    if not am:
        return thought, None
    s = am.group(1).strip()
    nm = re.match(r"(\w+)", s)
    name = nm.group(1).lower() if nm else ""
    cm = re.search(r"\(?\s*(\d{1,4})\s*,\s*(\d{1,4})\s*\)?", s)
    x = int(cm.group(1)) if cm else None
    y = int(cm.group(2)) if cm else None

    if name in ("click", "left_single", "left_double", "double_click", "right_single"):
        return thought, {"kind": "click", "x": x, "y": y, "raw": s}
    if name == "type":
        c = re.search(r"content\s*=\s*['\"](.*)['\"]", s, re.S)
        return thought, {"kind": "type", "content": c.group(1) if c else "", "raw": s}
    if name == "scroll":
        d = re.search(r"direction\s*=\s*['\"](\w+)['\"]", s)
        return thought, {"kind": "scroll", "x": x, "y": y,
                         "direction": (d.group(1).lower() if d else "down"), "raw": s}
    if name == "hotkey":
        k = re.search(r"key\s*=\s*['\"](.*?)['\"]", s)
        return thought, {"kind": "hotkey", "key": (k.group(1).lower() if k else ""), "raw": s}
    if name in ("finished", "done", "complete"):
        c = re.search(r"content\s*=\s*['\"](.*)['\"]", s, re.S)
        return thought, {"kind": "finished", "content": c.group(1) if c else "", "raw": s}
    if name in ("wait", "call_user"):
        return thought, {"kind": name, "raw": s}
    if name == "navigate":
        u = re.search(r"url\s*=\s*['\"]([^'\"]+)['\"]", s)
        return thought, {"kind": "navigate", "url": u.group(1) if u else "", "raw": s}
    if name == "select_option":
        o = re.search(r"option\s*=\s*['\"](.*?)['\"]", s)
        return thought, {"kind": "select_option", "option": o.group(1) if o else "", "raw": s}
    return thought, {"kind": "unsupported", "raw": s}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# Infer a click's risk + role from the model's thought. UI-TARS always emits
# category=safe and no button_role, so risk is raised on commit/destructive cues
# (e.g. a quick-entry modal's Save button) and lowered to navigate on
# clearly-navigational cues (so "Add Customer" isn't flagged).
# Ambiguous → safe + no role.
# Note: "create" is deliberately NOT a commit/nav cue — it's ambiguous (clicking
# "Add" to create a form vs. "create" meaning save the record).
_COMMIT_CUES = ("save", "update")
_DESTRUCTIVE_CUES = ("submit", "delete", "discard", "cancel", "post ", "send ", "print", "email", "confirm")
_NAV_CUES = ("new ", "add ", "open", "back", "navigate", "list", "menu",
             "sidebar", "breadcrumb", "tab", "link", "go to")


def infer_click_risk(thought):
    """From the model's thought, return (category, button_role) for a click.

    Order matters: destructive > commit > navigation > default-safe. This is how
    UI-TARS self-classifies risk (it otherwise can't), so Save/Delete are labelled
    correctly wherever they are on screen.
    """
    t = (thought or "").lower()
    if any(c in t for c in _DESTRUCTIVE_CUES):
        return "irreversible", None
    if any(c in t for c in _COMMIT_CUES):
        return "commit", None
    if any(c in t for c in _NAV_CUES):
        return "safe", "navigate"
    return "safe", None


def map_to_app(act, thought, sx=1.0, sy=1.0):
    """Map a parsed UI-TARS action to the app's action dict, or None if unmappable.
    sx/sy rescale model coordinates (smart-resized space) to true pixels."""
    rationale = (thought or "").replace("\n", " ").strip()[:200] or "(UI-TARS action)"
    kind = act["kind"]
    if kind == "click":
        if act.get("x") is None or act.get("y") is None:
            return None
        category, role = infer_click_risk(thought)
        action = {"type": "click_button", "category": category,
                  "x": clamp(int(round(act["x"] * sx)), 0, 1279),
                  "y": clamp(int(round(act["y"] * sy)), 0, 719),
                  "rationale": rationale}
        if role:
            action["button_role"] = role
        return action
    if kind == "type":
        return {"type": "type", "category": "safe",
                "text": act.get("content", ""), "rationale": rationale}
    if kind == "scroll":
        amount = -3 if act.get("direction") == "down" else 3
        return {"type": "scroll", "category": "safe", "amount": amount, "rationale": rationale}
    if kind == "hotkey":
        return {"type": "key", "category": "safe", "key": act.get("key", ""), "rationale": rationale}
    if kind == "navigate":
        url = act.get("url", "")
        if not url:
            return None
        return {"type": "navigate_url", "url": url, "category": "safe", "rationale": rationale}
    if kind == "select_option":
        return {"type": "select_option", "category": "safe",
                "text": act.get("option", ""), "rationale": rationale}
    return None


def describe(act):
    k = act["kind"]
    if k == "click":
        return f"click({act.get('x')},{act.get('y')})"
    if k == "type":
        return f"type({act.get('content', '')[:30]!r})"
    if k == "scroll":
        return f"scroll {act.get('direction')}"
    if k == "hotkey":
        return f"hotkey {act.get('key')}"
    if k == "navigate":
        return f"navigate({act.get('url', '')[:50]})"
    if k == "select_option":
        return f"select_option({act.get('option', '')!r})"
    return k


def action_signature(app_action):
    """Stable signature for loop detection, or None to skip the guard.

    Clicks bucket to ~15px so near-identical re-clicks collapse. Scroll / move
    are legitimately repeatable (long forms), so they're never guarded.
    """
    t = app_action.get("type")
    if t == "click_button":
        x, y = app_action.get("x"), app_action.get("y")
        if x is None or y is None:
            return None
        return ("click", round(x / 15), round(y / 15))
    if t == "type":
        return ("type", (app_action.get("text") or "").strip().lower())
    if t == "key":
        return ("key", app_action.get("key"))
    return None  # scroll / move / other — repeating these is fine


# ── Wait for the human's decision after a proposal ──────────────────
def wait_for_decision(pre_len, timeout=600.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = fetch_state()
        phase = st.get("phase")
        if phase == "IDLE":
            return st, None
        if phase in ("TASK_ACTIVE", "WAITING_HUMAN"):
            alog = st.get("action_log") or []
            return st, (alog[-1] if len(alog) > pre_len else None)
        time.sleep(POLL)
    return fetch_state(), None


def wait_unpaused():
    while True:
        st = fetch_state()
        if not st.get("paused") or st.get("phase") in ("IDLE", "STOPPED"):
            return st
        time.sleep(POLL)


# ── Per-task loop ───────────────────────────────────────────────────
def run_task(task):
    task_id = task.get("task_id")
    task_text = task["text"]
    log(f"task: {task_text!r}")
    history = []
    unparsed = 0
    last_pre_hash = None     # screenshot hash before the last approved action
    last_executed = False    # was the last proposal approved + executed?
    sig_counts = {}          # loop-breaker: how many times each action proposed
    consec_scroll = 0        # scroll-spiral guard: consecutive same-direction scrolls
    last_scroll_dir = None

    # Announce a coarse plan once, at task start.
    # Non-fatal: if the screenshot or model call fails, skip it.
    plan_steps = []
    plan_img = screenshot_b64()
    if plan_img is not None:
        try:
            plan_steps = make_plan(task_text, plan_img) or []
        except Exception as exc:  # noqa: BLE001 — plan is optional, never block the task
            log("plan step failed (non-fatal):", exc)

    for turn in range(MAX_TURNS):
        st = fetch_state()
        if st.get("phase") in ("IDLE", "STOPPED"):
            log("task ended externally")
            return
        if st.get("paused"):
            st = wait_unpaused()
            if st.get("phase") in ("IDLE", "STOPPED"):
                return

        img = screenshot_b64()
        if img is None:
            log("screenshot unavailable this turn; retrying shortly")
            time.sleep(1.0)
            continue
        cur_hash = hashlib.md5(img.encode("ascii"), usedforsecurity=False).hexdigest()
        # Miss-detection: if the last approved action left the screen unchanged,
        # tell the model so it stops re-proposing the same (missed) spot.
        if last_executed and last_pre_hash and cur_hash == last_pre_hash:
            history.append("NOTE: the previous action had NO visible effect "
                           "(likely a missed click) — try a clearly different location or action")
            log("miss-detected: previous action did not change the screen")

        api_call_start_ts = _now_iso()
        current_url = get_current_url()
        out, sx, sy, in_tok, out_tok = call_uitars(build_prompt(task_text, history, None, plan_steps, current_url), img)
        api_call_end_ts = _now_iso()
        try:
            http_post(f"{SERVER_URL}/log_turn", {
                "ts": api_call_start_ts,
                "task_id": task_id,
                "step_n": turn,
                "api_call_start_ts": api_call_start_ts,
                "api_call_end_ts": api_call_end_ts,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "brain": f"uitars-api-{UITARS_API_MODEL}" if UITARS_API_KEY else "uitars",
                "current_url": current_url,
            }, timeout=5)
        except Exception:  # noqa: BLE001 — never let logging break the brain
            pass

        if abs(sx - 1.0) > 1e-3 or abs(sy - 1.0) > 1e-3:
            log(f"coord rescale sx={sx:.4f} sy={sy:.4f}")
        if out is None:
            ctx = (f"No usable response from {UITARS_API_BASE_URL} (model={UITARS_API_MODEL})."
                   if UITARS_API_KEY else f"No response from {UITARS_HOST}.")
            http_post(f"{SERVER_URL}/ask_human",
                      {"question": "I can't reach the UI-TARS model.",
                       "context": ctx})
            return
        thought, act = parse_output(out)
        if act is None:
            unparsed += 1
            log("could not parse an Action from output:", out[:120])
            if unparsed >= 2:
                http_post(f"{SERVER_URL}/complete_task",
                          {"summary": "Stopped: UI-TARS did not return a usable action."})
                return
            continue
        unparsed = 0

        kind = act["kind"]
        if kind == "finished":
            summary = (act.get("content") or thought or "Task finished.").strip()[:300]
            http_post(f"{SERVER_URL}/complete_task", {"summary": summary})
            log("finished:", summary)
            return
        if kind == "wait":
            time.sleep(1.0)
            continue
        if kind in ("unsupported", "call_user"):
            http_post(f"{SERVER_URL}/complete_task",
                      {"summary": f"Stopped: UI-TARS proposed an unsupported action ({act['raw'][:60]})."})
            return

        app_action = map_to_app(act, thought, sx, sy)
        if app_action is None:
            log("unmappable action, skipping:", act.get("raw", "")[:80])
            continue

        # If the model reported a milestone number, forward it so the UI plan
        # checklist advances. The model supplies this only intermittently.
        if plan_steps:
            sm = re.search(r"Step:\s*(\d+)", out)
            if sm:
                n = int(sm.group(1))
                if 1 <= n <= len(plan_steps):
                    app_action["plan_step"] = n
                    log(f"plan_step={n}")

        # Loop breaker: stop the model re-proposing the same action in a cycle.
        sig = action_signature(app_action)
        if sig is not None:
            prior = sig_counts.get(sig, 0)
            if prior >= 2:
                # Third identical proposal: treat as a loop and stop.
                http_post(f"{SERVER_URL}/complete_task",
                          {"summary": "I got stuck repeating the same step, so I stopped to avoid "
                                      "looping. You can take over and continue, or send the task again."})
                log("loop-breaker: hard stop on repeated action", sig)
                return
            sig_counts[sig] = prior + 1
            if prior == 1:
                # Second identical proposal — warn and let the model re-decide
                # instead of proposing it again.
                history.append(
                    f"NOTE: you already did {describe(act)} — it is DONE. Do NOT repeat it. "
                    "If all task fields are filled and saved, output finished(); "
                    "otherwise choose a DIFFERENT action.")
                log("loop-breaker: soft warn on repeated action", sig)
                continue

        # Scroll-spiral guard: same thresholds as qwen_brain (soft warn 4th, hard-stop 6th).
        # The click loop-breaker above handles repeated clicks; this handles the separate
        # failure mode of endless same-direction scrolling with no interleaved field action.
        if app_action.get("type") == "scroll":
            scroll_dir = "down" if app_action.get("amount", 0) < 0 else "up"
            consec_scroll = (consec_scroll + 1) if scroll_dir == last_scroll_dir else 1
            last_scroll_dir = scroll_dir
            if consec_scroll >= 6:
                http_post(f"{SERVER_URL}/complete_task",
                          {"summary": "Stuck scrolling — no field interaction between scrolls; "
                                      "stopped to avoid burning turns."})
                log("scroll-spiral guard: hard stop on", consec_scroll, "consecutive scrolls")
                return
            if consec_scroll == 4:
                history.append(
                    f"NOTE: you have scrolled {consec_scroll} times in a row in the same direction. "
                    "STOP scrolling — try the OPPOSITE direction, use hotkey(key='tab') from a "
                    "focused field, or navigate() directly to the correct URL.")
                log("scroll-spiral guard: soft warn on", consec_scroll, "consecutive scrolls")
                continue
        else:
            consec_scroll = 0
            last_scroll_dir = None

        pre_len = len(fetch_state().get("action_log") or [])
        code, resp = http_post(f"{SERVER_URL}/propose_action",
                               {"action": app_action, "screenshot_b64": img})
        if code == 429:
            http_post(f"{SERVER_URL}/complete_task",
                      {"summary": "Stopped after repeated rejections of the same action."})
            return
        if code != 200:
            log("propose rejected by server:", code, resp)
            time.sleep(POLL)
            continue

        final_state, result = wait_for_decision(pre_len)
        if final_state.get("phase") == "IDLE":
            log("task ended during decision wait")
            return
        decision = (result or {}).get("decision")
        approved = decision == "approve" and (result or {}).get("executed")
        history.append(f"{describe(act)} - {'done' if approved else 'REJECTED, try a different approach'}")
        last_pre_hash = cur_hash if approved else None
        last_executed = approved

    http_post(f"{SERVER_URL}/complete_task", {"summary": f"Reached the {MAX_TURNS}-turn cap."})
    log("hit max turns")


# ── Health check + main poll loop ───────────────────────────────────
def check_host():
    if UITARS_API_KEY:
        if not UITARS_API_BASE_URL:
            log("ERROR: UITARS_API_BASE_URL not set (required when UITARS_API_KEY is set).")
            sys.exit(2)
        # API mode: key + model are sufficient — no pre-flight ping (would cost tokens).
        log(f"API mode: base_url={UITARS_API_BASE_URL} model={UITARS_API_MODEL}")
        return
    # Self-hosted mode: health-check the model server before starting the task loop.
    code, resp = http_get(f"{UITARS_HOST}/health")
    if code != 200:
        log(f"ERROR: cannot reach UI-TARS model server at {UITARS_HOST} ({resp}).")
        log("  Fix: set UITARS_API_KEY / UITARS_API_BASE_URL for a hosted endpoint, or point")
        log("  UITARS_HOST at a reachable self-hosted server exposing /health and /generate.")
        sys.exit(2)
    log(f"model server OK: {resp}")


def main():
    if UITARS_API_KEY:
        log(f"starting (API mode); server={SERVER_URL} model={UITARS_API_MODEL} max_turns={MAX_TURNS}")
    else:
        log(f"starting (local); server={SERVER_URL} model_host={UITARS_HOST} max_turns={MAX_TURNS}")
    check_host()
    log("polling for Do-mode tasks...")
    while True:
        _, task = http_get(f"{SERVER_URL}/pending_chat")
        if isinstance(task, dict) and task.get("task_id"):
            run_task(task)
        else:
            time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user")
