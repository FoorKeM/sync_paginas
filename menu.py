"""
SINCRONIZADOR MERCADOHOUSE — MENÚ PRINCIPAL
Pasos en orden:
  1. Upload Precios    → sube Excel de precios a Tivendo POS
  2. Sync Artículos   → baja artículos de Tivendo POS y los sube a Mercadohouse
  3. Sync Precios MH  → baja lista de precios de Tivendo ERP y la sube a Mercadohouse
"""

import asyncio
import sys
import subprocess
import re
import os
from pathlib import Path
from datetime import datetime, date
from app_paths import APP_DIR, configure_console_encoding, configure_playwright_browsers, runtime_path
from utils import MODO_LIMPIO_ENV, borrar_sesion_guardada

HERE = APP_DIR
sys.path.insert(0, str(HERE))
configure_console_encoding()
configure_playwright_browsers()
import config as _cfg
from version import APP_VERSION, APP_NAME


# ── Importar los 3 módulos ─────────────────────────────────
try:
    import upload_precios
    _tiene_upload = True
except ImportError:
    _tiene_upload = False

try:
    import sync_articulos
    _tiene_articulos = True
except ImportError:
    _tiene_articulos = False

try:
    import sync_precios
    _tiene_precios = True
except ImportError:
    _tiene_precios = False

try:
    import sync_packs
    _tiene_packs = True
except ImportError:
    _tiene_packs = False


# ── Utilidades de pantalla ─────────────────────────────────
def clear():
    import os
    os.system("cls" if os.name == "nt" else "clear")


def tarea_hoy_info():
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", "SyncMercadohouseHoy", "/FO", "LIST"],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "Próxima" in line or "Next Run" in line:
                    return f"  ⏰  Programado hoy — {line.strip()}"
    except Exception:
        pass
    return ""


def banner():
    i1 = "✓" if _tiene_upload    else "✗"
    i2 = "✓" if _tiene_articulos else "✗"
    i3 = "✓" if _tiene_precios   else "✗"
    i4 = "✓" if _tiene_packs     else "✗"
    suc = _cfg.sucursal_activa()

    ancho = 60

    def caja(texto=""):
        if len(texto) > ancho:
            texto = texto[:ancho - 3] + "..."
        print(f"  ║{texto:<{ancho}}║")

    def linea(izq="╠", relleno="═", der="╣"):
        print(f"  {izq}{relleno * ancho}{der}")

    def separador():
        caja("  " + "─" * (ancho - 4) + "  ")

    def opcion(num, nombre, destino="", estado=""):
        izquierda = f"  {num}.  {nombre}"
        if estado:
            derecha = f"{destino}   [{estado}]".strip()
        else:
            derecha = destino

        if derecha:
            espacio = ancho - len(izquierda) - len(derecha)
            texto = izquierda + (" " * max(1, espacio)) + derecha
        else:
            texto = izquierda
        caja(texto)

    print(f"  Version: {APP_VERSION}")
    print()
    linea("╔", "═", "╗")
    caja("      SINCRONIZADOR MERCADOHOUSE")
    caja(f"      Sucursal: {suc['nombre']}")
    linea()
    opcion("1", "Upload Precios", "→ Tivendo POS", i1)
    opcion("2", "Sync Artículos", "Tivendo → MH", i2)
    opcion("3", "Sync Precios", "Tivendo → MH", i3)
    opcion("4", "Sync Packs", "Tivendo → MH", i4)
    separador()
    opcion("5", "COMPLETO MANUAL", "(1 → 2 → 4 → 3)")
    opcion("6", "SYNC ART + PRECIOS", "(2 → 3)")
    opcion("7", "SYNC PACKS + PRECIOS", "(4 → 3)")
    opcion("8", "SYNC TODO", "(2 → 4 → 3)")
    opcion("9", "Programar para HOY a la hora que elijas")
    separador()
    opcion("10", "Cambiar sucursal activa")
    opcion("11", "Editar credenciales (correos y claves)")
    opcion("12", "Salir")
    opcion("13", "Borrar sesion guardada")
    linea("╚", "═", "╝")
    info = tarea_hoy_info()
    if info:
        print(info)
    print()


def titulo(txt):
    print()
    print("  " + "═" * 54)
    print(f"  ▶  {txt}")
    print("  " + "═" * 54)


