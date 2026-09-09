@echo off
rem 專案 CLI 包裝（Windows）：自動用 .venv 的 python，不用管路徑。
rem   nchu check | watch | login | logout
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "PYTHONUTF8=1"
"%PY%" -m nchu_course %*
