@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
rem Playwright 在系统 Temp 建临时目录可能被拒(EPERM)，固定到本目录
set TMP=%~dp0tmp
set TEMP=%~dp0tmp
if not exist "%TMP%" mkdir "%TMP%"
title 硬件比价 - 淘宝/京东/拼多多
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
"%PY%" run.py
if errorlevel 1 (
  echo.
  echo [启动失败] 请先安装依赖：
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt
  echo.
  pause
)
