"""PyAutoGUI executor for ERPBench.

The agent proposes actions via the Flask server; only approved actions reach
this module.

Every action returns a post-execution screenshot so the brain can verify the
outcome on its next turn. Nothing here decides whether an action is "allowed" —
that's the server's job. This module only knows how to execute.
"""

import base64
import io
import logging
import os
import platform
import subprocess
import tempfile
import time

os.environ.setdefault("DISPLAY", ":1")

import pyautogui  # noqa: E402 — DISPLAY must be set first

# Tracks whether the per-session CDP beforeunload-blocker has been injected.
_beforeunload_script_injected: bool = False

pyautogui.FAILSAFE = False  # actions only run after the server approves them


log = logging.getLogger("inspector")


# ── Constants ───────────────────────────────────────────────────────
SCREEN_W = 1280
SCREEN_H = 720

ALLOWED_KEYS = {
    "tab", "enter", "return", "escape", "esc", "backspace", "delete",
    # Keyboard chords (dispatched via pyautogui.hotkey, not press)
    "ctrl+s", "ctrl+a", "ctrl+enter",
}

ALLOWED_ACTION_TYPES = {
    "move_to",
    "scroll",
    "click_field",
    "click_dropdown",
    "click_button",
    "click_link",
    "type",
    "select_option",
    "set_date",
    "clear_field",
    "key",
    "screenshot",
    "navigate_url",
}

# After typing, give the form a moment to render (autocomplete dropdowns,
# validation badges, etc.) before screenshotting.
POST_TYPE_SETTLE_S = 0.15
POST_CLICK_SETTLE_S = 0.20


def cursor_position():
    """Read current cursor coordinates. No movement, no side effects."""
    pos = pyautogui.position()
    return int(pos[0]), int(pos[1])


def _select_all_modifier():
    """Return the OS-appropriate modifier for select-all. Container is Linux."""
    return "ctrl" if platform.system() != "Darwin" else "command"


# Labels we accept as "this click is really a Save". Whole-label match only
# (case-insensitive). Deliberately narrow — "Save Draft", "Save as Template",
# "Save & Continue" are distinct workflows that Ctrl+S may not equal, so we
# don't include them. Extend later only if we see false negatives.
_SAVE_LABELS = {"save", "submit", "update"}


def _should_rewrite_save(action: dict) -> bool:
    """Return True if this click_button should be dispatched as Ctrl+S.

    Two signals accepted:
      1. button_role == "save"   — set by Claude's tool schema explicitly.
      2. button_label ∈ {"save", "submit", "update"} (case-insensitive,
         whole-label)  — Qwen's typical Save-click label pattern.

    Fails closed: unknown / missing / ambiguous → False → normal pixel click.
    """
    if action.get("button_role") == "save":
        return True
    label = (action.get("button_label") or "").strip().lower()
    return label in _SAVE_LABELS


def _dismiss_leave_dialog() -> None:
    """Dismiss any open native Chrome 'Leave app?' dialog via CDP.

    Called before every screenshot as a safety net.
    Page.handleJavaScriptDialog returns an error if no dialog is open — caught silently.
    """
    try:
        import json as _json
        import urllib.request as _req
        import websocket
        with _req.urlopen("http://localhost:9222/json/list", timeout=2) as _r:
            _tabs = _json.load(_r)
        _tab = next((t for t in _tabs if t.get("type") == "page"), None)
        if not _tab:
            return
        _ws = websocket.create_connection(_tab["webSocketDebuggerUrl"], timeout=3)
        _ws.settimeout(3)
        _ws.send(_json.dumps({"id": 1, "method": "Page.handleJavaScriptDialog",
                              "params": {"accept": True}}))
        _ws.recv()
        _ws.close()
    except Exception:  # noqa: BLE001
        pass


