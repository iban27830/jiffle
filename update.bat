@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
if errorlevel 1 goto :failure_directory

set "DOWNLOAD_URL=https://github.com/iban27830/jiffle/archive/refs/heads/master.zip"
set "DATA_ROOT=%JIFFLE_DATA_ROOT%"
if not defined DATA_ROOT set "DATA_ROOT=%~dp0jiffle-data"
set "BACKUP_DIR="
set "UPDATE_ROOT=%TEMP%\jiffle-update-%RANDOM%-%RANDOM%"
set "ARCHIVE_PATH=!UPDATE_ROOT!\jiffle.zip"
set "STAGING_ROOT=!UPDATE_ROOT!\staging"
set "SOURCE_ROOT="

echo Jiffle updater
echo.

where powershell >nul 2>&1
if errorlevel 1 goto :failure_powershell

where python >nul 2>&1
if errorlevel 1 goto :failure_python

netstat -ano | findstr /R /C:":5001 .*LISTENING" >nul
if not errorlevel 1 goto :failure_running

call :backup_database
if errorlevel 1 goto :failure_backup

if not exist "!UPDATE_ROOT!" mkdir "!UPDATE_ROOT!"
if not exist "!UPDATE_ROOT!" goto :failure_temp
if not exist "!STAGING_ROOT!" mkdir "!STAGING_ROOT!"
if not exist "!STAGING_ROOT!" goto :failure_temp

echo Downloading the latest version from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri $env:DOWNLOAD_URL -OutFile $env:ARCHIVE_PATH"
if errorlevel 1 goto :failure_download
if not exist "!ARCHIVE_PATH!" goto :failure_download

echo Extracting the update...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; Expand-Archive -LiteralPath $env:ARCHIVE_PATH -DestinationPath $env:STAGING_ROOT -Force"
if errorlevel 1 goto :failure_extract

for /d %%D in ("!STAGING_ROOT!\*") do if not defined SOURCE_ROOT set "SOURCE_ROOT=%%~fD"
if not defined SOURCE_ROOT goto :failure_archive
if not exist "!SOURCE_ROOT!\jiffle" goto :failure_archive
if not exist "!SOURCE_ROOT!\run.py" goto :failure_archive

echo Checking the downloaded Python files...
python -m compileall "!SOURCE_ROOT!\jiffle" "!SOURCE_ROOT!\run.py"
if errorlevel 1 goto :failure_compile

echo Installing the update...
robocopy "!SOURCE_ROOT!" "%CD%" /E /R:2 /W:1 /COPY:DAT /DCOPY:DAT /XD __pycache__ /XF settings.json update.bat *.pyc
set "ROBOCOPY_EXIT=!errorlevel!"
if !ROBOCOPY_EXIT! GEQ 8 goto :failure_install

call :cleanup
echo.
echo Update completed. Pending database migrations will run automatically when Jiffle starts.
if defined BACKUP_DIR echo Database backup: %BACKUP_DIR%\jiffle-v2.db
echo Starting Jiffle...
call .\run.bat
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

:cleanup
if defined UPDATE_ROOT if exist "!UPDATE_ROOT!" rmdir /s /q "!UPDATE_ROOT!" >nul 2>&1
exit /b 0

:failure_directory
echo Could not switch to the Jiffle folder.
goto :failure

:failure_powershell
echo PowerShell was not found. Windows PowerShell 5.1 or newer is required for updates.
goto :failure

:failure_python
echo Python was not found. Install Python 3.11 or newer and try again.
goto :failure

:failure_running
echo Jiffle is still running on port 5001. Close its terminal and try again.
goto :failure

:failure_backup
echo The database backup failed. The update was cancelled.
goto :failure

:failure_temp
echo Could not create a temporary folder for the downloaded update.
goto :failure

:failure_download
echo Could not download the update from GitHub. Check the internet connection and GitHub access.
goto :failure

:failure_extract
echo The downloaded update could not be opened as a ZIP archive.
goto :failure

:failure_archive
echo The downloaded archive does not contain a valid Jiffle application.
goto :failure

:failure_compile
echo The downloaded source failed the syntax check. Jiffle was not changed.
goto :failure

:failure_install
echo The downloaded files could not be copied into the Jiffle folder.
goto :failure

:failure
call :cleanup
echo.
echo Update failed. No application restart was attempted.
pause
exit /b 1
