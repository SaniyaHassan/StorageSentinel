#!/bin/bash
# Wrapper to run the StorageSentinel server health report generator.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
python3 "$SCRIPT_DIR/server_health_report.py" "$@"
