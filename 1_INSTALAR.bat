@echo off
setlocal EnableDelayedExpansion
title Instalador - Sincronizador Mercadohouse
color 0A
cls

echo.
echo  ===========================================================
echo   INSTALADOR - SINCRONIZADOR MERCADOHOUSE
echo  ===========================================================
echo.
echo  Este instalador verificara e instalara todo lo necesario.
echo  Si algo ya esta instalado, lo saltara automaticamente.
echo.
pause

:: ============================================================
:: PASO 1 — VERIFICAR / INSTALAR PYTHON
:: ============================================================
echo.
echo  [1/4] Verificando Python...

call :find_python
if "!PYTHON_EXE!" neq "" (
    for /f "tokens=*" %%v in ('"!PYTHON_EXE!" --version 2^>^&1') do echo        %%v ya instalado. OK, saltando.
    goto :check_pip
)

echo        Python no encontrado. Instalando...

:: Intentar con winget primero
winget --version >nul 2>&1
if %errorlevel% == 0 (
    echo        Instalando Python via winget...
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    echo        Instalacion winget completada.
) else (
    echo        Descargando Python con PowerShell...
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe' -OutFile '%TEMP%\python_installer.exe'" 2>nul
    if exist "%TEMP%\python_installer.exe" (
        echo        Ejecutando instalador de Python...
        "%TEMP%\python_installer.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1
        del "%TEMP%\python_installer.exe" >nul 2>&1
        echo        Instalacion completada.
    ) else (
        echo        No se pudo descargar Python.
    )
)

:: Refrescar PATH desde el registro de Windows para encontrar Python recien instalado
echo        Actualizando PATH del sistema...
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USR_PATH=%%B"
if defined SYS_PATH set "PATH=!SYS_PATH!"
if defined USR_PATH set "PATH=!PATH!;!USR_PATH!"

:: Agregar rutas tipicas de Python por si acaso
set "PATH=!PATH!;%LOCALAPPDATA%\Programs\Python\Python312"
set "PATH=!PATH!;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
set "PATH=!PATH!;C:\Python312"
set "PATH=!PATH!;C:\Python312\Scripts"
set "PATH=!PATH!;C:\Program Files\Python312"
set "PATH=!PATH!;C:\Program Files\Python312\Scripts"

:: Buscar Python de nuevo con el PATH actualizado
call :find_python
if "!PYTHON_EXE!" == "" (
    echo.
    echo  AVISO: Python fue instalado pero esta ventana CMD no lo detecta todavia.
    echo  Esto es normal — el PATH se actualiza en la proxima sesion.
    echo.
    echo  Por favor:
    echo    1. Cierra esta ventana
    echo    2. Abre una CMD nueva como Administrador
    echo    3. Vuelve a ejecutar 1_INSTALAR.bat
    echo.
    pause
    exit /b 0
)
echo        Python encontrado en: !PYTHON_EXE!

:check_pip
:: ============================================================
:: PASO 2 — VERIFICAR / ACTUALIZAR PIP
:: ============================================================
echo.
echo  [2/4] Verificando pip...
"!PYTHON_EXE!" -m pip --version >nul 2>&1
if %errorlevel% == 0 (
    echo        pip OK. Actualizando...
    "!PYTHON_EXE!" -m pip install --upgrade pip --quiet
) else (
    echo        Instalando pip...
    "!PYTHON_EXE!" -m ensurepip --upgrade
)
echo        pip listo.

:: ============================================================
:: PASO 3 — VERIFICAR / INSTALAR PLAYWRIGHT
:: ============================================================
echo.
echo  [3/4] Verificando Playwright...
"!PYTHON_EXE!" -c "import playwright" >nul 2>&1
if %errorlevel% == 0 (
    echo        Playwright ya instalado. Actualizando...
    "!PYTHON_EXE!" -m pip install --upgrade playwright --quiet
) else (
    echo        Instalando Playwright...
    "!PYTHON_EXE!" -m pip install playwright
    if %errorlevel% neq 0 (
        echo.
        echo  ERROR: No se pudo instalar Playwright.
        echo  Verifica tu conexion a internet e intenta de nuevo.
        pause
        exit /b 1
    )
)
echo        Playwright listo.

:: ============================================================
:: PASO 4 — VERIFICAR / INSTALAR CHROMIUM
:: ============================================================
echo.
echo  [4/4] Verificando Chromium...

