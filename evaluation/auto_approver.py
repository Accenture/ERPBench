"""Auto-approver loop for autonomous benchmark runs.

Watches the observer server's /status for `phase == "PROPOSED"`. As soon as
one is seen, POSTs /decide with `decision=approve`, so every action is
approved automatically.

Runs as a daemon thread, controlled by a stop Event the runner sets when
the task ends. Decisions are mirrored to a per-run JSONL for latency metrics.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("auto_approver")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _append_decision_log(path: str, entry: dict) -> None:
    """Append one JSON line to the per-run decisions log. Swallows OSError."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("could not write decisions log: %r", exc)


AUTONOMOUS_CONTINUE_MSG = (
    "Please continue autonomously. Use your best judgment for any "
    "unspecified details and complete the task with the information "
    "you have. If a field has a sensible default, use it. Do not ask "
    "for further clarification — finish the task."
)


def run_auto_approver(server_url: str, stop_event: threading.Event,
                      decisions_log_path: str, poll_interval: float = 0.2) -> None:
    """Block until stop_event is set. Approves every PROPOSED action AND
    deflects every WAITING_HUMAN escalation with a generic 'continue'
    reply (autonomous mode contract).

    Why poll 0.2s: the brain proposes about once every 1-3 seconds (network +
    LLM latency). Polling faster than that just spins CPU; polling slower
    adds latency on every approval. 0.2s is a comfortable middle.
    """
    seen_tokens: set[str] = set()
    last_ask_human_ts: str | None = None     # de-dupe: only reply once per ask

    while not stop_event.is_set():
        try:
            response = requests.get(f"{server_url}/status", timeout=5)
            if response.status_code != 200:
                time.sleep(poll_interval)
                continue
            state = response.json()
        except Exception as exc:  # noqa: BLE001 — keep approver resilient
            log.debug("status poll failed: %r", exc)
            time.sleep(poll_interval)
            continue

        phase = state.get("phase")

        # Autonomous mode contract: if the brain escalates via ask_human,
        # we deflect with a generic continue message. The server transitions
        # WAITING_HUMAN → TASK_ACTIVE on any POST /chat.
        if phase == "WAITING_HUMAN":
            # The last chat-log entry from role=agent is the question. We
            # send our reply once per question (keyed by the question's ts)
            # to avoid hammering /chat on every poll.
            chat_log = state.get("chat_log") or []
            last_agent = next(
                (t for t in reversed(chat_log) if t.get("role") == "agent"),
                None,
            )
            ask_ts = last_agent.get("ts") if last_agent else None
            if ask_ts and ask_ts != last_ask_human_ts:
                last_ask_human_ts = ask_ts
                try:
                    requests.post(
                        f"{server_url}/chat",
                        json={"text": AUTONOMOUS_CONTINUE_MSG},
                        timeout=10,
                    )
                    _append_decision_log(decisions_log_path, {
                        "ts_decide_start": _now_iso(),
                        "phase_seen":      "WAITING_HUMAN",
                        "action_type":     "auto_continue_reply",
                        "ask_question_ts": ask_ts,
                        "reply_sent":      AUTONOMOUS_CONTINUE_MSG[:80],
                    })
                    log.info("auto-replied to ask_human (ts=%s)", ask_ts)
                except Exception as exc:  # noqa: BLE001
                    log.warning("auto-reply failed: %r", exc)
            time.sleep(poll_interval)
            continue

        if phase != "PROPOSED":
            time.sleep(poll_interval)
            continue

        token = state.get("pending_action_token")
        if not token or token in seen_tokens:
            time.sleep(poll_interval)
            continue
        seen_tokens.add(token)

        action = state.get("pending_action") or {}
        decide_start = _now_iso()
        try:
            r = requests.post(
                f"{server_url}/decide",
                json={
                    "decision": "approve",
                    "token": token,
                    "confirmed_critical": True,
                },
                timeout=60,    # generous: executor runs under the same call
            )
            decide_end = _now_iso()
            result_body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status_code": r.status_code}
        except Exception as exc:  # noqa: BLE001
            decide_end = _now_iso()
            result_body = {"error": str(exc)}

        # Append a per-decision audit line.
        _append_decision_log(decisions_log_path, {
            "ts_decide_start": decide_start,
            "ts_decide_end":   decide_end,
            "token":           token,
            "phase_seen":      state.get("phase"),
            "action_type":     action.get("type"),
            "action_category": action.get("category"),
            "category_original": action.get("category_original"),
            "x":               action.get("x"),
            "y":               action.get("y"),
            "result":          result_body,
        })
