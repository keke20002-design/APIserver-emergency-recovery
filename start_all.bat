@echo off

:: Kill port 8000 (uvicorn)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)

:: Kill any existing cloudflared tunnel (avoid duplicate tunnel connections)
taskkill /IM cloudflared.exe /F >nul 2>&1

timeout /t 2 /nobreak >nul

start "API Server" cmd /k "cd /d C:\Users\admin\AndroidStudioProjects\APIserver && python -m uvicorn main:app --host 0.0.0.0 --port 8000"
start "Cloudflare Tunnel" cmd /k "cd /d C:\Users\admin\AndroidStudioProjects\BaseStadium && cloudflared tunnel run basestadium-api"
