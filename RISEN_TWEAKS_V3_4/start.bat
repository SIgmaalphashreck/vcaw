@echo off
setlocal
cd /d "%~dp0"
title RISEN TWEAKS V3.2
where py >nul 2>nul
if errorlevel 1 (
  echo Python Launcher (py.exe) wurde nicht gefunden.
  echo Installiere Python 3.10+ von https://www.python.org/downloads/ und aktiviere "Add Python to PATH".
  pause
  exit /b 1
)
echo [1/2] Installiere / aktualisiere Abhaengigkeiten ...
py -3 -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installation fehlgeschlagen. Pruefe deine Internetverbindung und Python-Installation.
  pause
  exit /b 1
)
echo [2/2] Starte RISEN TWEAKS ...
py -3 main.py
if errorlevel 1 (
  echo.
  echo RISEN TWEAKS wurde mit einem Fehler beendet. Details stehen im Log unter %%LOCALAPPDATA%%\RisenTweaks.
  pause
)
endlocal
