#!/bin/sh
set -eu
# Home Assistant writes app options to /data/options.json. The application
# reads that file directly, so the token is never copied into argv or logged.
# The public CA is read only from the Supervisor-provided /config mount.
exec python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8099 --workers 1 --no-proxy-headers --no-access-log --timeout-graceful-shutdown 25
