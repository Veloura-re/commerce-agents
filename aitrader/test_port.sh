PORT=${PORT:-8000}
echo $PORT
python3 -m uvicorn app:app --host 0.0.0.0 --port $PORT &
PID=$!
sleep 2
kill $PID
