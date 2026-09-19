#!/bin/sh
set -eu
PORT_VALUE="${PORT:-8000}"
exec uvicorn app:app --host 0.0.0.0 --port "$PORT_VALUE"
