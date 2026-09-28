#!/bin/bash
echo "Starting backend API..."
mkdir -p ~/.config/gmgn
touch ~/.config/gmgn/.env
chmod 600 ~/.config/gmgn/.env
if [ ! -z "$GMGN_PRIVATE_KEY" ]; then
    echo "GMGN_PRIVATE_KEY=\"$GMGN_PRIVATE_KEY\"" >> ~/.config/gmgn/.env
    echo "GMGN private key configured from environment."
fi
if [ ! -z "$GMGN_API_KEY" ]; then
    echo "GMGN_API_KEY=\"$GMGN_API_KEY\"" >> ~/.config/gmgn/.env
    echo "GMGN API key configured from environment."
fi

# Purge all fabricated session state on fresh deploy -- start from zero
echo "Resetting session state (clean slate)..."
echo "[]" > aitrader/outputs/positions.json
echo "{}" > aitrader/outputs/session_audit.json
: > aitrader/outputs/brain_memory.jsonl
: > aitrader/outputs/trade_decisions.jsonl
echo "Session state reset complete."

cd aitrader

echo "Starting KIDA Bot..."
cd ../kida_bot
./start.sh &
cd ../aitrader

echo "Starting backend API in foreground..."
exec python3 -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips "*"