def esperar():
    input("\n  Presiona ENTER para volver al menú...")


def elegir_excel_precios_manual():
    """Abre el selector de archivo para elegir el Excel de precios."""
    print()
    print("  " + "-" * 54)
    print("  Elige el Excel de precios que se subira en el paso 1.")
    print("  (Se abrira el explorador de archivos...)")
    try:
        import tkinter as tk
        from tkinter import filedialog
        _root = tk.Tk()
        _root.withdraw()
        _root.wm_attributes("-topmost", True)
        excel_elegido = filedialog.askopenfilename(
            title="Selecciona el Excel de precios a subir",
            filetypes=[("Excel", "*.xlsx"), ("Todos los archivos", "*.*")],
        )
        _root.destroy()
    except Exception as _e:
        print(f"  No se pudo abrir el selector: {_e}")
        print("  Cancelado.")
        return None

    if not excel_elegido:
        print("  Cancelado. No se eligio ningun archivo.")
        return None

    print(f"  Archivo elegido: {Path(excel_elegido).name}")
    print("  " + "-" * 54)
    return excel_elegido


def imprimir_resumen_upload_precios():
    if not _tiene_upload:
        return
    resumen = getattr(upload_precios, "ULTIMO_RESULTADO_PRECIOS", None)
    if not resumen:
        return

    fallidos = resumen.get("fallidos") or []
    print()
    print("  RESULTADO CAMBIOS DE PRECIOS")
    print(f"  SE INGRESARON {resumen.get('total', 0)} CAMBIOS DE PRECIOS")
    print(f"  EXITO {resumen.get('ok', 0)}")
    print(f"  FALLIDOS {len(fallidos)}")
    if fallidos:
        print("  CODIGOS FALLIDOS:")
        for codigo in fallidos:
            print(f"    {codigo}")


# ── Ejecutor con reintentos automáticos ───────────────────
MAX_REINTENTOS = 3
ESPERA_REINTENTO = 30  # segundos entre reintentos

async def ejecutar_con_reintentos(nombre, fn_check, fn_run):
    """Llama a fn_run hasta MAX_REINTENTOS veces si falla."""
    if not fn_check():
        print(f"  ❌  Módulo no encontrado para: {nombre}")
        return False

    for intento in range(1, MAX_REINTENTOS + 1):
        os.environ[MODO_LIMPIO_ENV] = "1" if intento > 1 else "0"
        if intento > 1:
            print(f"\n  🔄  Reintentando {nombre}... (intento {intento}/{MAX_REINTENTOS})")
            print("      Borrando sesion guardada para entrar limpio...")
            borrar_sesion_guardada()
            print(f"      Esperando {ESPERA_REINTENTO}s antes de reintentar...")
            await asyncio.sleep(ESPERA_REINTENTO)
        try:
            await fn_run()
            os.environ.pop(MODO_LIMPIO_ENV, None)
            return True
        except SystemExit as e:
            ok = int(str(e)) == 0
            if ok:
                os.environ.pop(MODO_LIMPIO_ENV, None)
                return True
            if intento < MAX_REINTENTOS:
                print(f"  ⚠️   Intento {intento} falló, reintentando...")
            else:
                print(f"  ❌  Falló después de {MAX_REINTENTOS} intentos.")
        except Exception as ex:
            if intento < MAX_REINTENTOS:
                print(f"  ⚠️   Intento {intento} falló ({ex}), reintentando...")
            else:
                print(f"  ❌  Falló después de {MAX_REINTENTOS} intentos: {ex}")
    os.environ.pop(MODO_LIMPIO_ENV, None)
    return False


async def run_upload():
    titulo("PASO 1 — Upload Precios → Tivendo POS")
    ok = await ejecutar_con_reintentos(
        "Upload Precios",
        lambda: _tiene_upload,
        upload_precios.subir_precios
    )
    if _tiene_upload:
        ruta = getattr(upload_precios, "ULTIMO_EXCEL_USADO", None)
        if ruta:
            try:
                path = Path(ruta)
                if path.exists():
                    path.unlink()
                    print(f"  Archivo eliminado: {path.name}")
            except Exception as e:
                print(f"  No se pudo eliminar el Excel usado: {e}")
    return ok


