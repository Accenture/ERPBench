"""Human-baseline capture: turn real browser input into harness action logs.

WHY THIS EXISTS
---------------
The agents produce `actions.log` entries because they *propose* every action
through the observer. A human clicks directly in the browser, so nothing is
proposed and nothing is logged. Without a log, `partial_grader` cannot award
the Navigate / Interact / Commit stages, and `log_grader` cannot count actions.

So we observe the real DOM and synthesise entries in exactly the schema the
graders already read. Nothing downstream changes.

HOW IT WORKS
------------
1. `INJECTED_JS` is evaluated in the ERPNext page over CDP. It installs
   capture-phase listeners for click / change / Ctrl+S and POSTs each event
   straight to the observer.

   The POST is `text/plain` under `mode: no-cors` on purpose: that keeps it a
   CORS "simple request", so it needs no preflight and no CORS config on the
   observer. The observer parses the body as JSON by hand.

2. `map_event()` converts one raw event into one `actions.log` entry.

THE GRADER CONTRACT (see evaluation/graders/partial_grader.py)
----------------------------------------------------------
  right_page  ← `turns` entries carrying `current_url` (written by the
                observer's URL poller, not here)
  right_value ← an action with type "type" whose `text` matches an expected
                value, or type "select_option" whose `value` matches
  committed   ← an entry with top-level decision == "approve" AND either
                action.category == "commit", or action.type == "key" with
                key in {ctrl+s, cmd+s, command+s}

`decision: "approve"` is mandatory on every entry: `log_grader` ignores any
entry without a decision, and `_check_committed` requires it explicitly.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

# Labels that mean "this click was a save", matching inspector.py's _SAVE_LABELS.
_SAVE_LABELS = {"save", "submit", "update"}

_FIELD_TAGS = {"input", "textarea", "select"}


def _now_iso() -> str:
    """Millisecond precision: human clicks can land <1s apart."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_save_click(ev: dict) -> bool:
    """True if this click should count as a commit."""
    if (ev.get("role") or "").strip().lower() == "save":
        return True
    for key in ("text", "data_label", "aria_label"):
        if (ev.get(key) or "").strip().lower() in _SAVE_LABELS:
            return True
    return False


def map_event(ev: dict, task_id: str | None, step_n: int) -> dict | None:
    """Convert one raw browser event into one actions.log entry.

    Returns None for events we deliberately ignore (e.g. a change event that
    cleared a field to empty, which carries no gradeable value).
    """
    kind = (ev.get("kind") or "").strip()
    ts = ev.get("ts") or _now_iso()
    tag = (ev.get("tag") or "").strip().lower()
    label = (ev.get("label") or "").strip()
    text = (ev.get("text") or "").strip()

    if kind == "click":
        if is_save_click(ev):
            action = {
                "type": "click_button",
                "button_label": text or "Save",
                "button_role": "save",
                "category": "commit",
            }
        elif tag in _FIELD_TAGS:
            action = {
                "type": "click_field",
                "field_label": label or text,
                "category": "safe",
            }
        elif tag == "a":
            action = {
                "type": "click_link",
                "link_label": text,
                "category": "safe",
            }
        else:
            action = {
                "type": "click_button",
                "button_label": text,
                "category": "safe",
            }
        if ev.get("x") is not None:
            action["x"] = ev.get("x")
            action["y"] = ev.get("y")

    elif kind == "change":
        value = (ev.get("value") or "").strip()
        if not value:
            return None
        # A <select> or a Frappe Link/Select control is semantically a
        # select_option; a plain input is a type. We populate BOTH `text` and
        # `value` on either shape so _check_right_value matches whichever
        # branch it takes.
        is_select = tag == "select" or bool(ev.get("is_select"))
        action = {
            "type": "select_option" if is_select else "type",
            "text": value,
            "value": value,
            "field_label": label,
            "category": "safe",
        }

    elif kind == "save_key":
        # _check_committed has an explicit branch for this.
        action = {"type": "key", "key": "ctrl+s", "category": "safe"}

    elif kind == "scroll":
        # Agents emit `scroll` as a first-class action and it is a LARGE share of
        # their totals (Claude 9.9%, UI-TARS 6.5%, OpenCUA-32B 31.1%). Omitting
        # human scrolls would make humans look far more efficient than they are.
        # One wheel gesture fires dozens of DOM events, so the page agent
        # coalesces a burst into a single action, matching the agents' one-action
        # -per-scroll granularity.
        try:
            amount = int(ev.get("amount") or 0)
        except Exception:  # noqa: BLE001
            amount = 0
        action = {"type": "scroll", "amount": amount, "category": "safe"}

    elif kind == "key":
        # Only the discrete navigation keys the agents' own schema allows
        # (tab / enter / escape). Deliberately NOT backspace or delete: a human
        # deletes character by character while an agent issues one clear_field,
        # so counting them would penalise humans for a granularity difference.
        # The field's resulting value is already captured as one `type` action.
        key = (ev.get("key") or "").strip().lower()
        if key not in ("tab", "enter", "escape"):
            return None
        action = {"type": "key", "key": key, "category": "safe"}

    else:
        return None

    action["rationale"] = "(human annotator)"

    return {
        "ts": ts,
        "task_id": task_id,
        "step_n": step_n,
        "action": action,
        # Mandatory: graders skip entries with no decision.
        "decision": "approve",
        "decision_ts": ts,
        "server_upgraded": False,
        "source": "human",
    }


