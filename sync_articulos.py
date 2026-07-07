"""
SINCRONIZADOR DE ARTÍCULOS: TIVENDO PUNTO DE VENTAS → MERCADOHOUSE
Flujo:
  1. Login Tivendo → verifica empresa MERCADO HOUSE SPA (auto-corrige si está en NO USAR)
  2. Clic en Punto de Ventas
  3. Artículos → Listado de artículos → Exportar (espera descarga larga)
  4. Login mercadohouse.cl
  5. Configuración → Importar Lista de Artículos
  6. Selecciona archivo → Cargar Lista de Artículos
  7. Borra el archivo descargado
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime
from app_paths import runtime_path, configure_playwright_browsers, DESCARGA_DIR
configure_playwright_browsers()
from playwright.async_api import async_playwright
from utils import (
    asegurar_empresa_mercadohouse,
    click_y_capturar_pagina,
    crear_logger,
    crear_pagina_trabajo,
    guardar_diagnostico,
    ir_a_importadores_mercadohouse,
    limpiar_carpeta_archivos,
    esperar_confirmacion_carga_mercadohouse,
    login_tivendo,
    login_mercadohouse,
    MedidorEtapas,
    subir_archivo_mercadohouse,
    rotar_log,
)
from credenciales import TIVENDO_EMAIL, TIVENDO_PASSWORD, MH_EMAIL, MH_PASSWORD

# ============================================================
#  CONFIGURACIÓN — edita credenciales en credenciales.py
# ============================================================

# Carpeta donde se guardará el archivo descargado
CARPETA_DESCARGA = str(DESCARGA_DIR)

# True = ver el navegador, False = correr invisible
MOSTRAR_NAVEGADOR = True
# ============================================================

LOG_FILE = runtime_path("log_articulos.txt")
log = crear_logger(LOG_FILE)


async def sincronizar():
    rotar_log(LOG_FILE)
    medidor = MedidorEtapas(log)

    log("=" * 55)
    log("INICIO SINCRONIZACIÓN ARTÍCULOS TIVENDO → MERCADOHOUSE")
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
            # ── 1. LOGIN TIVENDO ───────────────────────────────────
            with medidor.etapa("Login Tivendo"):
                await login_tivendo(page, TIVENDO_EMAIL, TIVENDO_PASSWORD, log)

            # ── 2. VERIFICAR EMPRESA + IR A PUNTO DE VENTAS ───────
            # Asegurarse de estar en MERCADO HOUSE SPA antes de entrar
            # al módulo. Si la sesión quedó en "NO USAR", se corrige aquí.
            with medidor.etapa("Verificar empresa"):
                await asegurar_empresa_mercadohouse(page, log, TIVENDO_EMAIL, TIVENDO_PASSWORD)

            with medidor.etapa("Abrir Punto de Ventas"):
                log("Haciendo clic en Punto de Ventas...")
                pos_page, nueva_pestana = await click_y_capturar_pagina(
                    context,
                    page,
                    page.get_by_text("Punto de Ventas"),
                )

            # Capturar nueva pestaña si abrió
            if nueva_pestana:
                log("Punto de Ventas abrió en pestaña nueva")
            else:
                log("Punto de Ventas navegó en la misma pestaña")

            await pos_page.locator("text=Artículos").first.wait_for(state="visible", timeout=25000)
            log(f"✓ En Punto de Ventas — {pos_page.url}")

            # ── 3. IR A ARTÍCULOS → LISTADO DE ARTÍCULOS ──────────
            log("Navegando a Artículos → Listado de artículos...")

            # Clic en menú "Artículos" del sidebar
            await pos_page.locator("text=Artículos").first.click()
            await pos_page.get_by_role("link", name="Listado de artículos", exact=True).wait_for(state="visible", timeout=10000)

            # Clic en submenú "Listado de artículos"
            await pos_page.get_by_role("link", name="Listado de artículos", exact=True).click()
            await pos_page.get_by_role("button", name="Exportar").wait_for(state="visible", timeout=25000)
            log(f"✓ En Listado de artículos — {pos_page.url}")

            # ── 4. EXPORTAR (descarga puede demorar varios minutos) ─
            with medidor.etapa("Exportar articulos Tivendo"):
                log("Haciendo clic en Exportar... (puede demorar varios minutos)")
                async with pos_page.expect_download(timeout=300000) as download_info:
                    await pos_page.get_by_role("button", name="Exportar").click()

                download = await download_info.value
                nombre_archivo = download.suggested_filename or "listado_articulos.xlsx"
                ruta_archivo = str(Path(CARPETA_DESCARGA) / nombre_archivo)
                await download.save_as(ruta_archivo)

            log(f"✓ Archivo descargado: {nombre_archivo}")
            log(f"  Guardado en: {ruta_archivo}")

            # ── 5. LOGIN MERCADOHOUSE ──────────────────────────────
            with medidor.etapa("Login Mercadohouse"):
                await login_mercadohouse(page, MH_EMAIL, MH_PASSWORD, log)

            # ── 6. IR A CONFIGURACIÓN → IMPORTADORES → LISTA DE ARTÍCULOS ─
            await ir_a_importadores_mercadohouse(page, "LISTA DE ARTÍCULOS", log)
            log("✓ Pestaña Lista de Artículos abierta")

            # ── 7. SUBIR EL ARCHIVO ────────────────────────────────
            with medidor.etapa("Subir articulos Mercadohouse"):
                await subir_archivo_mercadohouse(
                    page,
                    ruta_archivo,
                    nombre_archivo,
                    "CARGAR LISTA DE ARTÍCULOS",
                    log,
                )
                await esperar_confirmacion_carga_mercadohouse(page, log)
            log("✓ Lista de artículos cargada")

            # ── 9. LIMPIAR CARPETA DE DESCARGAS ────────────────
            try:
                n = limpiar_carpeta_archivos(CARPETA_DESCARGA)
                log(f"🗑️  Carpeta limpiada: {n} archivo(s) eliminado(s)")
            except Exception as e:
                log(f"⚠️  No se pudo limpiar la carpeta: {e}")

            log("=" * 55)
            log("✅ SINCRONIZACIÓN DE ARTÍCULOS COMPLETADA EXITOSAMENTE")
            log("=" * 55)

        except Exception as e:
            log(f"❌ ERROR: {e}")
            try:
                captura = pos_page if "pos_page" in dir() else page
                await guardar_diagnostico(captura, e, log, "sync_articulos", medidor)
            except Exception:
                pass
            medidor.resumen()

            # Intentar limpiar carpeta aunque haya error
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
    asyncio.run(sincronizar())
