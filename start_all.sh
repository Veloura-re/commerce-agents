#!/bin/bash
echo "Starting backend API..."
mkdir -p ~/.config/gmgn
touch ~/.config/gmgn/.env
chmod 600 ~/.config/gmgn/.env
if [ -z "$GMGN_API_KEY" ]; then
    export GMGN_API_KEY="gmgn_1bec3d8d9f6334b3dabbc45b64e0775f"
fi
if [ -z "$GMGN_PRIVATE_KEY" ]; then
    export GMGN_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIFqayitUTOObLxp9Roq1k40+H3Ah1kEpvQGIkybbLqkr\n-----END PRIVATE KEY-----"
fi

echo "GMGN_API_KEY=\"$GMGN_API_KEY\"" > ~/.config/gmgn/.env
echo "GMGN_PRIVATE_KEY=\"$GMGN_PRIVATE_KEY\"" >> ~/.config/gmgn/.env
chmod 600 ~/.config/gmgn/.env
echo "GMGN credentials configured in environment and ~/.config/gmgn/.env."

# Purge all fabricated session state on fresh deploy -- start from zero
echo "Resetting session state (clean slate)..."
mkdir -p aitrader/outputs
echo "[]" > aitrader/outputs/positions.json
echo "{}" > aitrader/outputs/session_audit.json
: > aitrader/outputs/brain_memory.jsonl
: > aitrader/outputs/trade_decisions.jsonl
rm -f aitrader/outputs/learned_heuristics.json
echo "Session state reset complete."

cd aitrader

echo "Starting KIDA Bot..."
cd ../kida_bot
./start.sh &
cd ../aitrader

echo "Starting backend API in foreground..."
exec python3 -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips "*"
