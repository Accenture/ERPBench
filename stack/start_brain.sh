#!/bin/bash
# Brain dispatcher — picks which planner script to run based on $BRAIN_PROVIDER.
# Default: claude (preserves claude_brain.py behaviour when env unset).
#
# The brain script's `exec` is what supervisord sees as the long-running PID,
# so autorestart + signal handling work correctly.
#
# Required keys are validated inside each brain on startup:
#   - claude_brain.py exits 2 if ANTHROPIC_API_KEY is unset
#   - qwen_brain.py exits 2 if QWEN_API_KEY is unset
#   - opencua_brain.py exits 2 if OPENCUA_API_KEY is unset

set -e

BRAIN_PROVIDER="${BRAIN_PROVIDER:-claude}"
# Lowercase via tr for bash 3.2 compatibility (macOS local syntax-check).
BRAIN_PROVIDER_LC="$(printf '%s' "$BRAIN_PROVIDER" | tr '[:upper:]' '[:lower:]')"

case "$BRAIN_PROVIDER_LC" in
    qwen)
        # Use the Qwen-tuned system prompt unless the caller overrode SYSTEM_PROMPT_PATH.
        export SYSTEM_PROMPT_PATH="${SYSTEM_PROMPT_PATH:-/app/observer/system_prompt_qwen.md}"
        echo "[start_brain] BRAIN_PROVIDER=qwen → launching qwen_brain.py" >&2
        exec /usr/bin/python3 /app/observer/qwen_brain.py --url=http://localhost:5051
        ;;
    opencua)
        # Use the OpenCUA system prompt unless the caller overrode SYSTEM_PROMPT_PATH.
        export SYSTEM_PROMPT_PATH="${SYSTEM_PROMPT_PATH:-/app/observer/system_prompt_opencua.md}"
        echo "[start_brain] BRAIN_PROVIDER=opencua → launching opencua_brain.py" >&2
        exec /usr/bin/python3 /app/observer/opencua_brain.py --url=http://localhost:5051
        ;;
    uitars)
        # UI-TARS: hosted API (UITARS_API_KEY) or a self-hosted /generate server (UITARS_HOST). No system prompt file —
        # the prompt is built in-code. UITARS_HOST must be reachable from the container.
        echo "[start_brain] BRAIN_PROVIDER=uitars → launching uitars_brain.py" >&2
        exec /usr/bin/python3 /app/observer/uitars_brain.py
        ;;
    holo3)
        # Holo3-35B-A3B via an OpenAI-compatible endpoint.
        # HOLO3_API_KEY must be set in .env.
        export SYSTEM_PROMPT_PATH="${SYSTEM_PROMPT_PATH:-/app/observer/system_prompt_holo3.md}"
        echo "[start_brain] BRAIN_PROVIDER=holo3 → launching holo3_brain.py" >&2
        exec /usr/bin/python3 /app/observer/holo3_brain.py
        ;;
    human|none|off)
        # Human-baseline mode: a person drives the browser through /human.
        # Launch NOTHING. Falling through to the claude|* default here would
        # start claude_brain.py, which would claim the task and act on it,
        # destroying the human run (and spending API credit). Idle instead so
        # supervisord still has a live PID to supervise.
        echo "[start_brain] BRAIN_PROVIDER=$BRAIN_PROVIDER_LC → no brain (human baseline); idling" >&2
        exec sleep infinity
        ;;
    claude|*)
        export SYSTEM_PROMPT_PATH="${SYSTEM_PROMPT_PATH:-/app/observer/system_prompt_claude.md}"
        echo "[start_brain] BRAIN_PROVIDER=claude → launching claude_brain.py" >&2
        exec /usr/bin/python3 /app/observer/claude_brain.py --url=http://localhost:5051
        ;;
esac
