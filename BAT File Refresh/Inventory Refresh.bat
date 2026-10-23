@echo off
rem Inventory Refresh.bat - runs main.py (Walmart inventory and item report export).
rem The UiPath workflow "Markertplace Inventory Updater" calls the copy in
rem ".BAT File Refresh" about hourly with StartProcess (no arguments, no wait).
rem "BAT File Refresh\Inventory Refresh.bat" is a byte-identical copy: keep both the same.
rem
rem Pass 1 (no argument): relaunch this file minimized and return at once, so the
rem caller's window closes immediately and main.py runs in a minimized window.
rem Pass 2 (--worker): take the lock, run main.py with its output captured in the
rem "current run" file, then append that file and the exit code to the main log.
rem A second call while a run is in progress logs "already running" and exits.
rem (main.py cannot write straight into the main log: cmd opens redirect targets
rem without write sharing, so the log would be locked for the whole run.)

if /i "%~1"=="--worker" goto :worker
start /min "" "%~f0" --worker
exit /b 0

:worker
setlocal
set "logdir=G:\Documents And Logs"
set "logfile=%logdir%\Bat Walmart Logs.txt"
set "runlog=%logdir%\Bat Walmart Logs - current run.txt"
set "lockfile=%logdir%\Bat Walmart Logs.lock"
set "script=G:\Python Projects\WalmartInventoryPullAndMerge\main.py"
set "PYTHONIOENCODING=utf-8"

if not exist "%logdir%\" mkdir "%logdir%"

rem Lock: the .lock file is held open for writing on handle 9 for the whole run.
rem Windows refuses a second writer, so a second instance falls through with gotlock
rem unset. The handle is released when this window ends, even if it is killed, so a
rem crashed run never leaves a stale lock behind.
set "gotlock="
2>nul ( 9>"%lockfile%" ( set "gotlock=1" & call :run ) )
if not defined gotlock (
    >> "%logfile%" echo %date% %time%  already running - this call skipped
    exit /b 2
)
exit /b %rc%

:run
>> "%runlog%" echo.
>> "%runlog%" echo Starting Script at %date% %time%
call :pick_python
if not defined py (
    >> "%runlog%" echo ERROR: no Python with pandas and requests found - tried py -3.10, py, python, C:\Program Files\Python310 - main.py NOT run
    set "rc=9009"
    goto :finish
)
>> "%runlog%" echo Using: %py%
%py% -u -X utf8 "%script%" >> "%runlog%" 2>&1
set "rc=%errorlevel%"
:finish
>> "%runlog%" echo Finished at %date% %time% with exit code %rc%
type "%runlog%" >> "%logfile%" && del "%runlog%"
goto :eof

:pick_python
rem First interpreter that has pandas and requests wins.
set "py="
py -3.10 -c "import pandas, requests" >nul 2>&1 && set "py=py -3.10" && goto :eof
py -c "import pandas, requests" >nul 2>&1 && set "py=py" && goto :eof
python -c "import pandas, requests" >nul 2>&1 && set "py=python" && goto :eof
"C:\Program Files\Python310\python.exe" -c "import pandas, requests" >nul 2>&1 && set "py="C:\Program Files\Python310\python.exe"" && goto :eof
goto :eof
