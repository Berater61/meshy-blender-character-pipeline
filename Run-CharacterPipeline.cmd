@echo off
setlocal
echo Diese Pipeline verwendet jetzt vorhandene Workspace-Jobs.
echo Verwende beispielsweise:
echo   Run-WorkspaceJob.cmd --latest-job --preflight
call "%~dp0Run-WorkspaceJob.cmd" %*
exit /b %ERRORLEVEL%
