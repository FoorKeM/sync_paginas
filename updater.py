"""
updater.py — Auto-actualizacion via GitHub Releases.

Repo publico (FoorKeM/sync_paginas): la API de releases no requiere token.
Solo funciona reemplazando el .exe cuando la app corre congelada (PyInstaller);
en modo desarrollo (python menu.py) el chequeo se omite en menu.py.
"""

import csv
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

from app_paths import DATA_DIR

GITHUB_API_LATEST = "https://api.github.com/repos/FoorKeM/sync_paginas/releases/latest"
REQUEST_TIMEOUT = 12
NOMBRE_INSTALADOR_TRANSITORIO = "MHSync_Instalar.exe"


def otras_versiones_abiertas() -> list[str]:
    """Nombres de otros ejecutables MHSync activos en esta sesión de Windows."""
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        actual = Path(sys.executable).name.casefold()
        nombres = {
            row[0] for row in csv.reader(result.stdout.splitlines())
            if row and re.fullmatch(r"mhsync(?:_v[0-9.]+)?\.exe", row[0], re.IGNORECASE)
            and row[0].casefold() != actual
        }
        return sorted(nombres, key=str.casefold)
    except (OSError, subprocess.TimeoutExpired):
        return []


def cerrar_ejecutable(nombre: str) -> bool:
    """Cierra todas las instancias de un nombre MHSync validado."""
    if not re.fullmatch(r"MHSync(?:_V[0-9.]+)?\.exe", nombre, re.IGNORECASE):
        raise ValueError("Nombre de ejecutable MHSync no válido")
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/IM", nombre],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def limpiar_actualizacion_anterior(app_dir: Path, input_fn=input, print_fn=print) -> None:
    """Retira el exe viejo y el log que dejó el actualizador anterior al fallar."""
    app_dir = Path(app_dir).resolve()
    log_path = app_dir / "_actualizar_mhsync_log.txt"
    if not log_path.is_file():
        return
    try:
        contenido = log_path.read_text(encoding="utf-8", errors="replace")
        if "ADVERTENCIA: no se pudo borrar el archivo viejo" not in contenido:
            return
        viejo = next(
            (
                Path(line.strip()) for line in contenido.splitlines()
                if re.fullmatch(r"MHSync_V[0-9.]+\.exe", Path(line.strip()).name, re.IGNORECASE)
                and Path(line.strip()).parent.resolve() == app_dir
            ),
            None,
        )
        if viejo is None:
            return
        if viejo.exists():
            try:
                viejo.unlink()
            except OSError:
                print_fn(f"  ⚠️  La versión anterior ({viejo.name}) sigue en uso.")
                try:
                    respuesta = input_fn("  ¿Cerrar esa versión a la fuerza y limpiar la actualización? (s/n): ").strip().lower()
                except EOFError:
                    respuesta = "n"
                if respuesta != "s":
                    return
                cerrar_ejecutable(viejo.name)
                for _ in range(10):
                    try:
                        viejo.unlink()
                        break
                    except OSError:
                        time.sleep(0.5)
                if viejo.exists():
                    print_fn(f"  ❌  No se pudo borrar {viejo.name}. Cierra esa versión y vuelve a intentar.")
                    return
        log_path.unlink()
        print_fn("  ✓ Actualización anterior limpiada; quedó solo MHSync.exe.")
    except OSError as exc:
        print_fn(f"  ⚠️  No se pudo limpiar la actualización anterior: {exc}")


