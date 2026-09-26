#!/bin/bash
echo "Starting backend API..."
cd aitrader
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 &
cd ..

echo "Starting KIDA Bot..."
./start.sh
