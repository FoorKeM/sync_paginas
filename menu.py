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
import signal
import threading
import time
import zipfile
from getpass import getpass
from pathlib import Path
from datetime import datetime, date
from app_paths import APP_DIR, USER_DATA_DIR, configure_console_encoding, configure_playwright_browsers, runtime_path
from utils import MODO_LIMPIO_ENV, ErrorAccesoDefontana, ErrorCredenciales, borrar_sesion_guardada, crear_pagina_trabajo, login_tivendo, login_mercadohouse
from trabajo_lock import bot_exclusivo, trabajo_exclusivo
import telegram_bot

HERE = APP_DIR
TRAY_INFO_FILE = USER_DATA_DIR / "bandeja_info.txt"
TRAY_OPEN_LOG = USER_DATA_DIR / "bandeja_apertura.log"
_menu_bandeja_lock = threading.Lock()
_menu_bandeja_proceso = None


def _entorno_instancia_independiente():
    """Evita que PyInstaller confunda otro MHSync.exe con su proceso auxiliar."""
    entorno = os.environ.copy()
    if getattr(sys, "frozen", False):
        entorno["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return entorno
sys.path.insert(0, str(HERE))
configure_console_encoding()
configure_playwright_browsers()
import config as _cfg
from version import APP_VERSION


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

try:
    import updater
    _tiene_updater = True
except ImportError:
    _tiene_updater = False

try:
    import stock_negativo
    _tiene_stock_negativo = True
except ImportError:
    _tiene_stock_negativo = False


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


MENU_ANCHO = 45  # La opción más larga ocupa 43 caracteres; quedan dos de margen.


def caja_menu(texto=""):
    """Dibuja una fila; los valores largos se muestran completos en varias filas."""
    import textwrap
    sangria = texto[:len(texto) - len(texto.lstrip())]
    contenido = texto[len(sangria):]
    lineas = [sangria + parte for parte in textwrap.wrap(
        contenido, MENU_ANCHO - len(sangria), break_long_words=True,
        break_on_hyphens=False)] if contenido else [sangria]
    for linea in lineas:
        print(f"  ║{linea:<{MENU_ANCHO}}║")


def borde_menu(izq="╠", der="╣"):
    print(f"  {izq}{'═' * MENU_ANCHO}{der}")


def separador_menu():
    caja_menu("  " + "─" * (MENU_ANCHO - 4) + "  ")


def banner():
    suc = _cfg.sucursal_activa()

    def opcion(num, nombre):
        caja_menu(f"  {num}.  {nombre}")

    print(f"  Version: {APP_VERSION}")
    print()
    borde_menu("╔", "╗")
    caja_menu("      SINCRONIZADOR MERCADOHOUSE")
    caja_menu(f"      Sucursal: {suc['nombre']}")
    borde_menu()
    opcion("1", "UPLOAD PRECIOS")
    separador_menu()
    opcion("2", "SYNC ARTÍCULOS")
    opcion("3", "SYNC PRECIOS")
    opcion("4", "SYNC PACKS")
    separador_menu()
    opcion("5", "SYNC ART + PRECIOS")
    opcion("6", "SYNC PACKS + PRECIOS")
    opcion("7", "SYNC TODO")
    separador_menu()
    opcion("8", "COMPLETO MANUAL")
    opcion("9", "PROGRAMAR CAMBIO DE PRECIOS")
    separador_menu()
    opcion("10", "AJUSTAR STOCK NEGATIVO")
    separador_menu()
    opcion("11", "CONFIGURACIÓN")
    separador_menu()
    opcion("12", "OCULTAR EN BANDEJA")
    opcion("13", "SALIR")
    borde_menu("╚", "╝")
    info = tarea_hoy_info()
    if info:
        print(info)
    print()


def titulo(txt, depende_sucursal=None):
    """depende_sucursal=True muestra la sucursal activa (pasos que suben/leen precios
    por lista); depende_sucursal=False aclara que el paso no depende de la sucursal
    (sincroniza el catálogo completo); None no agrega ninguna línea extra."""
    print()
    print("  " + "═" * 54)
    print(f"  ▶  {txt}")
    if depende_sucursal is True:
        suc = _cfg.sucursal_activa()
        print(f"  🏬  Sucursal activa: {suc['nombre']}")
    elif depende_sucursal is False:
        print("  ℹ️   Este paso sincroniza el catálogo completo — no depende de la sucursal activa")
    print("  " + "═" * 54)


def esperar():
    input("\n  Presiona ENTER para volver al menú...")


def formatear_precio_clp(valor) -> str:
    """Presenta un precio en pesos chilenos sin alterar el valor del Excel."""
    return upload_precios.formatear_precio_clp(valor)


def mostrar_resumen_precios_y_confirmar(excel_path: str) -> bool:
    """Muestra los cambios de precio que trae el Excel elegido (igual que el paso 1
    en su log) y pide confirmación antes de seguir. Devuelve False si el usuario
    dice que el archivo no está bien, para que quien llama vuelva a pedirlo."""
    if not _tiene_upload:
        return True
    try:
        total_filas, filas_excel = upload_precios.resumen_excel_precios(Path(excel_path))
    except Exception as e:
        print(f"  ⚠️   No se pudo leer el archivo para mostrar el resumen: {e}")
        return False  # no programar ni subir un archivo cuyo contenido no se pudo revisar

    print()
    print("  " + "-" * 54)
    print(f"  Cambios de precio en el archivo ({total_filas} fila{'s' if total_filas != 1 else ''}):")
    codigos = {str(fila[0]).strip().upper() for fila in filas_excel[1:] if fila and fila[0]}
    detalles = upload_precios.detalles_productos_para_vista_previa(codigos)
    for idx, fila in enumerate(filas_excel[1:], start=1):
        codigo = str(fila[0]).strip() if fila else ""
        articulo = detalles.get(codigo.upper(), {})
        nombre = articulo.get("nombre") or "Nombre no disponible"
        precio_1 = fila[3] if len(fila) > 3 else ""
        precio_2 = fila[6] if len(fila) > 6 else ""
        precios = f"Precio: {formatear_precio_clp(precio_1)}"
        if precio_2:
            precios += f" | Precio 2: {formatear_precio_clp(precio_2)}"
        print(f"    {idx:02d}. {codigo} | {nombre}")
        print(f"        {precios}")
    if len(detalles) < len(codigos):
        faltantes = len(codigos) - len(detalles)
        print(f"  ℹ️  {faltantes} código(s) sin datos en los listados locales de artículos o packs.")

    errores = upload_precios.validar_estructura_precios(filas_excel)
    if errores:
        print()
        print(f"  ❌  El archivo tiene {len(errores)} error(es); la subida FALLARÁ:")
        for err in errores:
            print(f"    - {err}")

    avisos = upload_precios.validar_encabezado_precios(filas_excel)
    if avisos and not errores:
        print()
        print("  ⚠️   Encabezado con inconsistencias (se corrige solo, no bloquea):")
        for aviso in avisos:
            print(f"    - {aviso}")
    print("  " + "-" * 54)

    resp = input("  ¿Están correctos estos cambios de precio? (s/n): ").strip().lower()
    return resp == "s"


def elegir_excel_precios_manual():
    """Abre el selector de archivo para elegir el Excel de precios, muestra los
    cambios de precio que trae y pide confirmación. Si dices que no, vuelve a
    pedir el archivo."""
    while True:
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

        if mostrar_resumen_precios_y_confirmar(excel_elegido):
            return excel_elegido
        print("  Elige el archivo nuevamente...")


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
_fuente_cancelada = False
_resultados_operacion = None
_ultimo_error_fuente = ""
_bot_activo = False


def _partir_mensaje_telegram(lineas, max_caracteres=3900):
    """Conserva todas las filas de una lista larga dentro del límite de Telegram."""
    mensajes = []
    actual = []
    largo = 0
    for linea in lineas:
        while len(linea) > max_caracteres:
            if actual:
                mensajes.append("\n".join(actual))
                actual, largo = [], 0
            mensajes.append(linea[:max_caracteres])
            linea = linea[max_caracteres:]
        if largo + len(linea) + 1 > max_caracteres and actual:
            mensajes.append("\n".join(actual))
            actual, largo = [], 0
        actual.append(linea)
        largo += len(linea) + 1
    if actual:
        mensajes.append("\n".join(actual))
    return mensajes


def _lineas_precios_programados(resumen, solo_rechazados=False):
    """Detalla precios aceptados o rechazados según la respuesta de Tivendo."""
    filas = resumen.get("filas_precios") or []
    fallidos = {str(codigo).strip().upper() for codigo in resumen.get("fallidos") or []}
    codigos = {codigo.lstrip("'").strip().upper() for codigo, _, _ in filas if codigo}
    detalles = upload_precios.detalles_productos_para_vista_previa(codigos)
    if solo_rechazados:
        lineas = [f"Tivendo aceptó {resumen.get('ok', 0)} de {resumen.get('total', len(filas))} precios "
                  f"y rechazó {len(resumen.get('fallidos') or [])}:"]
    else:
        cantidad = resumen.get("ok", len(filas))
        verbo = "actualizó" if cantidad == 1 else "actualizaron"
        sustantivo = "precio" if cantidad == 1 else "precios"
        lineas = [f"Se {verbo} {cantidad} {sustantivo} en {_cfg.sucursal_activa()['nombre']}:"]
    mostrados = set()
    for codigo, precio_1, precio_2 in filas:
        codigo_limpio = codigo.lstrip("'").strip()
        clave = codigo_limpio.upper()
        if (clave in fallidos) != solo_rechazados:
            continue
        nombre = (detalles.get(clave, {}).get("nombre") or "Nombre no disponible")[:120]
        precios = formatear_precio_clp(precio_1)
        if precio_2:
            precios += f" / {formatear_precio_clp(precio_2)}"
        lineas.append(f"{len(lineas):02d}. {codigo_limpio} | {nombre} | {precios}")
        mostrados.add(clave)
    if solo_rechazados:
        for codigo in resumen.get("fallidos") or []:
            clave = str(codigo).strip().upper()
            if clave not in mostrados:
                lineas.append(f"{len(lineas):02d}. {codigo} | sin detalles en el Excel")
                mostrados.add(clave)
    return lineas


def _lineas_precios_sin_confirmar():
    """Muestra el Excel previsto si la subida falló antes de dar resultados por código."""
    ruta = getattr(upload_precios, "ULTIMO_EXCEL_USADO", None) or getattr(upload_precios, "EXCEL_FORZADO", None)
    if not ruta or not Path(ruta).is_file():
        return []
    try:
        _, filas = upload_precios.resumen_excel_precios(Path(ruta))
        codigos = {str(fila[0]).strip().lstrip("'").upper() for fila in filas[1:] if fila and fila[0]}
        detalles = upload_precios.detalles_productos_para_vista_previa(codigos)
        lineas = ["No se pudo confirmar el cambio de estos precios; comprueba Tivendo antes de reintentar:"]
        for indice, fila in enumerate(filas[1:], 1):
            codigo = str(fila[0]).strip().lstrip("'") if fila else ""
            nombre = (detalles.get(codigo.upper(), {}).get("nombre") or "Nombre no disponible")[:120]
            precio = formatear_precio_clp(fila[3] if len(fila) > 3 else "")
            if len(fila) > 6 and fila[6]:
                precio += f" / {formatear_precio_clp(fila[6])}"
            lineas.append(f"{indice:02d}. {codigo} | {nombre} | {precio}")
        return lineas
    except (OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile):
        return []


async def ejecutar_operacion(nombre, fn, origen="Menú"):
    """Registra y comunica un resultado final de una operación, una sola vez."""
    global _resultados_operacion, _fuente_cancelada
    with trabajo_exclusivo() as libre:
        if not libre:
            texto = f"⚠️ {nombre}: ya hay otra sincronización en este PC. No se inició."
            print(f"  {texto}")
            try:
                await telegram_bot.enviar(texto)
            except Exception:
                pass
            return False
        anterior = _resultados_operacion
        _resultados_operacion = []
        _fuente_cancelada = False
        telegram_bot.guardar_estado("En curso", tarea=nombre, origen=origen)
        try:
            _recargar_credenciales_para_ejecucion()
            resultado = await fn()
            pasos = list(_resultados_operacion)
            ok = bool(resultado) if resultado is not None else bool(pasos) and all(p["ok"] for p in pasos)
            if _fuente_cancelada:
                ok = False
            es_ajuste_stock = nombre == "Ajustar Stock Negativo" and _tiene_stock_negativo
            if es_ajuste_stock and not ok and stock_negativo.ULTIMO_GUARDADO_INCIERTO:
                estado = "Sin confirmar"
                lineas = ["⚠️ MHSync — Ajustar Stock Negativo: Tivendo no confirmó el guardado a tiempo. "
                          "Revisa el movimiento en Tivendo antes de repetir el ajuste."]
            elif es_ajuste_stock and not ok and stock_negativo.ULTIMO_AJUSTE_CANCELADO:
                estado = "Cancelado"
                lineas = ["Ajustar Stock Negativo: cancelado. No se modificó Tivendo."]
            elif es_ajuste_stock and ok:
                estado = "Completado"
                lineas = ["✅ MHSync — Ajustar Stock Negativo: terminado con éxito."]
            else:
                estado = "Completado" if ok else "Cancelado" if _fuente_cancelada else "Falló"
                lineas = [f"{'✅' if ok else '❌'} MHSync — {nombre}: {estado.lower()}."]
            if origen == "Programado" and ok:
                lineas = ["✅ MHSync — cambio de precios programado completado con éxito."]
            for paso in pasos:
                detalle = f" — {paso['detalle']}" if paso.get("detalle") and not paso["ok"] else ""
                lineas.append(f"{'✅' if paso['ok'] else '❌'} {paso['nombre']}{detalle}")
            if not pasos and not ok and not es_ajuste_stock:
                lineas.append("No se completó la operación; revisa el registro del PC.")
            incluye_upload = origen in ("Programado", "Telegram archivo") or any(
                "Upload Precios" in paso["nombre"] for paso in pasos)
            if incluye_upload and _tiene_upload:
                resumen = getattr(upload_precios, "ULTIMO_RESULTADO_PRECIOS", None) or {}
                if resumen.get("fallidos"):
                    lineas.append("")
                    lineas.extend(_lineas_precios_programados(resumen))
                    lineas.append("")
                    lineas.extend(_lineas_precios_programados(resumen, solo_rechazados=True))
                elif resumen.get("filas_precios"):
                    lineas.append("")
                    if not ok:
                        lineas.append("Tivendo aceptó los precios, pero otra etapa del ciclo falló.")
                    lineas.extend(_lineas_precios_programados(resumen))
                elif not ok:
                    lineas.append("")
                    lineas.extend(_lineas_precios_sin_confirmar())
            mensaje = "\n".join(lineas)
            telegram_bot.guardar_estado(estado, tarea=nombre, detalle=mensaje, origen=origen)
            try:
                for parte in _partir_mensaje_telegram(lineas):
                    await telegram_bot.enviar(parte)
            except Exception as exc:
                print(f"  ⚠️ No se pudo enviar aviso a Telegram: {type(exc).__name__}")
            return ok
        except BaseException as exc:
            telegram_bot.guardar_estado("Falló", tarea=nombre, detalle=type(exc).__name__, origen=origen)
            try:
                await telegram_bot.enviar(f"❌ MHSync — {nombre}: terminó con error ({type(exc).__name__}).")
            except Exception:
                pass
            raise
        finally:
            _resultados_operacion = anterior

async def ejecutar_con_reintentos(nombre, fn_check, fn_run, permitir_correccion=True):
    """Ctrl+C cancela la fuente actual y devuelve el control al menú."""
    global _fuente_cancelada, _ultimo_error_fuente
    _fuente_cancelada = False
    _ultimo_error_fuente = ""
    tarea = asyncio.current_task()
    anterior = signal.getsignal(signal.SIGINT)
    instalado = False
    if not _bot_activo:
        try:
            signal.signal(signal.SIGINT, lambda _s, _f: tarea.cancel())
            instalado = True
        except ValueError:  # Ejecución desde un hilo ajeno al principal.
            pass
    try:
        print(f"  {nombre}: Ctrl+C para cancelar esta fuente.")
        resultado = await _ejecutar_con_reintentos_impl(nombre, fn_check, fn_run, permitir_correccion)
        if _resultados_operacion is not None:
            _resultados_operacion.append({"nombre": nombre, "ok": resultado,
                                          "detalle": _ultimo_error_fuente if not resultado else ""})
        return resultado
    except asyncio.CancelledError:
        _fuente_cancelada = True
        if tarea:
            tarea.uncancel()
        os.environ.pop(MODO_LIMPIO_ENV, None)
        print(f"  ⏹️  {nombre} cancelado. Los pasos ya completados se conservan.")
        if _resultados_operacion is not None:
            _resultados_operacion.append({"nombre": nombre, "ok": False, "detalle": "Cancelado"})
        return False
    finally:
        if instalado:
            signal.signal(signal.SIGINT, anterior)


async def _ejecutar_con_reintentos_impl(nombre, fn_check, fn_run, permitir_correccion=True):
    """Llama a fn_run hasta MAX_REINTENTOS veces si falla."""
    global _ultimo_error_fuente
    if not fn_check():
        _ultimo_error_fuente = "Módulo no disponible"
        print(f"  ❌  Módulo no encontrado para: {nombre}")
        return False

    for intento in range(1, MAX_REINTENTOS + 1):
        os.environ[MODO_LIMPIO_ENV] = "1" if intento > 1 else "0"
        if intento == 1:
            telegram_bot.actualizar_etapa(nombre, "Iniciando")
        if intento > 1:
            telegram_bot.actualizar_etapa(
                nombre, f"Reintentando ({intento}/{MAX_REINTENTOS}); espera de {ESPERA_REINTENTO}s"
            )
            print(f"\n  🔄  Reintentando {nombre}... (intento {intento}/{MAX_REINTENTOS})")
            print("      Borrando sesion guardada para entrar limpio...")
            borrar_sesion_guardada()
            print(f"      Esperando {ESPERA_REINTENTO}s antes de reintentar...")
            await asyncio.sleep(ESPERA_REINTENTO)
        try:
            await fn_run()
            os.environ.pop(MODO_LIMPIO_ENV, None)
            return True
        except asyncio.CancelledError:
            global _fuente_cancelada
            _fuente_cancelada = True
            tarea = asyncio.current_task()
            if tarea:
                tarea.uncancel()
            os.environ.pop(MODO_LIMPIO_ENV, None)
            print(f"  ⏹️  {nombre} cancelado. Los pasos ya completados se conservan.")
            _ultimo_error_fuente = "Cancelado"
            return False
        except ErrorCredenciales as error:
            _ultimo_error_fuente = f"Contraseña rechazada por {error.servicio}"
            os.environ.pop(MODO_LIMPIO_ENV, None)
            if not permitir_correccion:
                print(f"  ❌  {nombre}: {error.servicio} volvió a rechazar la contraseña.")
                return False
            if "--auto" in sys.argv or "--bot" in sys.argv or _bot_activo:
                print(f"  ❌  {nombre}: contraseña incorrecta de {error.servicio}. Abre MHSync para cambiarla.")
                return False
            if await corregir_contrasena(error.servicio):
                borrar_sesion_guardada()
                return await _ejecutar_con_reintentos_impl(nombre, fn_check, fn_run, permitir_correccion=False)
            return False
        except ErrorAccesoDefontana as error:
            _ultimo_error_fuente = str(error)
            os.environ.pop(MODO_LIMPIO_ENV, None)
            print(f"  ❌  {nombre}: {error}")
            return False
        except SystemExit as e:
            _ultimo_error_fuente = f"Error del proceso (código {e.code})"
            codigo = int(str(e))
            if codigo == 0:
                os.environ.pop(MODO_LIMPIO_ENV, None)
                return True
            if _tiene_upload and codigo == upload_precios.CODIGO_SALIDA_FORMATO_INVALIDO:
                print(f"  ❌  {nombre}: el archivo de precios no tiene el formato correcto. No se reintentará.")
                os.environ.pop(MODO_LIMPIO_ENV, None)
                return False
            if intento < MAX_REINTENTOS:
                print(f"  ⚠️  {nombre}: falló. Reintentando… intento {intento + 1} de {MAX_REINTENTOS}.")
            else:
                print(f"  ❌  Falló después de {MAX_REINTENTOS} intentos.")
        except Exception as ex:
            _ultimo_error_fuente = f"Etapa {getattr(ex, 'etapa', 'desconocida')}: {type(ex.__cause__ or ex).__name__}"
            if intento < MAX_REINTENTOS:
                print(f"  ⚠️  {ex}")
                print(f"  🔄  {nombre}: falló. Reintentando… intento {intento + 1} de {MAX_REINTENTOS}.")
            else:
                print(f"  ❌  Falló después de {MAX_REINTENTOS} intentos: {ex}")
    os.environ.pop(MODO_LIMPIO_ENV, None)
    return False


async def run_upload():
    titulo("PASO 1 — Upload Precios → Tivendo POS", depende_sucursal=True)
    ok = await ejecutar_con_reintentos(
        "Upload Precios",
        lambda: _tiene_upload,
        upload_precios.subir_precios
    )
    if ok and _tiene_upload:
        resumen = getattr(upload_precios, "ULTIMO_RESULTADO_PRECIOS", None) or {}
        fallidos = resumen.get("fallidos") or []
        if fallidos:
            ok = False
            detalle = f"{len(fallidos)} código(s) rechazado(s) por Tivendo"
            print(f"  ⚠️  {detalle}. No se informará éxito total.")
            if _resultados_operacion is not None and _resultados_operacion:
                _resultados_operacion[-1]["ok"] = False
                _resultados_operacion[-1]["detalle"] = detalle
    if ok and _tiene_upload:
        ruta = getattr(upload_precios, "ULTIMO_EXCEL_USADO", None)
        if ruta:
            try:
                path = Path(ruta)
                if path.exists():
                    path.unlink()
                    print(f"  Archivo eliminado: {path.name}")
            except Exception as e:
                print(f"  No se pudo eliminar el Excel usado: {e}")
    elif not ok and _tiene_upload:
        print("  ⚠️   Upload Precios falló: se conserva el Excel para reintentar manualmente.")
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
    titulo("PASO 2 — Sync Artículos Tivendo POS → Mercadohouse", depende_sucursal=False)
    return await ejecutar_con_reintentos(
        "Sync Artículos",
        lambda: _tiene_articulos,
        sync_articulos.sincronizar
    )


async def run_precios():
    titulo("PASO 3 — Sync Precios Tivendo ERP → Mercadohouse", depende_sucursal=True)
    esperados = (
        dict(getattr(upload_precios, "ULTIMOS_PRECIOS_CONFIRMADOS", {}))
        if _tiene_upload
        else {}
    )
    sync_precios.PRECIOS_ESPERADOS = esperados
    if esperados:
        print(f"  Verificando propagacion de {len(esperados)} precio(s) antes de subir a Mercadohouse.")
    try:
        return await ejecutar_con_reintentos(
            "Sync Precios",
            lambda: _tiene_precios,
            sync_precios.sincronizar
        )
    finally:
        sync_precios.PRECIOS_ESPERADOS = {}
        if _tiene_upload:
            upload_precios.ULTIMOS_PRECIOS_CONFIRMADOS = {}


async def run_packs():
    titulo("PASO 4 — Sync Packs Tivendo POS", depende_sucursal=False)
    return await ejecutar_con_reintentos(
        "Sync Packs",
        lambda: _tiene_packs,
        sync_packs.exportar_packs
    )


def _confirmar_ajuste_stock_negativo(items, omitidos=None) -> bool:
    omitidos = omitidos or []
    if omitidos:
        print()
        print(f"  ❌  {len(omitidos)} artículo(s) con stock negativo NO se van a ajustar (revisa y anota manual):")
        for omitido in omitidos:
            print(f"    - {omitido['codigo']} | {omitido['descripcion']} | motivo: {omitido['motivo']}")
    print()
    print("  " + "-" * 54)
    print(f"  Artículos con stock negativo a ajustar ({len(items)}):")
    for idx, item in enumerate(items, start=1):
        print(
            f"    {idx:02d}. {item.codigo} | {item.descripcion} | "
            f"stock: {item.stock_informe} | ajuste: +{item.cantidad_propuesta}"
        )
    print("  " + "-" * 54)
    resp = input("  ¿Aplicar este ajuste en Tivendo? (s/n): ").strip().lower()
    return resp == "s"


async def run_stock_negativo(permitir_correccion=True, confirmar_fn=None):
    titulo("PASO 10 — Ajustar Stock Negativo", depende_sucursal=True)
    if not _tiene_stock_negativo:
        print("  ❌  Módulo no encontrado para: Ajustar Stock Negativo")
        return False
    # No se reintenta automáticamente: un ajuste de inventario que quedó en
    # duda (guardado_incierto_no_reintentar) NUNCA debe reintentarse solo,
    # podría duplicar el ajuste. Cualquier falla se revisa a mano.
    try:
        ok = await stock_negativo.ejecutar_ajuste_completo(
            confirmar_fn=confirmar_fn or _confirmar_ajuste_stock_negativo)
    except ErrorCredenciales as error:
        if permitir_correccion and await corregir_contrasena(error.servicio):
            borrar_sesion_guardada()
            return await run_stock_negativo(permitir_correccion=False,
                                           confirmar_fn=confirmar_fn)
        return False
    except Exception as e:
        if stock_negativo.ULTIMO_GUARDADO_INCIERTO:
            print("  ⚠️  Tivendo no confirmó el guardado a tiempo. Comprueba el movimiento antes de repetir.")
        else:
            print(f"  ❌  Ajustar Stock Negativo falló: {e}")
            print("  No se reintenta automáticamente: revisa el diagnóstico antes de volver a intentar.")
        return False
    if ok:
        print("  ✅  Ajustar Stock Negativo completado.")
    return ok


# ── Ciclo completo ─────────────────────────────────────────
async def run_completo(interactivo=True):
    titulo("CICLO COMPLETO  1 ➜ 2 ➜ 4 ➜ 3", depende_sucursal=True)
    inicio = datetime.now()
    pasos = [
        ("Upload Precios → Tivendo POS",    run_upload),
        ("Sync Artículos Tivendo POS → MH", run_articulos),
        ("Sync Packs     Tivendo POS → MH", run_packs),
        ("Sync Precios   Tivendo ERP → MH", run_precios),
    ]
    resultados = []
    for nombre, fn in pasos:
        if fn is run_precios and resultados and not resultados[0][1]:
            print("\n  ⏭️  Sync Precios omitido: Upload Precios no terminó; se evita subir precios anteriores.")
            resultados.append((nombre, False))
            if _resultados_operacion is not None:
                _resultados_operacion.append({"nombre": "Sync Precios", "ok": False,
                                              "detalle": "Omitido: Upload Precios falló"})
            continue
        print(f"\n  ▶  Iniciando: {nombre}...")
        ok = await fn()
        resultados.append((nombre, ok))
        if _fuente_cancelada:
            print("  Actualización cancelada; se conservan los pasos completados.")
            break
        if not ok:
            print(f"\n  ⚠️   Falló: {nombre}")
            print("  Continuando con las fuentes independientes.")

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
    titulo("SYNC MANUAL  2 ➜ 3  (sin upload)", depende_sucursal=True)
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
        if _fuente_cancelada:
            print("  Actualización cancelada; se conservan los pasos completados.")
            break
        if not ok:
            print(f"\n  ⚠️   Falló: {nombre}")
            print("  Continuando con la siguiente fuente independiente.")

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
    return bool(resultados) and all(ok for _, ok in resultados) and not _fuente_cancelada


async def run_packs_y_precios():
    titulo("SYNC PACKS + PRECIOS  4 ➜ 3", depende_sucursal=True)
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
        if _fuente_cancelada:
            print("  Actualización cancelada; se conservan los pasos completados.")
            break
        if not ok:
            print(f"\n  ⚠️   Falló: {nombre}")
            print("  Continuando con la siguiente fuente independiente.")

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
    return bool(resultados) and all(ok for _, ok in resultados) and not _fuente_cancelada


async def run_sync_todo():
    titulo("SYNC TODO  2 ➜ 4 ➜ 3", depende_sucursal=True)
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
        if _fuente_cancelada:
            print("  Actualización cancelada; se conservan los pasos completados.")
            break
        if not ok:
            print(f"\n  ⚠️   Falló: {nombre}")
            print("  Continuando con la siguiente fuente independiente.")

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
    return bool(resultados) and all(ok for _, ok in resultados) and not _fuente_cancelada


# ── Programar para HOY (tarea única, no repetitiva) ────────
def _es_administrador() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True  # si no se puede saber (ej. no-Windows), no bloquear


def _lineas_aviso_programacion(excel_path, fecha, hora, apagar):
    """Describe los precios previstos en el Excel al momento de programar."""
    total, filas = upload_precios.resumen_excel_precios(Path(excel_path))
    codigos = {str(fila[0]).strip().lstrip("'").upper() for fila in filas[1:] if fila and fila[0]}
    detalles = upload_precios.detalles_productos_para_vista_previa(codigos)
    sucursal = _cfg.sucursal_activa()["nombre"]
    lineas = ["📅 MHSync — cambio de precios programado (aún no ejecutado).",
              f"Fecha: {fecha} a las {hora} (hora del PC)",
              f"Sucursal: {sucursal}",
              ("Al terminar: apagar el PC solo si todo finaliza sin errores."
               if apagar else "Al terminar: dejar el PC encendido."),
              f"Excel: {Path(excel_path).name}",
              f"Precios previstos: {total}", "", "Artículos y packs:"]
    for indice, fila in enumerate(filas[1:], 1):
        codigo = str(fila[0]).strip().lstrip("'") if fila else ""
        nombre = (detalles.get(codigo.upper(), {}).get("nombre") or "Nombre no disponible")[:120]
        precio = formatear_precio_clp(fila[3] if len(fila) > 3 else "")
        precio_2 = fila[6] if len(fila) > 6 else ""
        if precio_2:
            precio += f" / {formatear_precio_clp(precio_2)}"
        lineas.append(f"{indice:02d}. {codigo} | {nombre} | {precio}")
    return lineas


def _archivo_programado():
    """Conserva la selección aunque Windows limpie la carpeta temporal."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return USER_DATA_DIR / "excel_programado.txt"


async def programar_para_hoy():
    titulo("PROGRAMAR CAMBIO DE PRECIOS")
    if not _es_administrador():
        print("  ❌  Este paso necesita permisos de administrador.")
        print("      Cierra el programa y vuelve a abrirlo con 'Ejecutar como administrador'")
        print("      (clic derecho sobre el .exe). Sin eso, Windows no permite crear la")
        print("      tarea programada y este paso no puede continuar.")
        return
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
    while True:
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

        if mostrar_resumen_precios_y_confirmar(excel_elegido):
            break
        print("  Elige el archivo nuevamente...")

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
            _archivo_programado().write_text(excel_elegido, encoding="utf-8")
            runtime_path("excel_programado.txt").unlink(missing_ok=True)
            mins_restantes = int((hora_elegida - datetime.now()).total_seconds() // 60)
            print(f"  ✅  Listo. El ciclo correrá hoy a las {hora_fmt}.")
            print(f"  📄  Excel: {Path(excel_elegido).name}")
            if apagar:
                print(f"      El equipo se apagará de inmediato y a la fuerza apenas termine")
                print(f"      (solo si termina sin errores), sin aviso previo.")
            print(f"      Faltan aproximadamente {mins_restantes} minutos.")
            print(f"      Puedes cerrar esta ventana, el PC hará el resto.")
            print()
            print("  ℹ️   Para cancelar antes de que corra:")
            print('       Abre CMD como Administrador y escribe:')
            print('       schtasks /Delete /TN "SyncMercadohouseHoy" /F')
            try:
                lineas = _lineas_aviso_programacion(excel_elegido, fecha_hoy, hora_fmt, apagar)
                enviado = False
                for parte in _partir_mensaje_telegram(lineas):
                    enviado = await telegram_bot.enviar(parte) or enviado
                if enviado:
                    print("  📲  Aviso de programación enviado a Telegram.")
                else:
                    print("  ℹ️   Telegram no está vinculado; la tarea quedó programada igualmente.")
            except Exception as error:
                print(f"  ⚠️  La tarea quedó programada, pero no se pudo enviar el aviso a Telegram: {type(error).__name__}.")
        else:
            print(f"  ❌  Error: {r.stderr or r.stdout}")
            print("  → Ejecuta INICIAR.bat como Administrador.")
    except Exception as e:
        print(f"  ❌  No se pudo programar: {e}")


async def programar_desde_bot(ruta_excel, hora, apagar):
    """Programa el mismo ciclo de la opción 9 con el Excel recibido por Telegram."""
    if not _es_administrador():
        return False, "El bot no está iniciado como administrador. Configura su inicio elevado en el PC y reinícialo."
    if not re.fullmatch(r"\d{2}:\d{2}", hora):
        return False, "La hora debe tener formato HH:MM."
    horas, minutos = map(int, hora.split(":"))
    if horas > 23 or minutos > 59:
        return False, "La hora indicada no existe."
    ahora = datetime.now()
    elegido = ahora.replace(hour=horas, minute=minutos, second=0, microsecond=0)
    if elegido <= ahora:
        return False, "Esa hora ya pasó hoy. Elige una hora futura."
    ruta_excel = Path(ruta_excel)
    if not ruta_excel.is_file():
        return False, "Ya no encuentro el Excel recibido. Envíalo de nuevo."
    fecha = date.today().strftime("%d/%m/%Y")
    flag = " --shutdown" if apagar else ""
    if getattr(sys, "frozen", False):
        tarea_auto = f'"{sys.executable}" --auto{flag}'
    else:
        bat_auto = HERE / "ejecutar_automatico.bat"
        comando = f'"{sys.executable}" "{HERE / "menu.py"}" --auto{flag}'
        bat_auto.write_text(f'@echo off\ncd /d "{HERE}"\n{comando}\n', encoding="utf-8")
        tarea_auto = f'"{bat_auto}"'
    cmd = ["schtasks", "/Create", "/F", "/TN", "SyncMercadohouseHoy",
           "/TR", tarea_auto, "/SC", "ONCE", "/SD", fecha,
           "/ST", hora, "/RL", "HIGHEST"]
    ok, detalle = await asyncio.to_thread(_guardar_tarea_bot, cmd, ruta_excel)
    if not ok:
        return False, detalle
    try:
        for parte in _partir_mensaje_telegram(_lineas_aviso_programacion(ruta_excel, fecha, hora, apagar)):
            await telegram_bot.enviar(parte)
    except Exception:
        return True, "Programado en Windows, pero no se pudo enviar el aviso detallado a Telegram."
    return True, "Programado correctamente."


def _guardar_tarea_bot(cmd, ruta_excel):
    """Serializa la programación con las sincronizaciones en curso."""
    with trabajo_exclusivo() as libre:
        if not libre:
            return False, "Hay una sincronización en curso. Intenta programar cuando termine."
        try:
            resultado = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False, "Windows no pudo crear la tarea programada."
        if resultado.returncode != 0:
            return False, "Windows rechazó la programación. Comprueba que el bot tenga permisos de administrador."
        puntero = _archivo_programado()
        anterior = puntero.read_text(encoding="utf-8").strip() if puntero.exists() else ""
        try:
            puntero.write_text(str(ruta_excel), encoding="utf-8")
            runtime_path("excel_programado.txt").unlink(missing_ok=True)
        except OSError:
            return False, "Windows creó la tarea, pero no pude guardar el Excel. Revisa el PC antes de la hora programada."
        if anterior and anterior != str(ruta_excel):
            viejo = Path(anterior)
            try:
                if viejo.resolve().is_relative_to(telegram_bot.ARCHIVOS_DIR.resolve()):
                    viejo.unlink(missing_ok=True)
            except OSError:
                pass
        return True, "Programado correctamente."


# ── Modo --auto (llamado por la tarea de Windows) ──────────
def solicitar_apagado_windows(log_auto, ts):
    # /s = apagar  /f = forzar cierre de apps sin guardar  /t 0 = inmediato, sin aviso ni espera
    r = subprocess.run(["shutdown", "/s", "/f", "/t", "0"], capture_output=True, text=True)
    with open(log_auto, "a", encoding="utf-8") as f:
        if r.returncode == 0:
            f.write(f"[{ts}] Comando shutdown enviado correctamente\n")
        else:
            detalle = (r.stderr or r.stdout or "").strip()
            f.write(f"[{ts}] ERROR al pedir apagado: codigo {r.returncode} {detalle}\n")
    return r.returncode == 0


async def modo_automatico():
    apagar = "--shutdown" in sys.argv

    import credenciales as cred
    if not all(cred.cargar_credenciales().values()):
        print("  ❌  Faltan credenciales. Abre MHSync manualmente para configurarlas antes de programar tareas.")
        telegram_bot.guardar_estado("Falló", tarea="Ciclo programado", detalle="Faltan credenciales")
        try:
            await telegram_bot.enviar("❌ MHSync — ciclo programado: faltan credenciales en este PC.")
        except Exception:
            pass
        return

    log_auto = runtime_path("log_automatico.txt")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    linea = "=" * 54
    msg = f"\n{linea}\n[{ts}] EJECUCIÓN AUTOMÁTICA INICIADA\n{linea}"
    print(msg)
    with open(log_auto, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    # Recuperar archivo de precios elegido al programar
    excel_file = _archivo_programado()
    if not excel_file.exists():
        excel_file = runtime_path("excel_programado.txt")  # compatibilidad con versiones anteriores
    if _tiene_upload and (not excel_file.exists() or not excel_file.read_text(encoding="utf-8").strip()):
        mensaje = "❌ MHSync — ciclo programado: falta el Excel elegido al programar. No se inició."
        print(f"  {mensaje}")
        telegram_bot.guardar_estado("Falló", tarea="Ciclo programado", detalle=mensaje)
        try:
            await telegram_bot.enviar(mensaje)
        except Exception:
            pass
        return
    if excel_file.exists() and _tiene_upload:
        ruta_excel = excel_file.read_text(encoding="utf-8").strip()
        if ruta_excel:
            upload_precios.EXCEL_FORZADO = ruta_excel
            msg_excel = f"[{ts}] Excel programado: {ruta_excel}"
            print(f"  📄  Usando archivo: {Path(ruta_excel).name}")
            with open(log_auto, "a", encoding="utf-8") as f:
                f.write(msg_excel + "\n")
        excel_file.unlink(missing_ok=True)

    todos_ok = await ejecutar_operacion("Ciclo programado (opción 9)", run_completo, origen="Programado")

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
            print("  💤  Apagando el equipo ahora (forzado)...")
            print("  " + "═" * 54)
            with open(log_auto, "a", encoding="utf-8") as f:
                f.write(f"[{ts2}] Apagado forzado inmediato solicitado\n")
            if not solicitar_apagado_windows(log_auto, ts2):
                print("  âš ï¸   Windows rechazo el comando de apagado. Revisa log_automatico.txt.")
        else:
            print()
            print("  " + "═" * 54)
            print("  ⚠️   Hubo errores — el equipo NO se apagará.")
            print("  " + "═" * 54)
            with open(log_auto, "a", encoding="utf-8") as f:
                f.write(f"[{ts2}] Apagado cancelado — proceso terminó con errores\n")



# ── Auto-actualización (solo .exe, solo modo interactivo) ──
def verificar_actualizacion():
    if not _tiene_updater:
        return
    try:
        info = updater.buscar_release_nuevo(APP_VERSION)
    except updater.ErrorConsultaActualizacion as e:
        print(f"  ℹ️   No se pudo revisar actualizaciones ({e}).\n")
        return
    if not info:
        return

    print()
    print("  " + "═" * 54)
    print(f"  🔔  Nueva versión disponible: {info['version']}  (tienes: {APP_VERSION})")
    if info["notas"]:
        print("  Notas de la versión:")
        for linea in info["notas"].splitlines()[:6]:
            print(f"    {linea}")
    print("  " + "═" * 54)
    resp = input("  ¿Descargar e instalar ahora? (s/n): ").strip().lower()
    if resp != "s":
        print("  Continuando con la versión actual.\n")
        return

    otras = updater.otras_versiones_abiertas()
    if otras:
        print(f"  ⚠️  Hay otras versiones abiertas: {', '.join(otras)}")
        cerrar = input("  ¿Cerrarlas a la fuerza antes de actualizar? (s/n): ").strip().lower()
        if cerrar != "s":
            print("  Actualización cancelada. Cierra esas versiones e intenta de nuevo.\n")
            return
        for nombre in otras:
            if not updater.cerrar_ejecutable(nombre):
                print(f"  ❌  No se pudo cerrar {nombre}. Actualización cancelada.\n")
                return
        print("  ✓ Versiones anteriores cerradas.")

    try:
        destino = runtime_path(info["nombre_archivo"])
        ultimo_pct = {"valor": -1}

        def _progreso(descargado, total):
            mb_descargado = descargado / (1024 * 1024)
            if total:
                pct = int(descargado * 100 / total)
                if pct == ultimo_pct["valor"]:
                    return
                ultimo_pct["valor"] = pct
                mb_total = total / (1024 * 1024)
                print(f"\r  Descargando actualización... {pct:3d}%  ({mb_descargado:.1f} / {mb_total:.1f} MB)", end="", flush=True)
            else:
                print(f"\r  Descargando actualización... {mb_descargado:.1f} MB", end="", flush=True)

        print("  Descargando actualización...")
        updater.descargar_actualizacion(info["url_descarga"], destino, progreso_fn=_progreso)
        print()
        if not _detener_bot_para_actualizar():
            print("  Actualización aplazada. Cierra la tarea activa y vuelve a intentar.")
            return
        print("  ✅  Descarga completa. Reiniciando con la nueva versión...")
        updater.aplicar_actualizacion_y_reiniciar(destino, info["nombre_archivo"])
    except Exception as e:
        print(f"  ❌  No se pudo actualizar: {e}")
        print("  Continuando con la versión actual.\n")


def _detener_bot_para_actualizar():
    """Libera el exe antes del reemplazo sin cortar una sincronización activa."""
    with trabajo_exclusivo() as libre:
        if not libre:
            return False
    control = telegram_bot.cargar_control()
    if control.get("run_id"):
        telegram_bot.solicitar_detencion(control["run_id"])
        for _ in range(60):
            with bot_exclusivo() as libre:
                if libre:
                    break
            time.sleep(0.5)
        else:
            return False
    return True


# ── Selector de sucursal ───────────────────────────────────
def seleccionar_sucursal():
    sucursales = list(_cfg.SUCURSALES.items())
    salida = str(len(sucursales) + 1)

    while True:
        clear()
        suc_actual = _cfg.sucursal_activa()
        borde_menu("╔", "╗")
        caja_menu("      SUCURSALES DISPONIBLES")
        borde_menu()
        for i, (key, data) in enumerate(sucursales, 1):
            activa = "  ◀ activa" if key == suc_actual["key"] else ""
            caja_menu(f"  {i}.  {data['nombre']}{activa}")
        separador_menu()
        caja_menu(f"  {salida}.  Volver a Configuración")
        borde_menu("╚", "╝")
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

    _aplicar_credenciales(tv_email, tv_pass, mh_email, mh_pass)


def _recargar_credenciales_para_ejecucion():
    """Toma los accesos del disco por si el bot los cambió desde otro proceso."""
    import credenciales as _cred
    datos = _cred.cargar_credenciales()
    _aplicar_credenciales(datos["TIVENDO_EMAIL"], datos["TIVENDO_PASSWORD"],
                          datos["MH_EMAIL"], datos["MH_PASSWORD"])


def _aplicar_credenciales(tv_email, tv_pass, mh_email, mh_pass):
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
    if _tiene_stock_negativo:
        stock_negativo.EMAIL = tv_email
        stock_negativo.PASSWORD = tv_pass


def leer_contrasena(prompt):
    """Lee una contraseña oculta; F2 o TAB permiten verla al editarla."""
    if os.name != "nt" or not sys.stdin.isatty():
        return getpass(prompt)

    import msvcrt
    print(prompt, end="", flush=True)
    caracteres = []
    visible = False
    while True:
        tecla = msvcrt.getwch()
        if tecla in ("\r", "\n"):
            print()
            return "".join(caracteres)
        if tecla == "\x03":
            print()
            raise KeyboardInterrupt
        if tecla == "\x1a":
            print()
            raise EOFError
        if tecla in ("\x00", "\xe0"):
            tecla_especial = msvcrt.getwch()
            alternar = tecla_especial == "<"  # F2 en la consola de Windows
        else:
            alternar = tecla == "\t"
        if alternar:
            visible = not visible
            if caracteres:
                mostrado = "".join(caracteres) if visible else "*" * len(caracteres)
                print("\b" * len(caracteres) + mostrado, end="", flush=True)
        elif tecla == "\b":
            if caracteres:
                caracteres.pop()
                print("\b \b", end="", flush=True)
        elif tecla.isprintable():
            caracteres.append(tecla)
            print(tecla if visible else "*", end="", flush=True)


async def verificar_credencial(servicio, correo, clave):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser, _, page = await crear_pagina_trabajo(p, incognito=True)
        try:
            if servicio == "Tivendo":
                await login_tivendo(page, correo, clave, lambda _: None)
            else:
                await login_mercadohouse(page, correo, clave, lambda _: None)
        finally:
            await browser.close()


async def configurar_credenciales_iniciales():
    import credenciales as cred
    datos = cred.cargar_credenciales()
    if all(datos.values()):
        return True
    print("\n  CONFIGURACIÓN INICIAL DE ACCESOS")
    print("  Se comprobará cada inicio de sesión antes de guardar las credenciales.\n")
    for servicio, titulo_acceso, campo_correo, campo_clave in (
        ("Tivendo", "TIVENDO", "TIVENDO_EMAIL", "TIVENDO_PASSWORD"),
        ("Mercadohouse", "MERCADO HOUSE", "MH_EMAIL", "MH_PASSWORD"),
    ):
        print("  " + "─" * 48)
        print(f"  INGRESA CREDENCIALES {titulo_acceso}")
        print("  " + "─" * 48)
        while True:
            correo = datos[campo_correo] or input(f"  Correo {servicio}: ").strip()
            clave = datos[campo_clave] or input(f"  Contraseña {servicio}: ")
            if not correo or not clave:
                print("  Debes ingresar correo y contraseña.")
                datos[campo_correo] = ""
                datos[campo_clave] = ""
                continue
            print(f"  Comprobando acceso a {servicio}...")
            try:
                await verificar_credencial(servicio, correo, clave)
            except ErrorCredenciales:
                print(f"  ❌  {servicio} rechazó la contraseña. Inténtalo de nuevo.")
            except Exception as error:
                if str(error).startswith(f"Login {servicio} fallido"):
                    print(f"  ❌  No se pudo iniciar sesión en {servicio}. Revisa correo y contraseña.")
                else:
                    print(f"  ❌  No se pudo comprobar {servicio}: {error}")
            else:
                datos[campo_correo], datos[campo_clave] = correo, clave
                print(f"  ✅  Acceso a {servicio} comprobado.\n")
                break
            datos[campo_correo], datos[campo_clave] = "", ""
    _guardar_credenciales(datos["TIVENDO_EMAIL"], datos["TIVENDO_PASSWORD"], datos["MH_EMAIL"], datos["MH_PASSWORD"])
    print("  ✅  Credenciales guardadas en este equipo.\n")
    return True


async def corregir_contrasena(servicio):
    import credenciales as cred
    datos = cred.cargar_credenciales()
    prefijo = "TIVENDO" if servicio == "Tivendo" else "MH"
    print(f"\n  ❌  {servicio} rechazó la contraseña durante la sincronización.")
    clave = leer_contrasena(f"  Nueva contraseña de {servicio} (F2/TAB para ver, ENTER cancela): ")
    if not clave:
        return False
    try:
        await verificar_credencial(servicio, datos[prefijo + "_EMAIL"], clave)
    except ErrorCredenciales:
        print("  ❌  La nueva contraseña también fue rechazada.")
        return False
    except Exception as error:
        print(f"  ❌  No se pudo comprobar la nueva contraseña: {error}")
        return False
    datos[prefijo + "_PASSWORD"] = clave
    _guardar_credenciales(datos["TIVENDO_EMAIL"], datos["TIVENDO_PASSWORD"], datos["MH_EMAIL"], datos["MH_PASSWORD"])
    print("  ✅  Nueva contraseña comprobada y guardada. Reintentando la tarea...\n")
    return True


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

    mostrar_claves = False
    while True:
        clear()
        borde_menu("╔", "╗")
        caja_menu("      CREDENCIALES ACTUALES")
        borde_menu()
        caja_menu("  1.  Correo Tivendo")
        caja_menu(f"      {vals['tv_email']}")
        caja_menu("  2.  Clave Tivendo")
        caja_menu(f"      {'*' * len(vals['tv_pass'])}")
        separador_menu()
        caja_menu("  3.  Correo Mercadohouse")
        caja_menu(f"      {vals['mh_email']}")
        caja_menu("  4.  Clave Mercadohouse")
        caja_menu(f"      {'*' * len(vals['mh_pass'])}")
        separador_menu()
        opcion_claves = f"  5.  {'Ocultar' if mostrar_claves else 'Mostrar'} contraseñas guardadas"
        caja_menu(opcion_claves)
        caja_menu("  6.  Volver a Configuración")
        borde_menu("╚", "╝")
        if mostrar_claves:
            print(f"  Clave Tivendo guardada: {vals['tv_pass']}")
            print(f"  Clave Mercadohouse guardada: {vals['mh_pass']}")
        print()

        op = input("  ¿Qué deseas cambiar? (1-6): ").strip()

        campo_map = {
            "1": ("tv_email",  "Correo Tivendo"),
            "2": ("tv_pass",   "Clave Tivendo"),
            "3": ("mh_email",  "Correo Mercadohouse"),
            "4": ("mh_pass",   "Clave Mercadohouse"),
        }

        if op == "6":
            break
        elif op == "5":
            mostrar_claves = not mostrar_claves
            continue
        elif op in campo_map:
            key, label = campo_map[op]
            actual = vals[key]
            print()
            print(f"  Editando: {label}")
            print(f"  Valor actual: {'********' if key.endswith('pass') and not mostrar_claves else actual}")
            nuevo = (leer_contrasena("  Nueva contraseña (F2/TAB para ver, ENTER cancela): ") if key.endswith("pass") else input("  Nuevo valor (ENTER para cancelar): ")).strip()
            if not nuevo:
                print("  Cancelado.")
                import time; time.sleep(1)
                continue
            if nuevo == actual:
                print("  El valor es igual al actual. Sin cambios.")
                import time; time.sleep(1)
                continue
            # Confirmar
            print(f"  {label}:  {'********' if key.endswith('pass') else actual}  →  {'********' if key.endswith('pass') else nuevo}")
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


# ── Configuración y menú principal ─────────────────────────
async def menu_configuracion():
    while True:
        clear()
        borde_menu("╔", "╗")
        caja_menu("      CONFIGURACIÓN")
        borde_menu()
        caja_menu("  1. Cambiar sucursal activa")
        caja_menu("  2. Editar credenciales (correos y claves)")
        caja_menu("  3. Borrar sesión guardada")
        separador_menu()
        caja_menu("  4. Telegram")
        caja_menu("  5. Volver al menú principal")
        borde_menu("╚", "╝")
        op = input("  Elige una opción (1-5): ").strip()
        if op == "1":
            seleccionar_sucursal()
        elif op == "2":
            editar_credenciales()
        elif op == "3":
            if borrar_sesion_guardada():
                print("\n  Sesión guardada borrada. El próximo intento entrará limpio.\n")
            else:
                print("\n  No había sesión guardada para borrar.\n")
            input("  Presiona ENTER para volver a Configuración...")
        elif op == "4":
            await menu_telegram()
        elif op == "5":
            return
        else:
            print("  Opción no válida.")


async def menu_principal():
    if getattr(sys, "frozen", False):
        if _tiene_updater:
            updater.limpiar_actualizacion_anterior(APP_DIR)
        verificar_actualizacion()
    await configurar_credenciales_iniciales()
    while True:
        clear()
        banner()
        op = input("  Elige una opción (1-13): ").strip()

        if op == "1":
            await ejecutar_operacion("Upload Precios", run_upload_manual_con_excel)
            esperar()
        elif op == "2":
            await ejecutar_operacion("Sync Artículos", run_articulos)
            esperar()
        elif op == "3":
            await ejecutar_operacion("Sync Precios", run_precios)
            esperar()
        elif op == "4":
            await ejecutar_operacion("Sync Packs", run_packs)
            esperar()
        elif op == "5":
            await ejecutar_operacion("Artículos + Precios", run_solo_sync)
            esperar()
        elif op == "6":
            await ejecutar_operacion("Packs + Precios", run_packs_y_precios)
            esperar()
        elif op == "7":
            await ejecutar_operacion("Sync Todo", run_sync_todo)
            esperar()
        elif op == "8":
            await ejecutar_operacion("Ciclo completo", run_completo_manual_con_excel)
            esperar()
        elif op == "9":
            await programar_para_hoy()
            esperar()
        elif op == "10":
            await ejecutar_operacion("Ajustar Stock Negativo", run_stock_negativo)
            esperar()
        elif op == "11":
            await menu_configuracion()
        elif op == "12":
            if iniciar_icono_bandeja():
                print("\n  MHSync quedó en la bandeja. Abre el menú desde su icono.\n")
                break
            esperar()
        elif op == "13":
            print("\n  Hasta luego! El bot seguirá activo en segundo plano.\n")
            break
        else:
            print("  ⚠️  Opción no válida.")
            await asyncio.sleep(1)


def _adquirir_instancia_unica(nombre: str) -> bool:
    """Impide abrir dos instancias del mismo componente en esta sesión."""
    if os.name != "nt":
        return True
    import atexit
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateMutexW(None, False, nombre)
    if not handle:
        if ctypes.get_last_error() == 5:  # instancia elevada: acceso denegado a su mutex
            return False
        raise OSError(ctypes.get_last_error(), "No se pudo comprobar la instancia de MHSync")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    atexit.register(kernel32.CloseHandle, handle)
    return True


def _adquirir_menu_unico() -> bool:
    return _adquirir_instancia_unica("Local\\MercadohouseSyncMenu")


def abrir_menu_desde_bandeja() -> bool:
    """Abre el menú con teclado y consola propios, sin heredar la salida del icono."""
    global _menu_bandeja_proceso
    try:
        if os.name == "nt" and getattr(sys, "frozen", False):
            import _winapi

            with _menu_bandeja_lock:
                if _menu_bandeja_proceso is not None:
                    if _winapi.WaitForSingleObject(_menu_bandeja_proceso, 0) == _winapi.WAIT_TIMEOUT:
                        return True
                    _winapi.CloseHandle(_menu_bandeja_proceso)
                    _menu_bandeja_proceso = None
                if _instancia_activa("Local\\MercadohouseSyncMenu"):
                    return True

                inicio = subprocess.STARTUPINFO()
                # No usar STARTF_USESTDHANDLES: el proceso de bandeja tiene stdin
                # cerrado y stdout dirigido al log, que cerrarían el menú al leer.
                inicio.dwFlags = 0
                proceso, hilo, _, _ = _winapi.CreateProcess(
                    sys.executable, subprocess.list2cmdline([sys.executable, "--tray-open"]),
                    None, None, False, _winapi.CREATE_NEW_CONSOLE,
                    _entorno_instancia_independiente(), str(APP_DIR), inicio,
                )
                _winapi.CloseHandle(hilo)
                _menu_bandeja_proceso = proceso
        else:
            subprocess.Popen([sys.executable, str(HERE / "menu.py")], cwd=str(APP_DIR))
        return True
    except OSError as error:
        print(f"No se pudo abrir MHSync desde la bandeja: {error}", flush=True)
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None, f"No se pudo abrir MHSync:\n{error}", "MHSync", 0x10)
            except (AttributeError, OSError):
                pass
        return False


def _registrar_apertura_bandeja(mensaje):
    try:
        TRAY_OPEN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TRAY_OPEN_LOG.open("a", encoding="utf-8") as registro:
            registro.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {mensaje}\n")
    except OSError:
        pass


def _conectar_consola_del_menu():
    """Reconecta el menú al teclado y pantalla de su propia consola nueva."""
    if os.name != "nt":
        return
    try:
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        _registrar_apertura_bandeja(f"Consola interactiva conectada; PID {os.getpid()}.")
    except OSError as error:
        _registrar_apertura_bandeja(f"No se pudo conectar CONIN$/CONOUT$: {error}.")
        raise


def iniciar_bandeja():
    """Mantiene el bot activo y ofrece abrir el menú desde el área de notificación."""
    if not _adquirir_instancia_unica("Local\\MercadohouseSyncBandeja"):
        return False
    if os.name == "nt" and getattr(sys, "frozen", False):
        import ctypes
        ventana = ctypes.windll.kernel32.GetConsoleWindow()
        if ventana:
            ctypes.windll.user32.ShowWindow(ventana, 0)
    import pystray
    from PIL import Image, ImageDraw

    imagen = Image.new("RGB", (64, 64), "#0a3152")
    dibujo = ImageDraw.Draw(imagen)
    dibujo.rounded_rectangle((5, 5, 58, 58), radius=12, fill="#f5a623")
    dibujo.text((12, 22), "MH", fill="#0a3152")

    def abrir(_icono, _opcion):
        abrir_menu_desde_bandeja()

    def cerrar(_icono, _opcion):
        _icono.stop()  # El bot sigue activo; se controla desde el menú o Telegram.

    icono = pystray.Icon("MHSync", imagen, f"MHSync {APP_VERSION} · bot de Telegram",
                         pystray.Menu(pystray.MenuItem(f"Abrir MHSync ({APP_VERSION.split('-')[0]})", abrir, default=True),
                                      pystray.MenuItem("Cerrar icono (bot activo)", cerrar)))
    TRAY_INFO_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRAY_INFO_FILE.write_text(f"{APP_VERSION}\n{os.getpid()}\n", encoding="utf-8")
    try:
        asyncio.run(activar_bot_en_segundo_plano())
        icono.run()
    finally:
        try:
            if _leer_info_bandeja()[1] == os.getpid():
                TRAY_INFO_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def _leer_info_bandeja():
    try:
        version, pid = TRAY_INFO_FILE.read_text(encoding="utf-8").splitlines()[:2]
        return version, int(pid)
    except (OSError, ValueError, IndexError):
        return "", None


def _instancia_activa(nombre):
    """Comprueba una instancia sin adquirir su bloqueo."""
    if os.name != "nt":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = (ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenMutexW(0x00100000, False, nombre)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # un icono elevado puede negar el acceso


def _icono_bandeja_activo():
    return _instancia_activa("Local\\MercadohouseSyncBandeja")


def iniciar_icono_bandeja() -> bool:
    """Abre la bandeja en otro proceso para que cerrar el menú no detenga el bot."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        print("  La bandeja se usa desde MHSync.exe en Windows.")
        return False
    if _icono_bandeja_activo():
        version, _ = _leer_info_bandeja()
        if version == APP_VERSION:
            return True
        print("  Hay un icono de MHSync anterior. Ciérralo desde la bandeja y vuelve a elegir esta opción.")
        return False
    comando = [sys.executable, "--tray"]
    telegram_bot.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_bandeja = telegram_bot.LOG_FILE.with_name("bandeja.log")
    try:
        with log_bandeja.open("ab") as log:
            proceso = subprocess.Popen(
                comando, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                cwd=str(APP_DIR),
                env=_entorno_instancia_independiente(),
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return True  # El icono aparece cuando termina de cargar el ejecutable.
    except OSError as exc:
        print(f"  No se pudo abrir la bandeja: {exc}")
        return False


async def ejecutar_desde_bot(clave, nombre):
    funciones = {"articulos": run_articulos, "precios": run_precios,
                 "packs": run_packs, "todo": run_sync_todo,
                 "art_precios": run_solo_sync,
                 "packs_precios": run_packs_y_precios}
    return await ejecutar_operacion(nombre, funciones[clave], origen="Telegram")


async def ejecutar_stock_desde_bot(confirmar_fn):
    """Ejecuta stock desde Telegram, sin selector ni archivo Excel."""
    return await ejecutar_operacion(
        "Ajustar Stock Negativo",
        lambda: run_stock_negativo(permitir_correccion=False,
                                  confirmar_fn=confirmar_fn),
        origen="Telegram",
    )


async def ejecutar_excel_desde_bot(modo, ruta_excel):
    """Usa el Excel recibido en Telegram sin abrir el selector de archivos."""
    if not _tiene_upload:
        return False
    anterior = upload_precios.EXCEL_FORZADO
    upload_precios.EXCEL_FORZADO = str(ruta_excel)
    opciones = {
        "upload": ("Upload Precios desde Telegram", run_upload, "Telegram archivo"),
        "completo": ("Ciclo completo desde Telegram", run_completo, "Telegram archivo"),
        "programado": ("Ciclo programado desde Telegram", run_completo, "Programado"),
    }
    try:
        nombre, funcion, origen = opciones[modo]
        return await ejecutar_operacion(nombre, funcion, origen=origen)
    finally:
        upload_precios.EXCEL_FORZADO = anterior


async def actualizar_credenciales_desde_bot(servicio, correo, clave):
    import credenciales as cred
    with trabajo_exclusivo() as libre:
        if not libre:
            return False, "Hay una sincronización en curso. Vuelve a intentarlo al terminar."
        try:
            await verificar_credencial(servicio, correo, clave)
        except ErrorCredenciales:
            return False, f"{servicio} rechazó las credenciales. Las anteriores siguen guardadas."
        except Exception:
            return False, f"No se pudo comprobar el acceso a {servicio}. Las anteriores siguen guardadas."
        datos = cred.cargar_credenciales()
        prefijo = "TIVENDO" if servicio == "Tivendo" else "MH"
        datos[prefijo + "_EMAIL"] = correo
        datos[prefijo + "_PASSWORD"] = clave
        try:
            _guardar_credenciales(datos["TIVENDO_EMAIL"], datos["TIVENDO_PASSWORD"],
                                  datos["MH_EMAIL"], datos["MH_PASSWORD"])
            borrar_sesion_guardada()
        except Exception:
            return False, "Se comprobó el acceso, pero no se pudieron guardar las credenciales."
        return True, f"Acceso a {servicio} comprobado y guardado en este PC."


async def cambiar_sucursal_desde_bot(key):
    if key not in _cfg.SUCURSALES:
        return False, "Sucursal desconocida."
    with trabajo_exclusivo() as libre:
        if not libre:
            return False, "Hay una sincronización en curso. Vuelve a intentarlo al terminar."
        try:
            _cfg.guardar_sucursal(key)
        except OSError:
            return False, "No se pudo guardar la sucursal en este PC."
        return True, f"Sucursal activa: {_cfg.sucursal_activa()['nombre']}. El menú del PC la mostrará al volver a abrirlo."


async def menu_cuentas_telegram():
    while True:
        clear()
        borde_menu("╔", "╗")
        caja_menu("      CUENTAS DE TELEGRAM")
        borde_menu()
        config = telegram_bot.cargar_config()
        if not config or config.get("modo") == "avisos":
            print("  Las cuentas se administran en el PC que recibe las órdenes.")
            input("\n  Presiona ENTER para volver...")
            return
        usuarios = telegram_bot.usuarios_autorizados(config)
        for numero, usuario in enumerate(usuarios, 1):
            nombre = usuario.get("nombre") or "Cuenta sin nombre"
            caja_menu(f"  {numero}. {nombre} (ID {usuario['user_id']})")
        borde_menu()
        caja_menu("  1. Agregar cuenta")
        caja_menu("  2. Eliminar cuenta")
        caja_menu("  3. Volver a Telegram")
        borde_menu("╚", "╝")
        op = input("  Elige una opción (1-3): ").strip()
        if op == "1":
            with bot_exclusivo() as libre:
                if libre:
                    print("  Activa primero el bot en este PC.")
                else:
                    control = telegram_bot.cargar_control()
                    if control.get("version") != APP_VERSION:
                        if not _es_administrador():
                            print("  El bot activo usa una versión anterior. Abre este MHSync.exe como administrador")
                            print("  y vuelve a Agregar cuenta para actualizar el bot sin perder permisos.")
                            input("\n  Presiona ENTER para volver a Cuentas...")
                            continue
                        print("  El bot activo usa una versión anterior. Reiniciándolo con esta versión...")
                        if not await detener_bot() or not await activar_bot_en_segundo_plano():
                            print("  No se pudo actualizar el bot. Revisa su estado en el menú Telegram.")
                            input("\n  Presiona ENTER para volver a Cuentas...")
                            continue
                    codigo = telegram_bot.crear_invitacion()
                    nombre_bot = config.get("bot_username", "")
                    if not telegram_bot.mostrar_qr_invitacion(nombre_bot, codigo):
                        print(f"  En el nuevo celular, abre @{nombre_bot or 'tu_bot'}.")
                    print(f"  También puedes enviar /vincular {codigo} antes de 10 minutos.")
        elif op == "2":
            if len(usuarios) == 1:
                print("  No se puede eliminar la última cuenta autorizada.")
            else:
                seleccion = input("  Número de la cuenta a eliminar: ").strip()
                if seleccion.isdigit() and 1 <= int(seleccion) <= len(usuarios):
                    usuario = usuarios[int(seleccion) - 1]
                    nombre = usuario.get("nombre") or str(usuario["user_id"])
                    if input(f"  ¿Eliminar a {nombre}? (s/n): ").strip().lower() == "s":
                        if telegram_bot.eliminar_cuenta(usuario["user_id"]):
                            print("  Cuenta eliminada. Ya no puede enviar órdenes ni recibe avisos.")
                        else:
                            print("  No se pudo eliminar; revisa la lista actual.")
                else:
                    print("  Número no válido.")
        elif op == "3":
            return
        else:
            print("  Opción no válida.")
        input("\n  Presiona ENTER para volver a Cuentas...")


async def menu_telegram():
    while True:
        clear()
        borde_menu("╔", "╗")
        caja_menu("      TELEGRAM")
        with bot_exclusivo() as libre:
            caja_menu(f"      Estado del bot: {'Detenido' if libre else 'Activo'}")
        borde_menu()
        caja_menu("  CUENTAS")
        caja_menu("  1. Administrar cuentas")
        separador_menu()
        caja_menu("  BOT EN ESTE PC")
        caja_menu("  2. Activar bot")
        caja_menu("  3. Detener bot")
        caja_menu("  4. Iniciar con Windows (admin)")
        separador_menu()
        caja_menu("  OTROS PC")
        caja_menu("  5. Mostrar ID de los chats")
        caja_menu("  6. Conectar este PC solo para avisos")
        separador_menu()
        caja_menu("  CONFIGURACIÓN INICIAL")
        caja_menu("  7. Configurar un bot nuevo")
        separador_menu()
        caja_menu("  8. Volver a Configuración")
        borde_menu("╚", "╝")
        op = input("  Elige una opción (1-8): ").strip()
        if op == "1":
            await menu_cuentas_telegram()
        elif op == "2":
            await activar_bot_en_segundo_plano()
        elif op == "3":
            await detener_bot()
        elif op == "4":
            configurar_inicio_bot()
        elif op == "5":
            config = telegram_bot.cargar_config()
            if config:
                print("  ID de los chats vinculados: " + ", ".join(
                    str(chat_id) for chat_id in telegram_bot.destinatarios(config)))
                print("  Usa estos ID y el token del bot en los otros PC.")
            else:
                print("  Vincula primero tu Telegram en el PC principal.")
        elif op == "6":
            with bot_exclusivo() as libre:
                if libre:
                    await telegram_bot.configurar_solo_avisos(leer_contrasena, input)
                else:
                    print("  Detén primero el bot activo en este PC.")
        elif op == "7":
            await telegram_bot.configurar(leer_contrasena)
        elif op == "8":
            return
        else:
            print("  Opción no válida.")
        input("\n  Presiona ENTER para volver al menú Telegram...")


async def servir_bot():
    global _bot_activo
    config = telegram_bot.cargar_config()
    if not config:
        print("  Primero vincula tu chat desde el menú Telegram.")
        return
    if config.get("modo") == "avisos":
        print("  Este PC solo envía avisos; las órdenes se reciben en el PC principal.")
        return
    import credenciales as cred
    if not all(cred.cargar_credenciales().values()):
        print("  Primero configura las credenciales de Tivendo y Mercadohouse.")
        return
    with bot_exclusivo() as libre:
        if not libre:
            print("  El bot ya está activo en otra ventana de este PC.")
            return
        run_id = telegram_bot.registrar_proceso(APP_VERSION)
        bot = telegram_bot.Bot(config, ejecutar_desde_bot,
                               debe_detener=lambda: telegram_bot.detencion_solicitada(run_id),
                               actualizar_credenciales=actualizar_credenciales_desde_bot,
                               cambiar_sucursal=cambiar_sucursal_desde_bot,
                               ejecutar_excel=ejecutar_excel_desde_bot,
                               programar_excel=programar_desde_bot,
                               ejecutar_stock=ejecutar_stock_desde_bot)
        anterior = signal.getsignal(signal.SIGINT)
        instalado = False
        try:
            signal.signal(signal.SIGINT, lambda _s, _f: bot.parar())
            instalado = True
        except ValueError:
            pass
        _bot_activo = True
        try:
            await bot.servir()
        except asyncio.CancelledError:
            bot.parar()
            tarea = asyncio.current_task()
            if tarea:
                tarea.uncancel()
            print("  Bot de Telegram detenido.")
        finally:
            _bot_activo = False
            if instalado:
                signal.signal(signal.SIGINT, anterior)
            telegram_bot.limpiar_control(run_id)


async def activar_bot_en_segundo_plano():
    config = telegram_bot.cargar_config()
    if not config:
        print("  Primero vincula tu chat desde el menú Telegram.")
        return False
    if config.get("modo") == "avisos":
        print("  Este PC solo envía avisos; no necesita activar el bot.")
        return False
    import credenciales as cred
    if not all(cred.cargar_credenciales().values()):
        print("  Primero configura las credenciales de Tivendo y Mercadohouse.")
        return False
    with bot_exclusivo() as libre:
        if not libre:
            print("  El bot ya está activo. Puedes volver al menú y seguir usando MHSync.")
            return True
    comando = ([sys.executable, "--bot"] if getattr(sys, "frozen", False)
               else [sys.executable, str(HERE / "menu.py"), "--bot"])
    telegram_bot.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    opciones = {"stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT,
                "cwd": str(HERE), "env": _entorno_instancia_independiente()}
    if os.name == "nt":
        opciones["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        opciones["start_new_session"] = True
    try:
        with telegram_bot.LOG_FILE.open("ab") as log:
            proceso = subprocess.Popen(comando, stdout=log, **opciones)
    except OSError as exc:
        print(f"  No se pudo iniciar el bot: {exc}")
        return False
    for _ in range(40):
        if proceso.poll() is not None:
            print(f"  El bot se cerró al iniciar. Revisa: {telegram_bot.LOG_FILE}")
            return False
        if telegram_bot.cargar_control().get("pid") == proceso.pid:
            print("  ✅ Bot activo en segundo plano. Puedes seguir usando el menú.")
            return True
        await asyncio.sleep(0.5)
    print(f"  El bot tarda en iniciar. Comprueba /menu en Telegram o revisa: {telegram_bot.LOG_FILE}")
    return False


async def detener_bot():
    with bot_exclusivo() as libre:
        if libre:
            print("  El bot ya está detenido.")
            return True
    control = telegram_bot.cargar_control()
    if not control.get("run_id"):
        print("  El bot está iniciando. Vuelve a intentar en unos segundos.")
        return False
    telegram_bot.solicitar_detencion(control["run_id"])
    for _ in range(24):
        with bot_exclusivo() as libre:
            if libre:
                print("  ✅ Bot detenido. El menú sigue disponible.")
                return True
        await asyncio.sleep(0.5)
    print("  El bot dejará de recibir órdenes y se cerrará cuando termine la tarea en curso.")
    return False


def configurar_inicio_bot():
    """Inicia el bot y su icono de bandeja elevados al entrar a Windows."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        print("  Esta opción se configura desde MHSync.exe en Windows.")
        return
    if not _es_administrador():
        print("  Abre MHSync como administrador una vez para configurar el bot elevado.")
        return
    nombre_tarea = "MHSyncBotAdmin"
    existe = False
    try:
        existente = subprocess.run(["schtasks", "/Query", "/TN", nombre_tarea],
                                   capture_output=True, text=True, timeout=15)
        existe = existente.returncode == 0
        if existe:
            respuesta = input("  Inicio automático activo. ¿Actualizar con bandeja (a), desactivar (d) o cancelar (ENTER)? ").strip().lower()
            if respuesta not in ("a", "d"):
                return
            if respuesta == "d":
                borrado = subprocess.run(["schtasks", "/Delete", "/TN", nombre_tarea, "/F"],
                                         capture_output=True, text=True, timeout=20)
                if borrado.returncode == 0:
                    print("  Inicio automático desactivado. El bot activo ahora seguirá hasta detenerlo.")
                else:
                    print("  Windows no permitió desactivar el inicio automático.")
                return
    except (OSError, subprocess.TimeoutExpired):
        print("  No se pudo comprobar el inicio automático en Windows.")
        return
    config = telegram_bot.cargar_config()
    if not config or config.get("modo") == "avisos":
        print("  Primero vincula el bot que recibirá las órdenes en este PC.")
        return
    if not existe and input("  ¿Iniciar bot y bandeja como administrador al entrar a Windows? (s/n): ").strip().lower() != "s":
        return
    ruta_exe = str(Path(sys.executable).resolve())
    comando = ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/IT",
               "/RL", "HIGHEST", "/TN", nombre_tarea,
               "/TR", f'"{ruta_exe}" --tray']
    try:
        creado = subprocess.run(comando, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        print("  El Programador de tareas no respondió en 45 segundos. No se activó el inicio automático.")
        return
    except OSError as error:
        print(f"  Windows no pudo abrir schtasks.exe: {error}.")
        return
    if creado.returncode != 0:
        detalle = (creado.stderr or creado.stdout or "").strip().splitlines()
        print("  Windows rechazó el inicio elevado: " + (detalle[0][:250] if detalle else f"código {creado.returncode}"))
        return
    (USER_DATA_DIR / "MHSyncBotAdmin.vbs").unlink(missing_ok=True)
    inicio = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
              / "Programs" / "Startup" / "MHSyncTelegramBot.vbs")
    inicio.unlink(missing_ok=True)
    try:
        subprocess.run(["schtasks", "/Delete", "/TN", "MHSyncBandeja", "/F"],
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass
    print("  Listo. El bot y su icono de bandeja se iniciarán como administrador al entrar a Windows.")
    print("  Para probar ahora: detén el bot y actívalo desde esta ventana administradora.")


if __name__ == "__main__":
    if "--auto" in sys.argv:
        asyncio.run(modo_automatico())
    elif "--bot" in sys.argv:
        asyncio.run(servir_bot())
    elif "--tray" in sys.argv:
        if not iniciar_bandeja():
            sys.exit(0)  # Ya hay otro icono; el menú comprobará su versión.
    else:
        desde_bandeja = "--tray-open" in sys.argv
        if desde_bandeja:
            _registrar_apertura_bandeja(f"Menú iniciado desde bandeja; versión {APP_VERSION}; PID {os.getpid()}.")
            _conectar_consola_del_menu()
        if getattr(sys, "frozen", False) and _tiene_updater:
            if Path(sys.executable).name.casefold() == updater.NOMBRE_INSTALADOR_TRANSITORIO.casefold():
                try:
                    updater.finalizar_instalacion_transitoria()
                except Exception as exc:
                    print(f"  ❌  No se pudo completar la actualización: {exc}")
                    try:
                        input("  Presiona ENTER para cerrar esta ventana...")
                    except EOFError:
                        pass
                    sys.exit(1)
            elif "--limpiar-instalador" in sys.argv:
                indice = sys.argv.index("--limpiar-instalador")
                if indice + 1 < len(sys.argv):
                    if not updater.limpiar_instalador_transitorio(APP_DIR, Path(sys.argv[indice + 1])):
                        print("  ⚠️  No se pudo retirar el instalador temporal; puedes borrarlo después.")
        if not _adquirir_menu_unico():
            if desde_bandeja:
                _registrar_apertura_bandeja("Otra ventana del menú conservaba la instancia única.")
            # El acceso directo o un doble clic no deben dejar otra consola abierta.
            sys.exit(0)
        try:
            asyncio.run(menu_principal())
        except Exception as error:
            if desde_bandeja:
                _registrar_apertura_bandeja(f"El menú terminó con {type(error).__name__}: {error}.")
            raise