def finalizar_instalacion_transitoria(input_fn=input, print_fn=print) -> None:
    """Convierte el nombre temporal del release en MHSync.exe sin pisar un exe activo."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("La instalación transitoria solo funciona desde el .exe")
    actual = Path(sys.executable).resolve()
    if actual.name.casefold() != NOMBRE_INSTALADOR_TRANSITORIO.casefold():
        raise RuntimeError("No se está ejecutando el instalador transitorio")
    destino = actual.with_name("MHSync.exe")

    if destino.exists():
        try:
            destino.unlink()
        except OSError:
            print_fn("  La versión anterior de MHSync sigue abierta o bloqueada.")
            try:
                respuesta = input_fn("  ¿Cerrarla a la fuerza para completar la actualización? (s/n): ").strip().lower()
            except EOFError:
                respuesta = "n"
            if respuesta != "s":
                raise RuntimeError("Cierra MHSync.exe y abre de nuevo MHSync_Instalar.exe")
            cerrar_ejecutable("MHSync.exe")
            for _ in range(20):
                try:
                    destino.unlink()
                    break
                except OSError:
                    time.sleep(0.5)
            if destino.exists():
                raise RuntimeError("Windows aún no permite reemplazar MHSync.exe")

    shutil.copy2(actual, destino)
    subprocess.Popen(
        [str(destino), "--limpiar-instalador", str(actual)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    log_anterior = actual.parent / "_actualizar_mhsync_log.txt"
    try:
        log_anterior.unlink(missing_ok=True)
    except OSError:
        pass
    print_fn("  Actualización instalada como MHSync.exe. Abriendo nueva versión...")
    sys.exit(0)


def limpiar_instalador_transitorio(app_dir: Path, archivo: Path) -> bool:
    """Borra la copia temporal después de que arranca el MHSync.exe definitivo."""
    app_dir = Path(app_dir).resolve()
    archivo = Path(archivo).resolve()
    if archivo.parent != app_dir or archivo.name.casefold() != NOMBRE_INSTALADOR_TRANSITORIO.casefold():
        return False
    for _ in range(60):
        try:
            archivo.unlink(missing_ok=True)
            log_anterior = app_dir / "_actualizar_mhsync_log.txt"
            try:
                log_anterior.unlink(missing_ok=True)
            except OSError:
                pass
            return True
        except OSError:
            time.sleep(0.5)
    return False


class ErrorConsultaActualizacion(Exception):
    """Fallo real al consultar GitHub (red, SSL, DNS...), distinto de 'no hay version nueva'."""


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
    version mas nueva con un .exe adjunto, o None si no hay ninguna version
    mas nueva. Lanza ErrorConsultaActualizacion si la consulta en si misma
    falla (red, SSL, GitHub caido, etc.) para que el llamador pueda avisar
    en pantalla en vez de fallar en silencio.
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
        raise ErrorConsultaActualizacion(f"{type(exc).__name__}: {exc}") from exc

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
    """Reemplaza el .exe actual por el descargado y lo reinicia.

    El archivo final queda en la misma carpeta que el .exe actual pero con el
    nombre del archivo adjunto al release (actualmente MHSync.exe). Termina
    el proceso actual; el reemplazo y el reinicio los hace un .bat auxiliar
    porque Windows no permite sobrescribir un .exe mientras esta corriendo.
    """
    if not getattr(sys, "frozen", False):
        raise Exception("La auto-actualizacion solo esta disponible en la version .exe.")

    exe_actual = Path(sys.executable).resolve()
    exe_destino = exe_actual.parent / nombre_final
    proceso_esperado = "MHSync.exe" if nombre_final.casefold() == NOMBRE_INSTALADOR_TRANSITORIO.casefold() else nombre_final
    bat_path = exe_actual.parent / "_actualizar_mhsync.bat"
    log_path = DATA_DIR / "_actualizar_mhsync_log.txt"
    pid_actual = os.getpid()
    bat_contenido = (
        "@echo off\r\n"
        "setlocal enabledelayedexpansion\r\n"
        f'echo [%date% %time%] Esperando cierre de PID {pid_actual}... > "{log_path}"\r\n'
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
        "    if !BORRAR_INTENTOS! lss 5 (\r\n"
        "        timeout /t 1 /nobreak >NUL\r\n"
        "        goto borrar_viejo\r\n"
        "    )\r\n"
        f'    echo [%date% %time%] ERROR: no se pudo borrar el archivo viejo tras !BORRAR_INTENTOS! intentos. Cierra otras ventanas de MHSync. >> "{log_path}"\r\n'
        "    goto fin\r\n"
        ")\r\n"
        ":mover\r\n"
        "set MOVER_INTENTOS=0\r\n"
        ":reintentar_mover\r\n"
        f'move /Y "{nuevo_exe}" "{exe_destino}" >> "{log_path}" 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        "    set /a MOVER_INTENTOS+=1\r\n"
        "    if !MOVER_INTENTOS! lss 20 (\r\n"
        "        timeout /t 1 /nobreak >NUL\r\n"
        "        goto reintentar_mover\r\n"
        "    )\r\n"
        f'    echo [%date% %time%] ERROR: no se pudo mover el archivo nuevo tras !MOVER_INTENTOS! intentos. >> "{log_path}"\r\n'
        "    goto fin\r\n"
        ")\r\n"
        f'echo [%date% %time%] Archivo reemplazado como {nombre_final}. Reabriendo... >> "{log_path}"\r\n'
        "set REABRIR_INTENTOS=0\r\n"
        ":reabrir\r\n"
        "set /a REABRIR_INTENTOS+=1\r\n"
        f'start "" /D "{exe_destino.parent}" "{exe_destino}"\r\n'
        "timeout /t 5 /nobreak >NUL\r\n"
        f'tasklist /FI "IMAGENAME eq {proceso_esperado}" /FO CSV /NH 2>NUL | findstr /I /C:"{proceso_esperado}" >NUL\r\n'
        "if errorlevel 1 (\r\n"
        f'    echo [%date% %time%] Intento !REABRIR_INTENTOS!: {proceso_esperado} no quedo abierto. >> "{log_path}"\r\n'
        "    if !REABRIR_INTENTOS! lss 3 goto reabrir\r\n"
        f'    echo [%date% %time%] ERROR: {proceso_esperado} no se pudo reabrir tras 3 intentos. >> "{log_path}"\r\n'
        "    goto fin\r\n"
        ")\r\n"
        f'echo [%date% %time%] {proceso_esperado} quedo abierto tras !REABRIR_INTENTOS! intento(s). >> "{log_path}"\r\n'
        ":fin\r\n"
        'del "%~f0"\r\n'
    )
    bat_path.write_text(bat_contenido, encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat_path)], creationflags=subprocess.CREATE_NO_WINDOW)
    sys.exit(0)
