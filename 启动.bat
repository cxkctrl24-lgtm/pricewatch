@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title 硬件比价 - 淘宝/京东/拼多多
cd /d "%~dp0"
python run.py
if errorlevel 1 (
  echo.
  echo [启动失败] 请先安装依赖：
  echo   python -m pip install -r requirements.txt
  echo.
  pause
)