# ── Injected page agent ─────────────────────────────────────────────
# Guarded so repeated injection is harmless. Uses only capture-phase listeners
# so ERPNext's own handlers cannot stop propagation before we see the event.
INJECTED_JS = r"""
(function () {
  if (window.__erpbenchHuman) { return "already-installed"; }
  window.__erpbenchHuman = true;

  var ENDPOINT = "__OBSERVER__/human/event";

  function post(payload) {
    try {
      fetch(ENDPOINT, {
        method: "POST",
        mode: "no-cors",
        headers: { "Content-Type": "text/plain" },
        body: JSON.stringify(payload),
        keepalive: true
      }).catch(function () {});
    } catch (e) {}
  }

  function txt(el) {
    if (!el) return "";
    var t = el.getAttribute && (el.getAttribute("data-label") || el.getAttribute("aria-label"));
    if (t) return String(t).trim().slice(0, 120);
    return (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 120);
  }

  // Frappe wraps every field in .frappe-control with a .control-label.
  function labelFor(el) {
    if (!el) return "";
    var ctrl = el.closest ? el.closest(".frappe-control, .form-group") : null;
    if (ctrl) {
      var lab = ctrl.querySelector(".control-label, label");
      if (lab) return (lab.innerText || "").trim().replace(/\s+/g, " ").slice(0, 120);
      var fn = ctrl.getAttribute && ctrl.getAttribute("data-fieldname");
      if (fn) return String(fn);
    }
    return (el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("placeholder"))) || "";
  }

  function isSelectish(el) {
    if (!el) return false;
    if ((el.tagName || "").toLowerCase() === "select") return true;
    var ctrl = el.closest ? el.closest(".frappe-control") : null;
    var dt = ctrl && ctrl.getAttribute ? (ctrl.getAttribute("data-fieldtype") || "") : "";
    return /select|link|autocomplete/i.test(dt) || !!(el.classList && el.classList.contains("awesomplete"));
  }

  document.addEventListener("click", function (ev) {
    var el = ev.target;
    if (!el) return;
    var act = el.closest
      ? el.closest("button, a, input, select, textarea, .btn, [role='button'], .list-row-col, .clickable")
      : null;
    var node = act || el;
    post({
      kind: "click",
      tag: (node.tagName || "").toLowerCase(),
      text: txt(node),
      data_label: (node.getAttribute && node.getAttribute("data-label")) || "",
      aria_label: (node.getAttribute && node.getAttribute("aria-label")) || "",
      label: labelFor(node),
      is_select: isSelectish(node),
      x: ev.clientX,
      y: ev.clientY
    });
  }, true);

  // ── Field value capture ───────────────────────────────────────────
  // Relying on `change` alone proved unreliable: a Frappe Select can commit its
  // value through its own JS without a native change event carrying the value,
  // which loses the Interact evidence the grader needs. So we watch three
  // paths — change, debounced input, and focusout — and dedupe per field so one
  // edit yields exactly one action.
  var lastValue = {};          // fieldKey -> last posted value, so one edit posts once

  function fieldKey(el) {
    return (labelFor(el) || "") + "|" + (el.tagName || "") + "|" + (el.type || "") +
           "|" + (el.name || "") + "|" + (el.id || "");
  }

  function readValue(el) {
    if (!el) return "";
    if (el.type === "checkbox") { return el.checked ? "1" : "0"; }
    var v = el.value;
    return v == null ? "" : String(v).slice(0, 500);
  }

  function postValue(el, why) {
    if (!el) return;
    var tag = (el.tagName || "").toLowerCase();
    if (tag !== "input" && tag !== "textarea" && tag !== "select") return;
    var v = readValue(el);
    if (!v) return;                      // empty carries nothing gradeable
    var k = fieldKey(el);
    if (lastValue[k] === v) return;      // already recorded this edit
    lastValue[k] = v;
    post({
      kind: "change",
      tag: tag,
      input_type: el.type || "",
      label: labelFor(el),
      value: v,
      is_select: isSelectish(el),
      via: why
    });
  }

  // We do NOT listen to `input`. Doing so recorded one action per typing pause
  // ("TX", "TX-", "TX-c3", ...), five actions for one field edit, while an agent
  // types a whole string in one action. `change` and `focusout` each fire once
  // per completed edit, which is the matching granularity.
  document.addEventListener("change",   function (ev) { postValue(ev.target, "change"); },   true);
  document.addEventListener("focusout", function (ev) { postValue(ev.target, "focusout"); }, true);

  document.addEventListener("keydown", function (ev) {
    var k = (ev.key || "").toLowerCase();
    if ((ev.ctrlKey || ev.metaKey) && k === "s") {
      // Ctrl+S saves without blurring the field, so `change`/`focusout` may
      // never fire and the typed value would be lost. Record it first.
      postValue(document.activeElement, "save_key");
      post({ kind: "save_key" });
      return;
    }
    // Only the discrete keys the agents' schema allows. Ordinary typing is
    // captured by the change listener as one `type` action, matching how an
    // agent types a whole string in a single action.
    if (k === "tab" || k === "enter" || k === "escape") {
      post({ kind: "key", key: (k === "escape" ? "escape" : k) });
    }
  }, true);

  // Scroll: coalesce a wheel burst into ONE action, because an agent's single
  // `scroll` action is one decision while a human gesture fires dozens of
  // events. Flushed after 400ms of no further wheel movement.
  var scrollAcc = 0, scrollTimer = null;
  window.addEventListener("wheel", function (ev) {
    scrollAcc += (ev.deltaY || 0);
    if (scrollTimer) { clearTimeout(scrollTimer); }
    scrollTimer = setTimeout(function () {
      var amt = Math.round(scrollAcc);
      scrollAcc = 0; scrollTimer = null;
      if (amt !== 0) { post({ kind: "scroll", amount: amt }); }
    }, 400);
  }, true);

  // Keyboard scrolling (PageUp/PageDown/Home/End) is the same intent as a wheel
  // scroll, so route it through the same coalescing path.
  document.addEventListener("keydown", function (ev) {
    var k = (ev.key || "").toLowerCase();
    if (k === "pagedown" || k === "pageup" || k === "home" || k === "end") {
      scrollAcc += (k === "pagedown" || k === "end") ? 600 : -600;
      if (scrollTimer) { clearTimeout(scrollTimer); }
      scrollTimer = setTimeout(function () {
        var amt = Math.round(scrollAcc);
        scrollAcc = 0; scrollTimer = null;
        if (amt !== 0) { post({ kind: "scroll", amount: amt }); }
      }, 400);
    }
  }, true);

  return "installed";
})();
"""


