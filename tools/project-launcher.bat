@echo off
rem ============================================================================
rem  dwookit project launcher. Placed in the project root by connect and removed
rem  by uninstall. It runs dwookit from its install folder (no copy of the system
rem  lives in the project).
rem    __dwookit.bat                     open dwookit (connect first if needed)
rem    tools                   tool versions and updates (--json for scripts)
rem    tool install^|update^|remove ^<lens^|statemonitor^|canvas^|builder^>   tool update --all
rem    DwoLens         analyze^|verify^|logs^|smoke^|perf^|sizes^|trace^|play
rem    DwoCanvas       canvas-validate ^<file^>^|canvas-import ^<file^> [--reset] [--doc Name]
rem    DwoBuilder      compile^|package^|ci-trigger [--reason text] [--stages compile,lens,package]^|ci-status
rem    connect^|status^|doctor^|update^|uninstall [--keep-data^|--delete-data] [--force]
rem ============================================================================
setlocal EnableExtensions
set "PROJ=%~dp0"
set "PROJ=%PROJ:~0,-1%"
set "CMD=%~1"
if "%CMD%"=="" (
  if exist "%PROJ%\__dwookit\state.json" (set "CMD=gui") else (set "CMD=connect")
)

rem ---- find the dwookit install folder: recorded at attach, else DWOOKIT_HOME (DWO_FORGE_HOME, DWO_LENS_HOME for older installs) ----
set "HOME_DIR="
if exist "%PROJ%\__dwookit\home.txt" set /p HOME_DIR=<"%PROJ%\__dwookit\home.txt"
rem home.txt is UTF-8; set /p reads it in the console code page (949 on Korean Windows), which breaks
rem non-ASCII folder names. Let PowerShell decode it; for /f gets it back in the console code page.
if not exist "%HOME_DIR%\tools\dwookit.ps1" if exist "%PROJ%\__dwookit\home.txt" (
  set "DWOOKIT_HOMEFILE=%PROJ%\__dwookit\home.txt"
  for /f "usebackq delims=" %%H in (`powershell -NoProfile -Command "[IO.File]::ReadAllText($env:DWOOKIT_HOMEFILE).Trim()"`) do set "HOME_DIR=%%H"
  set "DWOOKIT_HOMEFILE="
)
if not exist "%HOME_DIR%\tools\dwookit.ps1" set "HOME_DIR=%DWOOKIT_HOME%"
if not exist "%HOME_DIR%\tools\dwookit.ps1" set "HOME_DIR=%DWO_FORGE_HOME%"
if not exist "%HOME_DIR%\tools\dwookit.ps1" set "HOME_DIR=%DWO_LENS_HOME%"
if not exist "%HOME_DIR%\tools\dwookit.ps1" (
  echo [dwookit] dwookit was not found. Run dwookit.bat in the dwookit folder once, then try again.
  exit /b 1
)
dir /b "%PROJ%\*.uproject" >nul 2>&1 || (echo [dwookit] No .uproject next to this file. & exit /b 1)

set "DWOOKIT_FROM_BAT=1"
set "DWO_FORGE_FROM_BAT="
set "DWO_LENS_FROM_BAT="
cd /d "%PROJ%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HOME_DIR%\tools\dwookit.ps1" -Command %CMD% -Project "%PROJ%" %2 %3 %4 %5 %6 %7 %8 %9
set "RC=%ERRORLEVEL%"

rem 0 = clean, 3 = removed but something differs; either way dwookit is gone, so is this file.
set "GONE="
if "%RC%"=="0" set "GONE=1"
if "%RC%"=="3" set "GONE=1"
if /i "%CMD%"=="detach" set "CMD=uninstall"
if /i "%CMD%"=="uninstall" if defined GONE (
  echo [dwookit] Removing __dwookit.bat
  (goto) 2>nul & del "%~f0"
)
rem update replaces this file after it finished running
if exist "%PROJ%\__dwookit\launcher.next" (
  (goto) 2>nul & move /y "%PROJ%\__dwookit\launcher.next" "%~f0" >nul
)
exit /b %RC%
