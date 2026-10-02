#!/bin/sh
set -e

# server.py honors PORT (default 8080). Binds 0.0.0.0 so the operator's Service
# can reach it; ALL LLM traffic routes through MODEL_ENDPOINT (the LiteLLM gateway).
# Run as a script, not via the uvicorn CLI: in task mode (AGENT_EXECUTION_MODE=task)
# server.py stops itself when the run ends and sets the exit code. Absolute path:
# the operator sets the working directory to the cloned repo when there is one.
exec python /app/server.py
