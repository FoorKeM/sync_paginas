@echo off
setlocal
cd /d "%~dp0"

echo.
echo === Construyendo MercadohouseSync portable ===
echo.

python -m pip install pyinstaller
if errorlevel 1 goto error

python -m PyInstaller MercadohouseSync.spec --clean --noconfirm
if errorlevel 1 goto error

set "PORTABLE=%~dp0portable\MercadohouseSync"
if not exist "%PORTABLE%" mkdir "%PORTABLE%"

copy /Y "%~dp0dist\MercadohouseSync.exe" "%PORTABLE%\MHSync.exe"

echo.
echo Listo:
echo %PORTABLE%\MHSync.exe
echo.
pause
exit /b 0

:error
echo.
echo ERROR: No se pudo construir el portable.
echo.
pause
exit /b 1
