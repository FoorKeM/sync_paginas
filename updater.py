"""
updater.py — Auto-actualizacion via GitHub Releases.

Repo publico (FoorKeM/sync_paginas): la API de releases no requiere token.
Solo funciona reemplazando el .exe cuando la app corre congelada (PyInstaller);
en modo desarrollo (python menu.py) el chequeo se omite en menu.py.
"""

import json
import os
import ssl
import subprocess
import sys
import traceback
import urllib.request
from pathlib import Path

GITHUB_API_LATEST = "https://api.github.com/repos/FoorKeM/sync_paginas/releases/latest"
REQUEST_TIMEOUT = 6


def _contexto_ssl():
    """Contexto SSL con el bundle de certifi.

    PyInstaller no siempre logra que el .exe congelado use el almacen de
    certificados de Windows via el modulo ssl (aunque en modo desarrollo
    funciona perfecto); certifi empaqueta su propio cacert.pem y evita
    depender de eso.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _log_error_red(origen: str, exc: Exception) -> None:
    """Deja rastro de fallos de red del actualizador para poder diagnosticarlos despues."""
    try:
        from app_paths import RUNTIME_DIR
        log_path = RUNTIME_DIR / "updater_debug.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"--- {origen} ---\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def _version_tuple(version_str: str) -> tuple[int, int, int]:
    """Extrae (major, minor, patch) de algo como 'v1.8.2-2026-08-07' o 'v1.9.0'."""
    core = version_str.strip().lstrip("vV").split("-")[0]
    numeros = []
    for parte in core.split("."):
        try:
            numeros.append(int(parte))
        except ValueError:
            numeros.append(0)
    while len(numeros) < 3:
        numeros.append(0)
    return tuple(numeros[:3])


def buscar_release_nuevo(version_actual: str) -> dict | None:
    """Consulta el ultimo release publico de GitHub.

    Devuelve un dict con version/notas/url_descarga/nombre_archivo si hay una
    version mas nueva con un .exe adjunto, o None si no hay nada nuevo o si
    la consulta falla (sin internet, GitHub caido, etc.).
    """
    try:
        req = urllib.request.Request(
            GITHUB_API_LATEST,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "MercadohouseSync-Updater"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=_contexto_ssl()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log_error_red("buscar_release_nuevo", exc)
        return None

    tag = data.get("tag_name") or ""
    if not tag or _version_tuple(tag) <= _version_tuple(version_actual):
        return None

    assets = data.get("assets") or []
    exe_asset = next((a for a in assets if a.get("name", "").lower().endswith(".exe")), None)
    if not exe_asset:
        return None

    return {
        "version": tag,
        "notas": (data.get("body") or "").strip(),
        "url_descarga": exe_asset["browser_download_url"],
        "nombre_archivo": exe_asset["name"],
    }


def descargar_actualizacion(url: str, destino: Path, progreso_fn=None) -> None:
    """Descarga el .exe del release al archivo destino.

    progreso_fn(bytes_descargados, bytes_totales) se llama despues de cada
    bloque leido, si se entrega. bytes_totales es 0 si el servidor no informo
    Content-Length.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "MercadohouseSync-Updater"})
    with urllib.request.urlopen(req, timeout=120, context=_contexto_ssl()) as resp, open(destino, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        descargado = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            descargado += len(chunk)
            if progreso_fn:
                progreso_fn(descargado, total)


def aplicar_actualizacion_y_reiniciar(nuevo_exe: Path, nombre_final: str) -> None:
    """Reemplaza el .exe actual por el descargado, con el nombre de la version nueva, y reinicia.

    El archivo final queda en la misma carpeta que el .exe actual pero con el
    nombre del release (ej. MHSync_V1.9.1.exe), no con el nombre viejo. Termina
    el proceso actual; el reemplazo y el reinicio los hace un .bat auxiliar
    porque Windows no permite sobrescribir un .exe mientras esta corriendo.
    """
    if not getattr(sys, "frozen", False):
        raise Exception("La auto-actualizacion solo esta disponible en la version .exe.")

    exe_actual = Path(sys.executable).resolve()
    exe_destino = exe_actual.parent / nombre_final
    bat_path = exe_actual.parent / "_actualizar_mhsync.bat"
    log_path = exe_actual.parent / "_actualizar_mhsync_log.txt"
    pid_actual = os.getpid()
    bat_contenido = (
        "@echo off\r\n"
        f'echo [%date% %time%] Esperando cierre de PID {pid_actual}... >> "{log_path}"\r\n'
        ":esperar\r\n"
        f'tasklist /FI "PID eq {pid_actual}" 2>NUL | find "{pid_actual}" >NUL\r\n'
        "if not errorlevel 1 (\r\n"
        "    timeout /t 1 /nobreak >NUL\r\n"
        "    goto esperar\r\n"
        ")\r\n"
        f'echo [%date% %time%] Proceso cerrado. Reemplazando archivo... >> "{log_path}"\r\n'
        "set BORRAR_INTENTOS=0\r\n"
        ":borrar_viejo\r\n"
        f'if /I "{exe_actual}"=="{exe_destino}" goto mover\r\n'
        f'if not exist "{exe_actual}" goto mover\r\n'
        f'del /F /Q "{exe_actual}" >> "{log_path}" 2>&1\r\n'
        f'if exist "{exe_actual}" (\r\n'
        "    set /a BORRAR_INTENTOS+=1\r\n"
        "    if %BORRAR_INTENTOS% lss 5 (\r\n"
        "        timeout /t 1 /nobreak >NUL\r\n"
        "        goto borrar_viejo\r\n"
        "    )\r\n"
        f'    echo [%date% %time%] ADVERTENCIA: no se pudo borrar el archivo viejo tras 5 intentos. >> "{log_path}"\r\n'
        ")\r\n"
        ":mover\r\n"
        f'move /Y "{nuevo_exe}" "{exe_destino}" >> "{log_path}" 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        f'    echo [%date% %time%] ERROR: no se pudo mover el archivo nuevo. >> "{log_path}"\r\n'
        "    goto fin\r\n"
        ")\r\n"
        f'echo [%date% %time%] Archivo reemplazado como {nombre_final}. Reabriendo... >> "{log_path}"\r\n'
        f'start "MercadohouseSync" "{exe_destino}"\r\n'
        f'echo [%date% %time%] Comando start ejecutado (errorlevel %errorlevel%). >> "{log_path}"\r\n'
        ":fin\r\n"
        'del "%~f0"\r\n'
    )
    bat_path.write_text(bat_contenido, encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat_path)], creationflags=subprocess.CREATE_NEW_CONSOLE)
    sys.exit(0)
