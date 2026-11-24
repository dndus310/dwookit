@echo off
rem dwookit legacy launcher. dwo-Lens and dwo-Forge are now dwookit and this project's launcher is __dwookit.bat.
rem This file only forwards to it once, then deletes itself.
setlocal EnableExtensions
set "PROJ=%~dp0"
echo [dwookit] %~nx0 is the launcher of an earlier name: use __dwookit.bat (this file now removes itself).
if not exist "%PROJ%__dwookit.bat" (
  echo [dwookit] __dwookit.bat was not found. Run dwookit.bat in the dwookit folder and attach again.
  (goto) 2>nul & del "%~f0"
)
(goto) 2>nul & del "%~f0" & call "%PROJ%__dwookit.bat" %*
