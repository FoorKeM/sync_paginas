@echo off
title MercadohouseSync
cd /d "%~dp0"
"portable\MercadohouseSync\MHSync.exe"
echo.
echo El programa termino. Presiona una tecla para cerrar esta ventana.
pause >nul
