#!/bin/bash
echo "Starting backend API..."
mkdir -p ~/.config/gmgn
if [ ! -z "$GMGN_PRIVATE_KEY" ]; then
    echo "GMGN_PRIVATE_KEY=\"$GMGN_PRIVATE_KEY\"" > ~/.config/gmgn/.env
    chmod 600 ~/.config/gmgn/.env
    echo "GMGN private key configured from environment."
fi
cd aitrader

echo "Starting KIDA Bot..."
cd ../kida_bot
./start.sh &
cd ../aitrader

echo "Starting backend API in foreground..."
exec python3 -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
