#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Creating virtual environment (first run)..."
    python3 -m venv .venv || {
        echo "venv creation failed. Run: sudo apt install python3-venv"
        exit 1
    }
fi
. .venv/bin/activate

echo "Checking dependencies (first run may take a minute)..."
pip install -r requirements.txt --quiet --disable-pip-version-check

if [ ! -f cert.pem ]; then
    echo "Generating HTTPS certificate for this PC..."
    python gen_cert.py
fi

echo
python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print('  Open on the tablet:  https://'+s.getsockname()[0]+':8600');s.close()"
echo

python -m uvicorn main:app --host 0.0.0.0 --port 8600 --reload --ssl-keyfile key.pem --ssl-certfile cert.pem
