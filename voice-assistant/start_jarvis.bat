@echo off
title J.A.R.V.I.S.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from python.org and CHECK "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Checking dependencies (first run may take a minute)...
python -m pip install -r requirements.txt --quiet --disable-pip-version-check

if not exist cert.pem (
    echo Generating HTTPS certificate for this PC...
    python gen_cert.py
)

echo.
python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print('  Open on the tablet:  https://'+s.getsockname()[0]+':8600');s.close()"
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8600 --reload --ssl-keyfile key.pem --ssl-certfile cert.pem
pause
