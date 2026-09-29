#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT" || exit 1
echo "Starting KIDA Bot Engine from $PROJECT_ROOT..."
sleep 3
while true; do
    python3 -m kida_bot.main
    echo "Engine stopped. Restarting in 5 seconds..."
    sleep 5
done
