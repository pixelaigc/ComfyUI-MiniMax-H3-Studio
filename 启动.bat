@echo off
chcp 65001 >nul
title H3 Studio
cd /d %~dp0

echo ============================================
echo   H3 Studio - 本地视频生成工作台
echo ============================================

REM ====== 按需修改：你的 ComfyUI 安装目录（含 python_embeded 的那层） ======
set "COMFY_DIR=D:\ComfyUI"

REM 检查 ComfyUI 是否在运行
curl -s -m 3 http://127.0.0.1:8188/system_stats >nul 2>&1
if errorlevel 1 (
  echo [!] 未检测到 ComfyUI ^(127.0.0.1:8188^)
  echo     请先启动 ComfyUI，再运行本脚本；也可以选择继续启动 Studio（页面会提示未连接）
  choice /C YN /M "仍要启动 Studio 吗"
  if errorlevel 2 exit /b 1
)

REM 优先用 ComfyUI 自带 python（已含 aiohttp，零依赖）
set PY=%COMFY_DIR%\python_embeded\python.exe
if not exist "%PY%" (
  for /d %%D in (D:\ComfyUI* C:\ComfyUI*) do (
    if exist "%%D\python_embeded\python.exe" set PY=%%D\python_embeded\python.exe
  )
)
if not exist "%PY%" set PY=python

echo [*] 使用 Python: %PY%
echo [*] 启动后，浏览器打开 http://127.0.0.1:8190
echo.
"%PY%" server.py
pause
