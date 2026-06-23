#!/bin/sh
set -e

# server.py honors PORT (default 8080). Binds 0.0.0.0 so the operator's Service
# can reach it; ALL LLM traffic routes through MODEL_ENDPOINT (the LiteLLM gateway).
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8080}"
