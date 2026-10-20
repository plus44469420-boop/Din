@echo off
rem Double-click to open ps5cable Hub from source (needs Python 3.8+ from python.org).
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw hub.py & exit /b)
where pyw >nul 2>nul && (start "" pyw -3 hub.py & exit /b)
where py >nul 2>nul && (start "" py -3 hub.py & exit /b)
echo Python 3 was not found. Install it from https://www.python.org/downloads/
echo or use ps5cable-hub.exe instead.
pause
