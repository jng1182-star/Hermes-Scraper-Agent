#!/bin/bash
# Double-click this file in Finder to start the app.
cd "$(dirname "$0")"

if ! python3 -c "import uvicorn" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt -q
fi

echo "Starting server → http://127.0.0.1:8000/dashboard/index.html"
echo "Press Ctrl+C to stop."

(sleep 2 && open "http://127.0.0.1:8000/dashboard/index.html") &
python3 -m uvicorn api:app --host 127.0.0.1 --port 8000
