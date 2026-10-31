@echo off
setlocal
cd /d "%~dp0"
title Build RISEN TWEAKS EXE
py -3 -m pip install --disable-pip-version-check -r requirements.txt pyinstaller
if errorlevel 1 goto error
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name RISEN_TWEAKS --collect-all customtkinter main.py
if errorlevel 1 goto error
echo.
echo Fertig: dist\RISEN_TWEAKS.exe
pause
exit /b 0
:error
echo Build fehlgeschlagen. Pruefe Python und Internetverbindung.
pause
exit /b 1
