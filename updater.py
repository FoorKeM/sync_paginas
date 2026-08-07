"""
updater.py — Auto-actualizacion via GitHub Releases.

Repo publico (FoorKeM/sync_paginas): la API de releases no requiere token.
Solo funciona reemplazando el .exe cuando la app corre congelada (PyInstaller);
en modo desarrollo (python menu.py) el chequeo se omite en menu.py.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_API_LATEST = "https://api.github.com/repos/FoorKeM/sync_paginas/releases/latest"
REQUEST_TIMEOUT = 6


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
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
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


def descargar_actualizacion(url: str, destino: Path) -> None:
    """Descarga el .exe del release al archivo destino."""
    req = urllib.request.Request(url, headers={"User-Agent": "MercadohouseSync-Updater"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(destino, "wb") as f:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)


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
        f'if /I not "{exe_actual}"=="{exe_destino}" if exist "{exe_actual}" del /F /Q "{exe_actual}" >> "{log_path}" 2>&1\r\n'
        f'move /Y "{nuevo_exe}" "{exe_destino}" >> "{log_path}" 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        f'    echo [%date% %time%] ERROR: no se pudo mover el archivo nuevo. >> "{log_path}"\r\n'
        "    goto fin\r\n"
        ")\r\n"
        f'echo [%date% %time%] Archivo reemplazado como {nombre_final}. Reabriendo... >> "{log_path}"\r\n'
        f'start "" "{exe_destino}"\r\n'
        f'echo [%date% %time%] Comando start ejecutado. >> "{log_path}"\r\n'
        ":fin\r\n"
        'del "%~f0"\r\n'
    )
    bat_path.write_text(bat_contenido, encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat_path)], creationflags=subprocess.CREATE_NO_WINDOW)
    sys.exit(0)
