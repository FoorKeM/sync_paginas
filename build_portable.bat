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

for /f "usebackq tokens=*" %%v in (`python -c "from version import APP_VERSION; print(APP_VERSION)"`) do set "APP_VERSION=%%v"

set "PORTABLE=%~dp0portable\MercadohouseSync"
if exist "%PORTABLE%" rmdir /s /q "%PORTABLE%"
mkdir "%PORTABLE%"

copy /Y "%~dp0dist\MercadohouseSync.exe" "%PORTABLE%\MercadohouseSync.exe"
copy /Y "%~dp0dist\MercadohouseSync.exe" "%PORTABLE%\MercadohouseSync_%APP_VERSION%.exe"
echo %APP_VERSION%>"%PORTABLE%\VERSION.txt"

echo.
echo Listo:
echo %PORTABLE%\MercadohouseSync.exe
echo %PORTABLE%\MercadohouseSync_%APP_VERSION%.exe
echo.
pause
exit /b 0

:error
echo.
echo ERROR: No se pudo construir el portable.
echo.
pause
exit /b 1