async def run_upload_manual_con_excel():
    if not _tiene_upload:
        return await run_upload()

    excel_elegido = elegir_excel_precios_manual()
    if not excel_elegido:
        return False

    excel_anterior = getattr(upload_precios, "EXCEL_FORZADO", None)
    upload_precios.EXCEL_FORZADO = excel_elegido
    try:
        return await run_upload()
    finally:
        upload_precios.EXCEL_FORZADO = excel_anterior


async def run_articulos():
    titulo("PASO 2 — Sync Artículos Tivendo POS → Mercadohouse")
    return await ejecutar_con_reintentos(
        "Sync Artículos",
        lambda: _tiene_articulos,
        sync_articulos.sincronizar
    )


async def run_precios():
    titulo("PASO 3 — Sync Precios Tivendo ERP → Mercadohouse")
    return await ejecutar_con_reintentos(
        "Sync Precios",
        lambda: _tiene_precios,
        sync_precios.sincronizar
    )


async def run_packs():
    titulo("PASO 4 — Sync Packs Tivendo POS")
    return await ejecutar_con_reintentos(
        "Sync Packs",
        lambda: _tiene_packs,
        sync_packs.exportar_packs
    )


# ── Ciclo completo ─────────────────────────────────────────
async def run_completo():
    titulo("CICLO COMPLETO  1 ➜ 2 ➜ 4 ➜ 3")
    inicio = datetime.now()
    pasos = [
        ("Upload Precios → Tivendo POS",    run_upload),
        ("Sync Artículos Tivendo POS → MH", run_articulos),
        ("Sync Packs     Tivendo POS → MH", run_packs),
        ("Sync Precios   Tivendo ERP → MH", run_precios),
    ]
    resultados = []
    for nombre, fn in pasos:
        print(f"\n  ▶  Iniciando: {nombre}...")
        ok = await fn()
        resultados.append((nombre, ok))
        if not ok:
            print(f"\n  ⚠️   Falló: {nombre}")
            resp = input("  ¿Continuar con el siguiente paso de todas formas? (s/n): ").strip().lower()
            if resp != "s":
                print("  Proceso abortado.")
                break

    dur = datetime.now() - inicio
    m, s = divmod(int(dur.total_seconds()), 60)
    print()
    print("  " + "═" * 54)
    print("  RESUMEN")
    print("  " + "═" * 54)
    for nombre, ok in resultados:
        icono = "✅" if ok else "❌"
        print(f"  {icono}  {nombre}")
    imprimir_resumen_upload_precios()
    todos_ok = all(ok for _, ok in resultados)
    print(f"\n  Tiempo total: {m}m {s}s")
    if todos_ok:
        print("  🎉  Todo completado sin errores.")
    print("  " + "═" * 54)
    return todos_ok


async def run_completo_manual_con_excel():
    if not _tiene_upload:
        return await run_completo()

    excel_elegido = elegir_excel_precios_manual()
    if not excel_elegido:
        return False

    excel_anterior = getattr(upload_precios, "EXCEL_FORZADO", None)
    upload_precios.EXCEL_FORZADO = excel_elegido
    try:
        return await run_completo()
    finally:
        upload_precios.EXCEL_FORZADO = excel_anterior


# ── Solo pasos 2 y 3 ──────────────────────────────────────
async def run_solo_sync():
    titulo("SYNC MANUAL  2 ➜ 3  (sin upload)")
    inicio = datetime.now()
    pasos = [
        ("Sync Artículos Tivendo POS → MH", run_articulos),
        ("Sync Precios   Tivendo ERP → MH", run_precios),
    ]
    resultados = []
    for nombre, fn in pasos:
        print(f"\n  ▶  Iniciando: {nombre}...")
        ok = await fn()
        resultados.append((nombre, ok))
        if not ok:
            print(f"\n  ⚠️   Falló: {nombre}")
            resp = input("  ¿Continuar con el siguiente paso de todas formas? (s/n): ").strip().lower()
            if resp != "s":
                print("  Proceso abortado.")
                break

    dur = datetime.now() - inicio
    m, s = divmod(int(dur.total_seconds()), 60)
    print()
    print("  " + "═" * 54)
    print("  RESUMEN")
    print("  " + "═" * 54)
    for nombre, ok in resultados:
        icono = "✅" if ok else "❌"
        print(f"  {icono}  {nombre}")
    print(f"\n  Tiempo total: {m}m {s}s")
    if all(ok for _, ok in resultados):
        print("  🎉  Todo completado sin errores.")
    print("  " + "═" * 54)


