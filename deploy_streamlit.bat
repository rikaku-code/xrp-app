@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo  XRP Monitor - Streamlit Cloud 部署助手
echo ========================================
echo.
echo [1/3] 本地 Git 已就绪。请确保已有 GitHub 账号。
echo.
echo [2/3] 在 GitHub 创建私有仓库 xrp_monitor 后，执行：
echo   gh auth login
echo   gh repo create xrp_monitor --private --source=. --push
echo.
echo 若无 gh，可手动：
echo   git remote add origin https://github.com/你的用户名/xrp_monitor.git
echo   git branch -M main
echo   git push -u origin main
echo.
echo [3/3] 打开 Streamlit Cloud 部署页面：
start https://share.streamlit.io/
echo.
echo 部署时填写：
echo   Main file: streamlit_app.py
echo   Secrets 粘贴 .streamlit\secrets.toml.example 内容并填入 Webhook
echo.
pause