set "CHROMIUM_OK=0"
for /f "delims=" %%d in ('"!PYTHON_EXE!" -c "import os; print(os.path.join(os.path.expanduser(chr(126)),chr(65)+chr(112)+chr(112)+chr(68)+chr(97)+chr(116)+chr(97),chr(76)+chr(111)+chr(99)+chr(97)+chr(108),chr(109)+chr(115)+chr(45)+chr(112)+chr(108)+chr(97)+chr(121)+chr(119)+chr(114)+chr(105)+chr(103)+chr(104)+chr(116)))" 2^>nul') do set "PW_DIR=%%d"

if defined PW_DIR (
    if exist "!PW_DIR!" (
        dir /b /s "!PW_DIR!\chromium*\chrome-win\chrome.exe" >nul 2>&1
        if !errorlevel! == 0 set "CHROMIUM_OK=1"
    )
)

if "!CHROMIUM_OK!"=="1" (
    echo        Chromium ya instalado. OK, saltando.
) else (
    echo        Instalando Chromium ^(puede tardar varios minutos^)...
    "!PYTHON_EXE!" -m playwright install chromium
    if %errorlevel% neq 0 (
        echo.
        echo  ERROR: No se pudo instalar Chromium.
        echo  Verifica tu conexion a internet e intenta de nuevo.
        pause
        exit /b 1
    )
    set "CHROMIUM_OK=1"
    echo        Chromium instalado correctamente.
)

:: ============================================================
:: CREAR CARPETA DE DESCARGAS
:: ============================================================
echo.
echo  Verificando carpeta de descargas...
if not exist "C:\Precios\descargas" (
    mkdir "C:\Precios\descargas"
    echo        Carpeta C:\Precios\descargas creada.
) else (
    echo        Carpeta C:\Precios\descargas ya existe. OK.
)

:: ============================================================
:: VERIFICACION FINAL
:: ============================================================
echo.
echo  ===========================================================
echo   VERIFICACION FINAL
echo  ===========================================================
echo.

set "TODO_OK=1"

"!PYTHON_EXE!" --version >nul 2>&1
if %errorlevel% == 0 (
    for /f "tokens=*" %%v in ('"!PYTHON_EXE!" --version 2^>^&1') do echo   [OK] Python       -  %%v
) else (
    echo   [!!] Python       -  NO ENCONTRADO
    set "TODO_OK=0"
)

"!PYTHON_EXE!" -c "import playwright" >nul 2>&1
if %errorlevel% == 0 (
    "!PYTHON_EXE!" -m playwright --version > "%TEMP%\pw_ver.txt" 2>nul
    set /p PW_VER=<"%TEMP%\pw_ver.txt"
    del "%TEMP%\pw_ver.txt" >nul 2>&1
    echo   [OK] Playwright   -  !PW_VER!
) else (
    echo   [!!] Playwright   -  NO ENCONTRADO
    set "TODO_OK=0"
)

if "!CHROMIUM_OK!"=="1" (
    echo   [OK] Chromium     -  instalado
) else (
    echo   [!!] Chromium     -  NO ENCONTRADO
    set "TODO_OK=0"
)

if exist "C:\Precios\descargas" (
    echo   [OK] Carpeta      -  C:\Precios\descargas
) else (
    echo   [!!] Carpeta      -  NO CREADA
    set "TODO_OK=0"
)

echo.
if "!TODO_OK!"=="1" (
    echo  ===========================================================
    echo   TODO INSTALADO CORRECTAMENTE
    echo   Ya puedes usar INICIAR.bat
    echo  ===========================================================
) else (
    echo  ===========================================================
    echo   ATENCION: Algunos componentes no quedaron bien.
    echo   Cierra y vuelve a ejecutar este instalador como Admin.
    echo  ===========================================================
)
echo.
pause
exit /b 0

:: ============================================================
:: FUNCION: Buscar Python en rutas conocidas
:: ============================================================
:find_python
set "PYTHON_EXE="

:: Intentar python del PATH actual
python --version >nul 2>&1
if %errorlevel% == 0 (
    set "PYTHON_EXE=python"
    goto :eof
)

:: Intentar python3
python3 --version >nul 2>&1
if %errorlevel% == 0 (
    set "PYTHON_EXE=python3"
    goto :eof
)

:: Buscar en rutas tipicas de instalacion
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
) do (
    if exist %%p (
        set "PYTHON_EXE=%%~p"
        goto :eof
    )
)
goto :eof
