@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
if errorlevel 1 goto :failure_directory

set "REMOTE=origin"
set "BRANCH=master"
set "REPOSITORY_URL=https://github.com/iban27830/jiffle.git"
set "DATA_ROOT=%JIFFLE_DATA_ROOT%"
if not defined DATA_ROOT set "DATA_ROOT=%~dp0jiffle-data"
set "BACKUP_DIR="

echo Jiffle updater
echo.

where git >nul 2>&1
if errorlevel 1 goto :failure_git

where python >nul 2>&1
if errorlevel 1 goto :failure_python

if not exist ".git\HEAD" goto :failure_repository

for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "CURRENT_BRANCH=%%B"
if /i not "%CURRENT_BRANCH%"=="%BRANCH%" goto :failure_branch

git diff --quiet
if errorlevel 1 goto :failure_dirty
git diff --cached --quiet
if errorlevel 1 goto :failure_staged

netstat -ano | findstr /R /C:":5001 .*LISTENING" >nul
if not errorlevel 1 goto :failure_running

git remote get-url %REMOTE% >nul 2>&1
if errorlevel 1 call :add_remote
if errorlevel 1 goto :failure_remote

call :backup_database
if errorlevel 1 goto :failure_backup

echo Fetching the latest version from GitHub...
git fetch --prune %REMOTE% %BRANCH%
if errorlevel 1 goto :failure_fetch

git merge --ff-only %REMOTE%/%BRANCH%
if errorlevel 1 goto :failure_merge

echo Checking the updated Python files...
python -m compileall jiffle run.py
if errorlevel 1 goto :failure_compile

echo.
echo Update completed. Pending database migrations will run automatically when Jiffle starts.
if defined BACKUP_DIR echo Database backup: %BACKUP_DIR%\jiffle-v2.db
echo Starting Jiffle...
call .\run.bat
exit /b %errorlevel%

:add_remote
echo The %REMOTE% remote is missing. Adding %REPOSITORY_URL%...
git remote add %REMOTE% "%REPOSITORY_URL%"
exit /b %errorlevel%

:backup_database
if not exist "%DATA_ROOT%\jiffle-v2.db" (
    echo No existing database was found. A backup is not needed for this installation.
    exit /b 0
)

for /f "delims=" %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "BACKUP_DIR=%DATA_ROOT%\migration-backups\update-%%I"
if not defined BACKUP_DIR exit /b 1
if not exist "!BACKUP_DIR!" mkdir "!BACKUP_DIR!"
if not exist "!BACKUP_DIR!" exit /b 1

echo Backing up the database to:
echo   !BACKUP_DIR!\jiffle-v2.db
python -c "import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()" "%DATA_ROOT%\jiffle-v2.db" "!BACKUP_DIR!\jiffle-v2.db"
if errorlevel 1 exit /b 1
exit /b 0

:failure_directory
echo Could not switch to the Jiffle folder.
goto :failure

:failure_git
echo Git was not found. Install Git for Windows and try again.
goto :failure

:failure_python
echo Python was not found. Install Python 3.11 or newer and try again.
goto :failure

:failure_repository
echo This folder is not a Git working copy.
goto :failure

:failure_branch
echo The current branch is "%CURRENT_BRANCH%". Switch to "%BRANCH%" before updating.
goto :failure

:failure_dirty
echo Tracked working-tree changes were found. Commit or stash them before updating.
goto :failure

:failure_staged
echo Staged changes were found. Commit or stash them before updating.
goto :failure

:failure_running
echo Jiffle is still running on port 5001. Close its terminal and try again.
goto :failure

:failure_remote
echo The GitHub remote could not be configured.
goto :failure

:failure_backup
echo The database backup failed. The update was cancelled.
goto :failure

:failure_fetch
echo Could not fetch updates. Check the internet connection and GitHub access.
goto :failure

:failure_merge
echo The update could not be applied as a fast-forward.
echo Resolve the branch state manually, then run this updater again.
goto :failure

:failure_compile
echo The updated source failed the syntax check. Jiffle was not started.
goto :failure

:failure
echo.
echo Update failed. No application restart was attempted.
pause
exit /b 1
