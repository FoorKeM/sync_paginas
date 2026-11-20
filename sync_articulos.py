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
from app_paths import runtime_path, configure_playwright_browsers, DESCARGA_DIR
configure_playwright_browsers()
from playwright.async_api import async_playwright
from utils import (
    ErrorAccesoDefontana,
    ErrorAccesoMercadohouse,
    ErrorCredenciales,
    ErrorEtapaFuente,
    abrir_seccion_pos,
    asegurar_empresa_mercadohouse,
    asegurar_punto_ventas_abierto,
    crear_logger,
    crear_pagina_trabajo,
    guardar_diagnostico,
    exportar_excel_pos,
    ir_a_importadores_mercadohouse,
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
MOSTRAR_NAVEGADOR = False
# ============================================================

LOG_FILE = runtime_path("log_articulos.txt")
log = crear_logger(LOG_FILE)


async def sincronizar():
    rotar_log(LOG_FILE)
    medidor = MedidorEtapas(log, "Artículos")

    log("=" * 55)
    log("INICIO SINCRONIZACIÓN ARTÍCULOS TIVENDO → MERCADOHOUSE")
    log("ℹ️ Sincroniza el catálogo completo — no depende de la sucursal activa")
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
                pos_page = await asegurar_punto_ventas_abierto(context, page, log)
            log(f"✓ En Punto de Ventas — {pos_page.url}")

            # ── 3. IR A ARTÍCULOS → LISTADO DE ARTÍCULOS ──────────
            with medidor.etapa("Entrar al listado de artículos"):
                await abrir_seccion_pos(pos_page, "Listado de artículos", log)
            log(f"✓ En Listado de artículos — {pos_page.url}")

            # ── 4. EXPORTAR (descarga puede demorar varios minutos) ─
            with medidor.etapa("Exportar articulos Tivendo"):
                nombre_archivo, ruta_archivo = await exportar_excel_pos(
                    pos_page, CARPETA_DESCARGA, "listado_articulos.xlsx", log
                )

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

            log("=" * 55)
            log("✅ SINCRONIZACIÓN DE ARTÍCULOS COMPLETADA EXITOSAMENTE")
            log("=" * 55)

        except asyncio.CancelledError:
            await browser.close()
            raise
        except Exception as e:
            if isinstance(e, (ErrorCredenciales, ErrorAccesoDefontana, ErrorAccesoMercadohouse)):
                await browser.close()
                raise
            log(f"❌ ERROR: {e}")
            try:
                captura = pos_page if "pos_page" in dir() else page
                await guardar_diagnostico(captura, e, log, "sync_articulos", medidor)
            except Exception:
                pass
            medidor.resumen()

            await browser.close()
            raise ErrorEtapaFuente("Artículos", medidor.ultima_etapa or "Inicio", e) from e

        await browser.close()
        medidor.resumen()
        log("")


if __name__ == "__main__":
    asyncio.run(sincronizar())
