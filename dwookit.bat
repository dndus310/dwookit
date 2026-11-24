@echo off
rem ============================================================================
rem  dwookit. Double-click to open the control panel. No setup step is needed:
rem  the first run registers this folder and builds the window (about a minute).
rem    dwookit.bat                                open the control panel
rem    dwookit.bat <project folder or .uproject>  open it on that project (drag and drop works)
rem    dwookit.bat <command> <project folder> [options]
rem        attach, update, status, doctor, tools, uninstall [--keep-data^|--delete-data]
rem        Lens            analyze, verify, logs, smoke, perf, sizes, trace, play
rem        DwoStateMonitor sm-install [--runtime], sm-remove
rem        DwoCanvas       canvas-install, canvas-remove, canvas-validate ^<file^>,
rem                        canvas-import ^<file^> [--reset] [--doc Name]
rem        build           compile, package
rem    dwookit.bat ci-bundle ^| ci-install [--bundle zip] ^| ci-start ^| ci-stop ^| ci-status ^| ci-remove
rem        DwoBuilder (Jenkins on this PC, offline bundle). Per project: __dwookit.bat ci-job / ci-trigger
rem ============================================================================
setlocal EnableExtensions
set "HOME_DIR=%~dp0"
set "HOME_DIR=%HOME_DIR:~0,-1%"

rem ---- register this folder for __dwookit.bat launchers in projects ----
if /i not "%DWOOKIT_HOME%"=="%HOME_DIR%" (
  setx DWOOKIT_HOME "%HOME_DIR%" >nul 2>&1
  set "DWOOKIT_HOME=%HOME_DIR%"
)
rem launchers written by dwo-Forge (__dwo-forge.bat) look for DWO_FORGE_HOME until their first run migrates them
if defined DWO_FORGE_HOME if /i not "%DWO_FORGE_HOME%"=="%HOME_DIR%" setx DWO_FORGE_HOME "%HOME_DIR%" >nul 2>&1

set "ARG=%~1"
if "%ARG%"=="" goto :gui
if /i "%~x1"==".uproject" (
  set "ARG=%~dp1"
  goto :project
)
pushd "%ARG%" 2>nul && (popd & goto :project)

rem ---- DwoBuilder on this PC (no project needed): dwookit.bat ci-<command> [options] ----
set "C1=%~1"
if /i "%C1:~0,3%"=="ci-" goto :ci

rem ---- command line: dwookit.bat <command> <project> ----
set "PROJ=%~2"
if "%PROJ%"=="" set "PROJ=%CD%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HOME_DIR%\tools\dwookit.ps1" -Command %1 -Project "%PROJ%" %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:project
if "%ARG:~-1%"=="\" set "ARG=%ARG:~0,-1%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HOME_DIR%\tools\gui.ps1" -SystemDir "%HOME_DIR%" -ForgeHome "%HOME_DIR%" -Project "%ARG%"
if errorlevel 1 pause
exit /b

:gui
powershell -NoProfile -ExecutionPolicy Bypass -File "%HOME_DIR%\tools\gui.ps1" -SystemDir "%HOME_DIR%" -ForgeHome "%HOME_DIR%"
if errorlevel 1 pause
exit /b

:ci
powershell -NoProfile -ExecutionPolicy Bypass -File "%HOME_DIR%\tools\ci.ps1" -Command %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%