async def run_packs_y_precios():
    titulo("SYNC PACKS + PRECIOS  4 ➜ 3")
    inicio = datetime.now()
    pasos = [
        ("Sync Packs   Tivendo POS → MH", run_packs),
        ("Sync Precios Tivendo ERP → MH", run_precios),
    ]
    resultados = []
    for nombre, fn in pasos:
        print(f"\n  ▶  Iniciando: {nombre}...")
        ok = await fn()
        resultados.append((nombre, ok))
        if not ok:
            print(f"\n  ⚠️   Falló: {nombre}")
            resp = input("  ¿Continuar con el siguiente paso de todas formas? (s/n): ").strip().lower()
            if resp != "s":
                print("  Proceso abortado.")
                break

    dur = datetime.now() - inicio
    m, s = divmod(int(dur.total_seconds()), 60)
    print()
    print("  " + "═" * 54)
    print("  RESUMEN")
    print("  " + "═" * 54)
    for nombre, ok in resultados:
        icono = "✅" if ok else "❌"
        print(f"  {icono}  {nombre}")
    print(f"\n  Tiempo total: {m}m {s}s")
    if all(ok for _, ok in resultados):
        print("  🎉  Todo completado sin errores.")
    print("  " + "═" * 54)


async def run_sync_todo():
    titulo("SYNC TODO  2 ➜ 4 ➜ 3")
    inicio = datetime.now()
    pasos = [
        ("Sync Artículos Tivendo POS → MH", run_articulos),
        ("Sync Packs     Tivendo POS → MH", run_packs),
        ("Sync Precios   Tivendo ERP → MH", run_precios),
    ]
    resultados = []
    for nombre, fn in pasos:
        print(f"\n  ▶  Iniciando: {nombre}...")
        ok = await fn()
        resultados.append((nombre, ok))
        if not ok:
            print(f"\n  ⚠️   Falló: {nombre}")
            resp = input("  ¿Continuar con el siguiente paso de todas formas? (s/n): ").strip().lower()
            if resp != "s":
                print("  Proceso abortado.")
                break

    dur = datetime.now() - inicio
    m, s = divmod(int(dur.total_seconds()), 60)
    print()
    print("  " + "═" * 54)
    print("  RESUMEN")
    print("  " + "═" * 54)
    for nombre, ok in resultados:
        icono = "✅" if ok else "❌"
        print(f"  {icono}  {nombre}")
    print(f"\n  Tiempo total: {m}m {s}s")
    if all(ok for _, ok in resultados):
        print("  🎉  Todo completado sin errores.")
    print("  " + "═" * 54)


