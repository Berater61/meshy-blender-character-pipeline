@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIPELINE_ROOT=%~dp0"
set "PIPELINE_PYTHON=%PIPELINE_ROOT%.venv\Scripts\python.exe"

if not exist "%PIPELINE_PYTHON%" (
    echo Python-Umgebung fehlt: %PIPELINE_PYTHON%
    echo Bitte die Pipeline-Einrichtung erneut ausfuehren lassen.
    exit /b 1
)

"%PIPELINE_PYTHON%" "%PIPELINE_ROOT%run_pipeline.py" %*
exit /b %ERRORLEVEL%
