@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-demo-lock.txt
.venv\Scripts\python.exe demo\run_demo.py %*
