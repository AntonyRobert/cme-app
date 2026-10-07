@echo off
rem Start the local development server. Double-click, or run from any shell.
rem Uses the virtual environment's own Python, so nothing needs activating.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No virtual environment found. Create it first:
    echo     py -3.13 -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo No .env file found. Copy .env.example to .env and fill it in.
    pause
    exit /b 1
)

echo Applying any pending migrations...
".venv\Scripts\python.exe" manage.py migrate --check >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" manage.py migrate
    if errorlevel 1 (
        echo Migrations failed. Is PostgreSQL running?
        pause
        exit /b 1
    )
)

echo.
echo Admin: http://127.0.0.1:8000/admin/   (Ctrl+C stops the server)
echo.
start "" http://127.0.0.1:8000/admin/
".venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000
