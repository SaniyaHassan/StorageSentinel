#!/bin/bash
# StorageSentinel - Bash CLI Wrapper

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Run sentinel.py, passing along all command line arguments
python3 "$SCRIPT_DIR/sentinel.py" "$@"
