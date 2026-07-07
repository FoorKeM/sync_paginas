"""
SINCRONIZADOR DE PACKS: TIVENDO PUNTO DE VENTAS -> MERCADOHOUSE

Flujo:
  1. Login Tivendo.
  2. Verifica empresa MERCADO HOUSE SPA.
  3. Entra a Punto de Ventas.
  4. Articulos -> Packs.
  5. Exportar y guardar el archivo descargado.
  6. Login Mercadohouse.
  7. Configuracion -> Importadores -> Lista de Packs.
  8. Selecciona archivo -> Cargar Lista de Packs.
"""

import asyncio
import sys
from pathlib import Path
from app_paths import runtime_path, configure_playwright_browsers, DESCARGA_DIR
configure_playwright_browsers()
from playwright.async_api import async_playwright
from utils import (
    asegurar_empresa_mercadohouse,
    click_y_capturar_pagina,
    crear_logger,
    crear_pagina_trabajo,
    esperar_carga_ligera,
    esperar_confirmacion_carga_mercadohouse,
    guardar_diagnostico,
    ir_a_importadores_mercadohouse,
    limpiar_carpeta_archivos,
    login_mercadohouse,
    login_tivendo,
    MedidorEtapas,
    pausa_corta,
    reintentar_accion,
    rotar_log,
)
from credenciales import TIVENDO_EMAIL, TIVENDO_PASSWORD, MH_EMAIL, MH_PASSWORD


CARPETA_DESCARGA = str(DESCARGA_DIR)
MOSTRAR_NAVEGADOR = True

LOG_FILE = runtime_path("log_packs.txt")
log = crear_logger(LOG_FILE)

BOTONES_CARGA_PACKS = (
    "CARGAR LISTA DE PACKS",
    "CARGAR LISTA DE ARTÍCULOS",
    "CARGAR LISTA DE ARTICULOS",
)


async def subir_packs_mercadohouse(page, ruta_archivo: str, nombre_archivo: str, log_fn) -> None:
    """Adjunta el archivo una vez y presiona el primer boton de carga disponible."""
    await reintentar_accion(
        "esperar selector de archivo packs",
        lambda: page.locator('input[type="file"]').wait_for(state="attached", timeout=15000),
        log_fn,
    )

    log_fn(f"Subiendo archivo: {nombre_archivo}")
    await reintentar_accion(
        "adjuntar archivo packs",
        lambda: page.locator('input[type="file"]').set_input_files(ruta_archivo),
        log_fn,
    )
    await pausa_corta(0.3)
    log_fn("Archivo cargado")

    ultimo_error = Exception("No se encontro boton para cargar lista de packs")
    for boton in BOTONES_CARGA_PACKS:
        locator = page.get_by_role("button", name=boton)
        try:
            await locator.wait_for(state="visible", timeout=1500)
            log_fn(f"Haciendo clic en '{boton}'...")
            await reintentar_accion(
                f"click {boton}",
                lambda: locator.click(),
                log_fn,
            )
            await esperar_carga_ligera(page)
            await pausa_corta(1)
            return
        except Exception as e:
            ultimo_error = e
            log_fn(f"Boton no disponible '{boton}': {e}")
    raise ultimo_error


async def exportar_packs():
    rotar_log(LOG_FILE)
    medidor = MedidorEtapas(log)

    log("=" * 55)
    log("INICIO SYNC PACKS TIVENDO -> MERCADOHOUSE")
    log("=" * 55)

    Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser, context, page = await crear_pagina_trabajo(
            p,
            mostrar_navegador=MOSTRAR_NAVEGADOR,
            carpeta_descarga=CARPETA_DESCARGA,
            incognito=True,
        )

        ruta_archivo = None

        try:
            with medidor.etapa("Login Tivendo"):
                await login_tivendo(page, TIVENDO_EMAIL, TIVENDO_PASSWORD, log)

            with medidor.etapa("Verificar empresa"):
                await asegurar_empresa_mercadohouse(page, log, TIVENDO_EMAIL, TIVENDO_PASSWORD)

            with medidor.etapa("Abrir Punto de Ventas"):
                log("Haciendo clic en Punto de Ventas...")
                pos_page, nueva_pestana = await click_y_capturar_pagina(
                    context,
                    page,
                    page.get_by_text("Punto de Ventas"),
                )

            if nueva_pestana:
                log("Punto de Ventas abrio en pestana nueva")
            else:
                log("Punto de Ventas navego en la misma pestana")

            await pos_page.locator("text=Artículos").first.wait_for(state="visible", timeout=25000)
            log(f"En Punto de Ventas: {pos_page.url}")

            with medidor.etapa("Abrir Packs"):
                log("Navegando a Articulos > Packs...")
                await pos_page.locator("text=Artículos").first.click()
                await pos_page.get_by_role("link", name="Packs", exact=True).wait_for(
                    state="visible",
                    timeout=10000,
                )
                await pos_page.get_by_role("link", name="Packs", exact=True).click()
                await pos_page.get_by_role("button", name="Exportar").wait_for(
                    state="visible",
                    timeout=25000,
                )
                log(f"En Packs: {pos_page.url}")

            with medidor.etapa("Exportar packs Tivendo"):
                log("Haciendo clic en Exportar Packs...")
                async with pos_page.expect_download(timeout=300000) as download_info:
                    await pos_page.get_by_role("button", name="Exportar").click()

                download = await download_info.value
                nombre_archivo = download.suggested_filename or "packs.xlsx"
                ruta_archivo = str(Path(CARPETA_DESCARGA) / nombre_archivo)
                await download.save_as(ruta_archivo)

            log(f"Archivo de packs descargado: {nombre_archivo}")
            log(f"Guardado en: {ruta_archivo}")

            with medidor.etapa("Login Mercadohouse"):
                await login_mercadohouse(page, MH_EMAIL, MH_PASSWORD, log)

            await ir_a_importadores_mercadohouse(page, "LISTA DE PACKS", log)
            log("Pestana Lista de Packs abierta")

            with medidor.etapa("Subir packs Mercadohouse"):
                await subir_packs_mercadohouse(page, ruta_archivo, nombre_archivo, log)
                await esperar_confirmacion_carga_mercadohouse(page, log)
            log("Lista de packs cargada")

            try:
                n = limpiar_carpeta_archivos(CARPETA_DESCARGA)
                log(f"Carpeta limpiada: {n} archivo(s) eliminado(s)")
            except Exception as e:
                log(f"No se pudo limpiar la carpeta: {e}")

            log("=" * 55)
            log("SYNC PACKS COMPLETADO")
            log("=" * 55)

        except Exception as e:
            log(f"ERROR: {e}")
            try:
                captura = pos_page if "pos_page" in dir() else page
                await guardar_diagnostico(captura, e, log, "sync_packs", medidor)
            except Exception:
                pass
            medidor.resumen()
            try:
                limpiar_carpeta_archivos(CARPETA_DESCARGA)
            except Exception:
                pass
            await browser.close()
            sys.exit(1)

        await browser.close()
        medidor.resumen()
        log("")


if __name__ == "__main__":
    asyncio.run(exportar_packs())