# ── Programar para HOY (tarea única, no repetitiva) ────────
def programar_para_hoy():
    titulo("PROGRAMAR EJECUCIÓN PARA HOY")
    print("  Esto programará el ciclo completo (1→2→4→3) para")
    print("  ejecutarse UNA SOLA VEZ hoy a la hora que elijas.")
    print("  El equipo debe estar encendido a esa hora.")
    print()

    while True:
        hora_str = input("  ¿A qué hora ejecutar hoy? (HH:MM, ej: 22:00  |  ENTER para cancelar): ").strip()
        if hora_str == "":
            print("  Cancelado.")
            return
        if re.match(r"^\d{1,2}:\d{2}$", hora_str):
            h, m = hora_str.split(":")
            if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                ahora = datetime.now()
                hora_elegida = ahora.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
                if hora_elegida <= ahora:
                    print(f"  ⚠️   Las {h}:{m} ya pasaron. Elige una hora futura.")
                    continue
                break
        print("  ⚠️  Formato incorrecto, usa HH:MM")

    hora_fmt = f"{int(h):02d}:{int(m):02d}"

    # ── Selector de archivo Excel ──────────────────────────
    print()
    print("  " + "─" * 54)
    print("  Elige el Excel de precios que se subirá en el paso 1.")
    print("  (Se abrirá el explorador de archivos...)")
    try:
        import tkinter as tk
        from tkinter import filedialog
        _root = tk.Tk()
        _root.withdraw()
        _root.wm_attributes("-topmost", True)
        excel_elegido = filedialog.askopenfilename(
            title="Selecciona el Excel de precios a subir",
            filetypes=[("Excel", "*.xlsx"), ("Todos los archivos", "*.*")],
        )
        _root.destroy()
    except Exception as _e:
        print(f"  ⚠️  No se pudo abrir el selector: {_e}")
        print("  Cancelado.")
        return
    if not excel_elegido:
        print("  Cancelado. No se eligió ningún archivo.")
        return
    print(f"  📄  Archivo elegido: {Path(excel_elegido).name}")
    print("  " + "─" * 54)

    # ── Preguntar apagado AQUÍ, antes de confirmar ─────────
    print()
    print("  " + "─" * 54)
    resp_apagado = input("  ¿Apagar el equipo al terminar (solo si termina sin errores)? (s/n): ").strip().lower()
    apagar = (resp_apagado == "s")
    print("  " + "─" * 54)

    fecha_hoy  = date.today().strftime("%d/%m/%Y")
    # El bat pasa --shutdown si el usuario eligió apagar
    flag_shutdown = " --shutdown" if apagar else ""
    if getattr(sys, "frozen", False):
        tarea_auto = f'"{sys.executable}" --auto{flag_shutdown}'
    else:
        bat_auto = HERE / "ejecutar_automatico.bat"
        comando_auto = f'"{sys.executable}" "{HERE / "menu.py"}" --auto{flag_shutdown}'
        bat_auto.write_text(f'@echo off\ncd /d "{HERE}"\n{comando_auto}\n', encoding="utf-8")
        tarea_auto = f'"{bat_auto}"'

    cmd = [
        "schtasks", "/Create", "/F",
        "/TN", "SyncMercadohouseHoy",
        "/TR", tarea_auto,
        "/SC", "ONCE",
        "/SD", fecha_hoy,
        "/ST", hora_fmt,
        "/RL", "HIGHEST",
    ]

    print(f"\n  Programando para hoy {fecha_hoy} a las {hora_fmt}...")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            runtime_path("excel_programado.txt").write_text(excel_elegido, encoding="utf-8")
            mins_restantes = int((hora_elegida - datetime.now()).total_seconds() // 60)
            print(f"  ✅  Listo. El ciclo correrá hoy a las {hora_fmt}.")
            print(f"  📄  Excel: {Path(excel_elegido).name}")
            if apagar:
                print(f"      El equipo se apagará apenas termine (solo si termina sin errores).")
            print(f"      Faltan aproximadamente {mins_restantes} minutos.")
            print(f"      Puedes cerrar esta ventana, el PC hará el resto.")
            print()
            print("  ℹ️   Para cancelar antes de que corra:")
            print('       Abre CMD como Administrador y escribe:')
            print('       schtasks /Delete /TN "SyncMercadohouseHoy" /F')
            if apagar:
                print('       Para cancelar solo el apagado:  shutdown /a')
        else:
            print(f"  ❌  Error: {r.stderr or r.stdout}")
            print("  → Ejecuta INICIAR.bat como Administrador.")
    except Exception as e:
        print(f"  ❌  No se pudo programar: {e}")


# ── Modo --auto (llamado por la tarea de Windows) ──────────
async def modo_automatico():
    apagar = "--shutdown" in sys.argv

    log_auto = runtime_path("log_automatico.txt")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    linea = "=" * 54
    msg = f"\n{linea}\n[{ts}] EJECUCIÓN AUTOMÁTICA INICIADA\n{linea}"
    print(msg)
    with open(log_auto, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    # Recuperar archivo de precios elegido al programar
    excel_file = runtime_path("excel_programado.txt")
    if excel_file.exists() and _tiene_upload:
        ruta_excel = excel_file.read_text(encoding="utf-8").strip()
        if ruta_excel:
            upload_precios.EXCEL_FORZADO = ruta_excel
            msg_excel = f"[{ts}] Excel programado: {ruta_excel}"
            print(f"  📄  Usando archivo: {Path(ruta_excel).name}")
            with open(log_auto, "a", encoding="utf-8") as f:
                f.write(msg_excel + "\n")
        excel_file.unlink(missing_ok=True)

    todos_ok = await run_completo()

    ts2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg2 = f"[{ts2}] EJECUCIÓN AUTOMÁTICA FINALIZADA"
    print(msg2)
    with open(log_auto, "a", encoding="utf-8") as f:
        f.write(msg2 + "\n")

    # Apagar solo si fue solicitado Y el proceso terminó sin errores
    if apagar:
        if todos_ok:
            print()
            print("  " + "═" * 54)
            print("  💤  Apagando el equipo en 60 segundos...")
            print("  ℹ️   Para cancelar escribe en CMD:  shutdown /a")
            print("  " + "═" * 54)
            with open(log_auto, "a", encoding="utf-8") as f:
                f.write(f"[{ts2}] Apagado programado en 60 segundos\n")
            # /s = apagar  /f = forzar cierre de apps  /t 60 = 1 minuto de gracia
            subprocess.run(["shutdown", "/s", "/f", "/t", "60"])
        else:
            print()
            print("  " + "═" * 54)
            print("  ⚠️   Hubo errores — el equipo NO se apagará.")
            print("  " + "═" * 54)
            with open(log_auto, "a", encoding="utf-8") as f:
                f.write(f"[{ts2}] Apagado cancelado — proceso terminó con errores\n")



# ── Selector de sucursal ───────────────────────────────────
def seleccionar_sucursal():
    sucursales = list(_cfg.SUCURSALES.items())
    salida = str(len(sucursales) + 1)

    while True:
        clear()
        suc_actual = _cfg.sucursal_activa()
        titulo("CAMBIAR SUCURSAL ACTIVA")
        print("  ╔══════════════════════════════════════════════════════╗")
        print("  ║      SUCURSALES DISPONIBLES                         ║")
        print("  ╠══════════════════════════════════════════════════════╣")
        for i, (key, data) in enumerate(sucursales, 1):
            activa = "◀ activa" if key == suc_actual["key"] else "        "
            print(f"  ║  {i}.  {data['nombre']:<40} {activa} ║")
        print("  ║  ────────────────────────────────────────────────  ║")
        print(f"  ║  {salida}.  Volver al menú principal                      ║")
        print("  ╚══════════════════════════════════════════════════════╝")
        print()

        op = input(f"  Elige una opción (1-{salida}): ").strip()

        if op == salida:
            break
        elif op.isdigit() and 1 <= int(op) <= len(sucursales):
            key, data = sucursales[int(op) - 1]
            if key == suc_actual["key"]:
                print(f"  ℹ️   Ya estás en {data['nombre']}.")
                import time; time.sleep(1.2)
                continue
            _cfg.guardar_sucursal(key)
            print()
            print(f"  ✅  Sucursal cambiada a: {data['nombre']}")
            print(f"      Todos los pasos usarán esta sucursal.")
            import time; time.sleep(1.5)
            break
        else:
            print("  ⚠️  Opción no válida.")
            import time; time.sleep(1)


# ── Editor de credenciales ─────────────────────────────────
def _guardar_credenciales(tv_email, tv_pass, mh_email, mh_pass):
    import credenciales as _cred
    _cred.guardar_credenciales(tv_email, tv_pass, mh_email, mh_pass)

    if _tiene_upload:
        upload_precios.EMAIL = tv_email
        upload_precios.PASSWORD = tv_pass
    if _tiene_articulos:
        sync_articulos.TIVENDO_EMAIL = tv_email
        sync_articulos.TIVENDO_PASSWORD = tv_pass
        sync_articulos.MH_EMAIL = mh_email
        sync_articulos.MH_PASSWORD = mh_pass
    if _tiene_precios:
        sync_precios.TIVENDO_EMAIL = tv_email
        sync_precios.TIVENDO_PASSWORD = tv_pass
        sync_precios.MH_EMAIL = mh_email
        sync_precios.MH_PASSWORD = mh_pass
    if _tiene_packs:
        sync_packs.TIVENDO_EMAIL = tv_email
        sync_packs.TIVENDO_PASSWORD = tv_pass
        sync_packs.MH_EMAIL = mh_email
        sync_packs.MH_PASSWORD = mh_pass
def editar_credenciales():
    import importlib

    # Cargar valores actuales
    try:
        import credenciales as _cred
        importlib.reload(_cred)
        vals = {
            "tv_email": _cred.TIVENDO_EMAIL,
            "tv_pass":  _cred.TIVENDO_PASSWORD,
            "mh_email": _cred.MH_EMAIL,
            "mh_pass":  _cred.MH_PASSWORD,
        }
    except Exception as e:
        print(f"  ❌  No se pudo leer credenciales: {e}")
        return

    while True:
        clear()
        titulo("EDITAR CREDENCIALES")
        print("  ╔══════════════════════════════════════════════════════╗")
        print("  ║      CREDENCIALES ACTUALES                          ║")
        print("  ╠══════════════════════════════════════════════════════╣")
        print(f"  ║  1.  Correo Tivendo      {vals['tv_email']:<30}║")
        print(f"  ║  2.  Clave  Tivendo      {'*' * len(vals['tv_pass']):<30}║")
        print("  ║  ────────────────────────────────────────────────  ║")
        print(f"  ║  3.  Correo Mercadohouse {vals['mh_email']:<30}║")
        print(f"  ║  4.  Clave  Mercadohouse {'*' * len(vals['mh_pass']):<30}║")
        print("  ║  ────────────────────────────────────────────────  ║")
        print("  ║  5.  Volver al menú principal                      ║")
        print("  ╚══════════════════════════════════════════════════════╝")
        print()

        op = input("  ¿Qué deseas cambiar? (1-5): ").strip()

        campo_map = {
            "1": ("tv_email",  "Correo Tivendo"),
            "2": ("tv_pass",   "Clave Tivendo"),
            "3": ("mh_email",  "Correo Mercadohouse"),
            "4": ("mh_pass",   "Clave Mercadohouse"),
        }

        if op == "5":
            break
        elif op in campo_map:
            key, label = campo_map[op]
            actual = vals[key]
            print()
            print(f"  Editando: {label}")
            print(f"  Valor actual: {actual}")
            nuevo = input("  Nuevo valor (ENTER para cancelar): ").strip()
            if not nuevo:
                print("  Cancelado.")
                import time; time.sleep(1)
                continue
            if nuevo == actual:
                print("  El valor es igual al actual. Sin cambios.")
                import time; time.sleep(1)
                continue
            # Confirmar
            print(f"  {label}:  {actual}  →  {nuevo}")
            ok = input("  ¿Confirmar? (s/n): ").strip().lower()
            if ok != "s":
                print("  Cancelado.")
                import time; time.sleep(1)
                continue
            vals[key] = nuevo
            try:
                _guardar_credenciales(
                    vals["tv_email"], vals["tv_pass"],
                    vals["mh_email"], vals["mh_pass"]
                )
                print(f"  ✅  {label} actualizado correctamente.")
            except Exception as e:
                print(f"  ❌  Error al guardar: {e}")
            import time; time.sleep(1.5)
        else:
            print("  ⚠️  Opción no válida.")
            import time; time.sleep(1)


# ── Menú principal ─────────────────────────────────────────
async def menu_principal():
    while True:
        clear()
        banner()
        op = input("  Elige una opción (1-13): ").strip()

        if op == "1":
            await run_upload_manual_con_excel()
            esperar()
        elif op == "2":
            await run_articulos()
            esperar()
        elif op == "3":
            await run_precios()
            esperar()
        elif op == "4":
            await run_packs()
            esperar()
        elif op == "5":
            await run_completo_manual_con_excel()
            esperar()
        elif op == "6":
            await run_solo_sync()
            esperar()
        elif op == "7":
            await run_packs_y_precios()
            esperar()
        elif op == "8":
            await run_sync_todo()
            esperar()
        elif op == "9":
            programar_para_hoy()
            esperar()
        elif op == "10":
            seleccionar_sucursal()
            esperar()
        elif op == "11":
            editar_credenciales()
            esperar()
        elif op == "12":
            print("\n  Hasta luego!\n")
            break
        elif op == "13":
            if borrar_sesion_guardada():
                print("\n  Sesion guardada borrada. El proximo intento entrara limpio.\n")
            else:
                print("\n  No habia sesion guardada para borrar.\n")
            esperar()
        else:
            print("  ⚠️  Opción no válida.")
            await asyncio.sleep(1)


if __name__ == "__main__":
    if "--auto" in sys.argv:
        asyncio.run(modo_automatico())
    else:
        asyncio.run(menu_principal())
