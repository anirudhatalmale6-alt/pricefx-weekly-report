@echo off
setlocal enabledelayedexpansion
title Weekly PriceFx report

rem ---------------------------------------------------------------
rem  Double-click this file to build the weekly approved price-list
rem  report. No typing, no arguments needed.
rem
rem  With no arguments it does the Sunday-to-Saturday week that has
rem  FINISHED. Run it on a Sunday and you get the seven days that
rem  ended the night before.
rem
rem  To force a particular week, run it from a terminal with the
rem  Sunday that week starts:
rem      run_weekly.bat 2026-09-13
rem
rem  Everything printed - including any error - is saved to
rem  C:\Automation\output\pricefx_run_log.txt so it can be sent as a
rem  file instead of a photo of the screen.
rem ---------------------------------------------------------------

set "BASE=C:\Automation"
set "PY=%BASE%\venv\Scripts\python.exe"
set "SCRIPT=%BASE%\Weekly MDI report\pricefx_weekly.py"
set "CONFIG=%BASE%\Weekly MDI report\pricefx_config.ini"
set "LOGDIR=%BASE%\output"
set "LOG=%LOGDIR%\pricefx_run_log.txt"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo.
echo  Weekly PriceFx report
echo  ---------------------

rem Check the three things that can be missing, and say which one it is
rem rather than letting Windows print something unreadable.
if not exist "%PY%" (
    echo  PROBLEM: the virtual environment python is not where I expect it.
    echo  Looked for: %PY%
    goto :done
)
if not exist "%SCRIPT%" (
    echo  PROBLEM: the script is missing.
    echo  Looked for: %SCRIPT%
    goto :done
)
if not exist "%CONFIG%" (
    echo  PROBLEM: pricefx_config.ini is missing - that is the only file
    echo  your password lives in, so the script cannot sign in without it.
    echo  Looked for: %CONFIG%
    goto :done
)

if "%~1"=="" (
    echo  Week: the last full Sunday-to-Saturday week
) else (
    echo  Week: starting Sunday %~1
)
echo  Log:  %LOG%
echo.
echo  Working. This walks every approved price list, so give it a minute.
echo.

if "%~1"=="" (
    "%PY%" "%SCRIPT%" > "%LOG%" 2>&1
) else (
    "%PY%" "%SCRIPT%" --week %~1 > "%LOG%" 2>&1
)
set "RC=%ERRORLEVEL%"

type "%LOG%"

echo.
echo  ---------------------------------------------------------------
if "%RC%"=="0" (
    echo  Finished cleanly. The line above starting "Wrote" tells you
    echo  where the CSV is.
) else (
    echo  It stopped with an error - everything is in the log file.
    echo  Send me this file and I will not need a screenshot:
    echo     %LOG%
)
echo  ---------------------------------------------------------------

:done
echo.
pause
endlocal
