@echo off
cd /d "%~dp0"

echo Installing required AI dependencies (Gemini ^& Groq)...
.venv\Scripts\pip install google-genai groq >nul 2>&1

:: ── Window 1: Backend API ────────────────────────────────────────────────────
start "TEXTING Backend (API)" cmd /k "title TEXTING Backend (API :5000) && cd /d "%~dp0" && echo. && echo  [BACKEND] FastAPI running on http://localhost:5000 && echo  ---------------------------------------- && echo. && .venv\Scripts\python.exe -m uvicorn dashboard.app:app --host 0.0.0.0 --port 5000 --reload"

:: Give backend 3 seconds to start
timeout /t 3 >nul

:: ── Window 2: Cloudflare Tunnel ──────────────────────────────────────────────
start "TEXTING Cloudflare Tunnel" cmd /k "title TEXTING Cloudflare Tunnel && echo. && echo  [TUNNEL] Starting Cloudflare tunnel on port 5000... && echo  ---------------------------------------- && echo. && cloudflared tunnel --url http://localhost:5000"

:: ── Open browser ─────────────────────────────────────────────────────────────
start "" "http://localhost:5000"

:: Close this launcher window silently
exit