def injected_js(observer_url: str) -> str:
    """The page agent with the observer URL baked in."""
    return INJECTED_JS.replace("__OBSERVER__", observer_url.rstrip("/"))


# ── CDP injection ───────────────────────────────────────────────────
def _page_ws_url(cdp_http: str) -> str | None:
    """Find the websocket debugger URL of the active page target."""
    try:
        with urllib.request.urlopen(f"{cdp_http}/json/list", timeout=5) as r:
            tabs = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    for tab in tabs if isinstance(tabs, list) else []:
        if isinstance(tab, dict) and tab.get("type") == "page":
            return tab.get("webSocketDebuggerUrl")
    return None


def inject_into_page(cdp_http: str = "http://localhost:9222",
                     observer_url: str = "http://localhost:5051",
                     timeout: float = 8.0) -> tuple[bool, str]:
    """Install the page agent in the live page and on all future documents.

    Returns (ok, detail). Never raises. Requires websocket-client, which is
    already a declared dependency of the stack (requirements.txt).
    """
    try:
        import websocket  # type: ignore
    except Exception:
        return False, "websocket-client is not installed in this environment"

    ws_url = _page_ws_url(cdp_http)
    if not ws_url:
        return False, f"no page target found at {cdp_http}"

    source = injected_js(observer_url)
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"cdp connect failed: {exc!r}"

    def send(mid: int, method: str, params: dict) -> dict | None:
        ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        # Drain until we see our id; ignore unsolicited events.
        for _ in range(50):
            try:
                msg = json.loads(ws.recv())
            except Exception:
                return None
            if msg.get("id") == mid:
                return msg
        return None

    detail = []
    try:
        send(1, "Page.enable", {})
        # Survives future document loads (full reloads, not just SPA routing).
        r2 = send(2, "Page.addScriptToEvaluateOnNewDocument", {"source": source})
        detail.append("persistent=ok" if r2 and "result" in r2 else "persistent=failed")
        # Install into the document that is already open.
        r3 = send(3, "Runtime.evaluate", {"expression": source, "returnByValue": True})
        val = ((r3 or {}).get("result", {}).get("result", {}) or {}).get("value")
        detail.append(f"current={val!r}")
        ok = bool(r2 and "result" in r2) or val in ("installed", "already-installed")
        return ok, "; ".join(detail)
    except Exception as exc:  # noqa: BLE001
        return False, f"cdp error: {exc!r}"
    finally:
        try:
            ws.close()
        except Exception:
            pass
