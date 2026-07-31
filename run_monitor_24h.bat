@echo off
cd /d "%~dp0"
:loop
python xrp_monitor.py
echo [%date% %time%] 监控进程退出，5 秒后自动重启...
timeout /t 5 /nobreak >nul
goto loop