def _take_screenshot_b64() -> str:
    """Capture a PNG of the Xvfb display, return base64 string.

    Uses scrot directly: pyautogui→Pillow ImageGrab prefers gnome-screenshot,
    which writes an empty/invalid file on headless Xvfb. scrot is reliable here.
    """
    _dismiss_leave_dialog()
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        subprocess.run(
            ["scrot", "-o", path],
            check=True,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _ok(action_str: str, *, post_screenshot: bool = True) -> dict:
    """Build a success result, optionally with a post-action screenshot."""
    result = {"executed": True, "action": action_str}
    if post_screenshot:
        try:
            result["post_screenshot_b64"] = _take_screenshot_b64()
        except Exception as exc:  # noqa: BLE001
            result["post_screenshot_error"] = str(exc)
    return result


def _missing(action_type: str, field: str) -> dict:
    msg = f"{action_type} skipped: '{field}' missing"
    log.warning(msg)
    return {"executed": False, "action": msg}


def _err(action_type: str, exc: BaseException) -> dict:
    msg = f"{action_type} failed: {exc.__class__.__name__}: {exc}"
    log.warning(msg)
    return {"executed": False, "action": msg, "error": str(exc)}


def execute(action: dict) -> dict:
    """Execute one approved action against the Xvfb display.

    Always returns {executed: bool, action: str, error?: str,
    post_screenshot_b64?: str}. Never raises. Server is responsible for
    approval — this function trusts that its caller already gated.
    """
    if not isinstance(action, dict):
        return {"executed": False, "action": "no action provided"}

    atype = action.get("type") or action.get("action_type")
    if atype not in ALLOWED_ACTION_TYPES:
        return {"executed": False, "action": f"unknown action type: {atype!r}",
                "error": "unknown action type"}

    try:
        if atype == "move_to":
            x, y = action.get("x"), action.get("y")
            if x is None or y is None:
                return _missing("move_to", "x or y")
            xi, yi = int(x), int(y)
            pyautogui.moveTo(xi, yi, duration=0.3)
            return _ok(f"move_to ({xi}, {yi})")

        if atype == "scroll":
            amount = action.get("amount")
            if amount is None:
                return _missing("scroll", "amount")
            pyautogui.scroll(int(amount))
            time.sleep(POST_CLICK_SETTLE_S)
            return _ok(f"scroll {int(amount)}")

        if atype in ("click_field", "click_dropdown", "click_button", "click_link"):
            x, y = action.get("x"), action.get("y")
            if x is None or y is None:
                return _missing(atype, "x or y")
            xi, yi = int(x), int(y)
            label = action.get("button_label") or action.get("link_label") or ""
            label_suffix = f" '{label}'" if label else ""

            # Save-button rewrite: intercept click_button on a Save/Submit/Update
            # button and dispatch Ctrl+S via Frappe's global keydown handler,
            # bypassing the coordinate click. Phase 2 matrix data showed Qwen
            # reliably tags Save clicks (button_role or button_label) but
            # misgrounds the toolbar Save button's pixel position, killing
            # ~4 create/edit tasks with a right_value=5/5 committed=0/5 pattern.
            # This rewrite decouples "save intent" from "correct pixel coord".
            # Toggle via EXECUTOR_SAVE_REWRITE env var (default 'on').
            if atype == "click_button" and _should_rewrite_save(action):
                if os.environ.get("EXECUTOR_SAVE_REWRITE", "on").lower() != "off":
                    pyautogui.hotkey(_select_all_modifier(), "s")
                    time.sleep(POST_CLICK_SETTLE_S)
                    return _ok(f"key ctrl+s (rewrite of click_button ({xi}, {yi}){label_suffix})")

            pyautogui.moveTo(xi, yi, duration=0.2)
            pyautogui.click()
            time.sleep(POST_CLICK_SETTLE_S)
            return _ok(f"{atype} ({xi}, {yi}){label_suffix}")

        if atype == "type":
            text = action.get("text")
            if text is None:
                return _missing("type", "text")
            # interval gives the form a chance to handle autocomplete per char
            pyautogui.write(str(text), interval=0.05)
            time.sleep(POST_TYPE_SETTLE_S)
            return _ok(f"type {text!r}")

        if atype == "select_option":
            # Pick/confirm a value in the focused field. Idempotent: clear any
            # partially- or fully-typed text FIRST, then write the value once and
            # press Enter. Without the clear, a model that does `type "Government"`
            # then `select_option "Government"` (to confirm the autocomplete) ends
            # up with "GovernmentGovernment" — an invalid Link value that silently
            # leaves the field unset. Clearing makes select_option safe whether or
            # not the model already typed the value.
            value = action.get("text") or action.get("value")
            if not value:
                return _missing("select_option", "text or value")
            pyautogui.hotkey(_select_all_modifier(), "a")
            pyautogui.press("delete")
            time.sleep(POST_TYPE_SETTLE_S)
            pyautogui.write(str(value), interval=0.04)
            time.sleep(POST_TYPE_SETTLE_S)
            pyautogui.press("enter")
            time.sleep(POST_CLICK_SETTLE_S)
            return _ok(f"select_option {value!r}")

        if atype == "set_date":
            # ERPNext date fields accept text input (typically YYYY-MM-DD).
            # We assume the field is already focused.
            value = action.get("value") or action.get("text")
            if not value:
                return _missing("set_date", "value")
            # Clear any existing content first (select-all + delete)
            pyautogui.hotkey(_select_all_modifier(), "a")
            pyautogui.press("delete")
            pyautogui.write(str(value), interval=0.04)
            time.sleep(POST_TYPE_SETTLE_S)
            return _ok(f"set_date {value!r}")

        if atype == "clear_field":
            # Select all + delete in currently-focused field.
            pyautogui.hotkey(_select_all_modifier(), "a")
            pyautogui.press("delete")
            time.sleep(POST_TYPE_SETTLE_S)
            return _ok("clear_field")

        if atype == "key":
            key = str(action.get("key", "")).lower()
            if not key:
                return _missing("key", "key")
            if key not in ALLOWED_KEYS:
                return {"executed": False,
                        "action": f"key {key!r} not in allowlist",
                        "error": f"key not allowed: {key}"}
            # Chord (e.g. "ctrl+s", "ctrl+a") → pyautogui.hotkey. Single key
            # (e.g. "tab", "enter") → pyautogui.press. The `+` split is the
            # chord marker — mirrors how set_date/clear_field invoke hotkey
            # for their internal select-all step.
            if "+" in key:
                parts = [p.strip() for p in key.split("+") if p.strip()]
                pyautogui.hotkey(*parts)
                time.sleep(POST_CLICK_SETTLE_S)
                return _ok(f"key {key}")
            # Normalise return -> enter, esc -> escape
            press_key = "enter" if key == "return" else ("escape" if key == "esc" else key)
            pyautogui.press(press_key)
            time.sleep(POST_CLICK_SETTLE_S)
            return _ok(f"key {press_key}")

        if atype == "navigate_url":
            url = action.get("url")
            if not url:
                return _missing("navigate_url", "url")
            try:
                import json as _json
                import urllib.request as _req
                import websocket
                with _req.urlopen("http://localhost:9222/json/list", timeout=5) as r:
                    tabs = _json.load(r)
                page_tab = next((t for t in tabs if t.get("type") == "page"), None)
                if not page_tab:
                    raise RuntimeError("no page tab found in CDP /json/list")
                ws = websocket.create_connection(page_tab["webSocketDebuggerUrl"], timeout=10)
                ws.settimeout(30)

                # Send all setup commands before navigating; their responses are
                # consumed in the drain loop so nothing blocks here.

                # Enable Page domain events so Page.javascriptDialogOpening is delivered.
                ws.send(_json.dumps({"id": 10, "method": "Page.enable"}))

                # Re-inject on every call so the blocker survives any Chrome restart.
                # Blocks both addEventListener('beforeunload') and window.onbeforeunload=...
                ws.send(_json.dumps({"id": 1, "method": "Page.addScriptToEvaluateOnNewDocument",
                                     "params": {"source":
                                         "(function(){"
                                         "var _a=EventTarget.prototype.addEventListener;"
                                         "EventTarget.prototype.addEventListener=function(t,h,o){"
                                         "if(t==='beforeunload')return;"
                                         "return _a.call(this,t,h,o);};"
                                         "try{Object.defineProperty(window,'onbeforeunload',{"
                                         "get:function(){return null;},"
                                         "set:function(){},"
                                         "configurable:false"
                                         "})}catch(e){window.onbeforeunload=null;}"
                                         "})();"
                                     }}))

                # Clear dirty state on the current page before navigating.
                ws.send(_json.dumps({"id": 2, "method": "Runtime.evaluate",
                                     "params": {"expression":
                                         "window.onbeforeunload=null;"
                                         "try{if(window.cur_frm){"
                                         "cur_frm.doc.__unsaved=0;"
                                         "cur_frm.dirty=false;"
                                         "}}catch(e){}"
                                     }}))

                ws.send(_json.dumps({"id": 3, "method": "Page.navigate",
                                     "params": {"url": str(url)}}))

                # Drain messages until the navigate response (id:3) arrives.
                # Each Page.javascriptDialogOpening event is dismissed immediately —
                # handles any number of sequential dialogs without timing out.
                deadline = time.time() + 30
                while time.time() < deadline:
                    ws.settimeout(max(1.0, deadline - time.time()))
                    try:
                        msg = _json.loads(ws.recv())
                    except Exception:
                        break
                    if msg.get("method") == "Page.javascriptDialogOpening":
                        log.warning("navigate_url: beforeunload dialog detected — dismissing")
                        ws.send(_json.dumps({"id": 99, "method": "Page.handleJavaScriptDialog",
                                             "params": {"accept": True}}))
                    elif msg.get("id") == 3:
                        break

                # Wait for the page to actually FINISH rendering. Frappe loads
                # list rows / form data via AJAX AFTER the document 'load' event
                # (readyState hits 'complete' in ~0.3s but heavy pages like the
                # Item list can take ~15s to paint their rows). A fixed sleep
                # races that and starts the task on a blank screen. Instead poll
                # a view-agnostic signal — the size of the rendered body text —
                # and return once it has stabilised (stopped growing) or the cap
                # is hit. Works for list, form, and workspace pages alike; fast
                # pages return in ~1s, slow pages get the time they need.
                READY_CAP_S = 25.0          # hard ceiling; never hang (cold Item list ~15s)
                READY_MIN_S = 1.0           # floor; let first paint start
                STABLE_POLLS = 3            # consecutive unchanged reads => done
                POLL_EVERY_S = 0.5
                poll_id = 1000
                ready_deadline = time.time() + READY_CAP_S
                min_until = time.time() + READY_MIN_S
                last_len = -1
                stable_hits = 0

                def _eval_value(expr: str) -> object:
                    """Runtime.evaluate on the open ws; skip interleaved events."""
                    nonlocal poll_id
                    poll_id += 1
                    my_id = poll_id
                    ws.send(_json.dumps({"id": my_id, "method": "Runtime.evaluate",
                                         "params": {"expression": expr,
                                                    "returnByValue": True}}))
                    while True:
                        m = _json.loads(ws.recv())
                        if m.get("method") == "Page.javascriptDialogOpening":
                            ws.send(_json.dumps({"id": 98, "method": "Page.handleJavaScriptDialog",
                                                 "params": {"accept": True}}))
                            continue
                        if m.get("id") == my_id:
                            return (m.get("result", {}) or {}).get("result", {}).get("value")

                while time.time() < ready_deadline:
                    ws.settimeout(6.0)
                    try:
                        rs = _eval_value("document.readyState")
                        tlen = _eval_value(
                            "(document.body?document.body.innerText.length:0)")
                    except Exception:
                        break  # fall through to the settle + return
                    if rs == "complete" and isinstance(tlen, int) and tlen > 0:
                        if tlen == last_len:
                            stable_hits += 1
                            if stable_hits >= STABLE_POLLS and time.time() >= min_until:
                                break
                        else:
                            stable_hits = 0
                            last_len = tlen
                    time.sleep(POLL_EVERY_S)

                # Nav-assist only (clear_list_filters flag): reset any persisted
                # list-view filters so each task starts on an unfiltered list.
                # Frappe saves list filters in user_settings and RESTORES them
                # when you return to /app/<doctype> — so a filter set by a prior
                # task (e.g. search_and_open_customer's name filter, or a prior
                # filter_customers_by_group run) leaks into this task and shows an
                # empty/wrong list. filter_area.clear() clears AND persists empty.
                if action.get("clear_list_filters"):
                    try:
                        _eval_value(
                            "(function(){try{if(window.cur_list&&cur_list.filter_area)"
                            "{cur_list.filter_area.clear();}}catch(e){}})()")
                        time.sleep(1.5)  # let the cleared list refresh
                    except Exception:  # noqa: BLE001
                        pass

                ws.close()
            except Exception as exc:  # noqa: BLE001
                return {"executed": False, "action": f"navigate_url {url!r}",
                        "error": f"CDP navigate failed: {exc}"}
            time.sleep(0.5)  # brief settle for late-rendering widgets
            return _ok(f"navigate_url {url!r}")

        if atype == "screenshot":
            # Internal helper. Server never exposes this through the approval
            # flow; the brain takes screenshots through this same function
            # between proposals.
            return _ok("screenshot captured")

        return {"executed": False, "action": f"unreachable dispatch for {atype}"}

    except Exception as exc:  # noqa: BLE001 — surface, never crash callers
        return _err(atype, exc)


if __name__ == "__main__":
    # Standalone smoke test — exercise every action type briefly.
    # Run inside the container: `python3 inspector.py`
    print("=== move_to (640, 360) ===")
    print({k: v for k, v in execute({"type": "move_to", "x": 640, "y": 360}).items()
           if k != "post_screenshot_b64"})

    print("\n=== scroll(-2) ===")
    print({k: v for k, v in execute({"type": "scroll", "amount": -2}).items()
           if k != "post_screenshot_b64"})

    print("\n=== key escape ===")
    print({k: v for k, v in execute({"type": "key", "key": "escape"}).items()
           if k != "post_screenshot_b64"})

    print("\n=== key tab ===")
    print({k: v for k, v in execute({"type": "key", "key": "tab"}).items()
           if k != "post_screenshot_b64"})

    print("\n=== unknown type ===")
    print(execute({"type": "wiggle"}))

