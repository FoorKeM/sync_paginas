"""
SINCRONIZADOR DE PRECIOS: TIVENDO ERP → MERCADOHOUSE
Flujo:
  1. Login Tivendo → verifica empresa MERCADO HOUSE SPA (auto-corrige si está en NO USAR)
  2. Clic en ERP Digital
  3. Ventas → Informes de Ventas → Lista de Precios
  4. Selecciona la lista de precios de la sucursal activa → Exportar
  5. Login mercadohouse.cl
  6. Configuración → Importar Lista de Precios
  7. Selecciona local de la sucursal activa → sube el archivo → Cargar Lista de Precios
"""

import asyncio
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from datetime import datetime
from app_paths import runtime_path, configure_playwright_browsers, DESCARGA_DIR
configure_playwright_browsers()
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
import config as _cfg
from utils import (
    ErrorCredenciales,
    ErrorEtapaFuente,
    abrir_modulo_defontana,
    asegurar_empresa_mercadohouse,
    click_y_capturar_pagina,
    click_tarjeta_defontana,
    crear_logger,
    crear_pagina_trabajo,
    esperar_confirmacion_carga_mercadohouse,
    esperar_carga_ligera,
    guardar_diagnostico,
    guardar_descarga_validada,
    ir_a_importadores_mercadohouse,
    login_tivendo,
    login_mercadohouse,
    MedidorEtapas,
    pausa_corta,
    subir_archivo_mercadohouse,
    rotar_log,
)
from credenciales import TIVENDO_EMAIL, TIVENDO_PASSWORD, MH_EMAIL, MH_PASSWORD

# ============================================================
#  CONFIGURACIÓN — edita credenciales en credenciales.py
# ============================================================

# Carpeta donde se guardará el archivo descargado de Tivendo
CARPETA_DESCARGA = str(DESCARGA_DIR)

# True = ver el navegador, False = correr invisible
MOSTRAR_NAVEGADOR = False
PAUSAR_ENTRE_PASOS = False
# ============================================================

LOG_FILE = runtime_path("log_sync.txt")
log = crear_logger(LOG_FILE)
PRECIOS_ESPERADOS: dict[str, int] = {}
ERP_DASHBOARD_URL = "https://erp.defontana.com/#/dashboard"

BOTONES_CONFIRMAR_EXPORTACION = (
    "Descargar",
    "Guardar",
    "Aceptar",
    "Excel",
    "Exportar",
    "Generar",
    "Continuar",
)


class _TablaPreciosParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fila_actual = None
        self.celda_actual = None
        self.filas = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.fila_actual = []
        elif tag == "td" and self.fila_actual is not None:
            self.celda_actual = []

    def handle_data(self, data):
        if self.celda_actual is not None:
            self.celda_actual.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "td" and self.celda_actual is not None:
            self.fila_actual.append("".join(self.celda_actual).strip())
            self.celda_actual = None
        elif tag == "tr" and self.fila_actual is not None:
            self.filas.append(self.fila_actual)
            self.fila_actual = None


def leer_precios_exportados(archivo: Path) -> dict[str, int]:
    """Lee las columnas código/precio del XLS HTML o XLSX de Defontana."""
    with archivo.open("rb") as handle:
        if handle.read(2) == b"PK":
            from openpyxl import load_workbook
            handle.seek(0)
            workbook = load_workbook(handle, read_only=True, data_only=True)
            try:
                filas = (fila for hoja in workbook.worksheets
                         for fila in hoja.iter_rows(values_only=True))
                precios = {}
                for fila in filas:
                    if len(fila) < 4:
                        continue
                    codigo = str(fila[1] or "").lstrip("'").strip().upper()
                    valor = fila[3]
                    precio = (str(int(valor)) if isinstance(valor, (int, float)) else
                              str(valor or "").replace(",", "").replace(".", "").strip())
                    if codigo and precio.isdigit():
                        precios[codigo] = int(precio)
                return precios
            finally:
                workbook.close()
    contenido = archivo.read_text(encoding="latin-1", errors="replace")
    parser = _TablaPreciosParser()
    parser.feed(contenido)
    precios = {}
    for fila in parser.filas:
        if len(fila) < 4:
            continue
        codigo = fila[1].replace("\xa0", "").lstrip("'").strip().upper()
        precio = fila[3].replace("\xa0", "").replace(",", "").replace(".", "").strip()
        if codigo and precio.isdigit():
            precios[codigo] = int(precio)
    return precios


def comparar_precios_exportados(
    archivo: Path,
    esperados: dict[str, int],
) -> dict[str, tuple[int, int | None]]:
    actuales = leer_precios_exportados(archivo)
    return {
        codigo: (precio, actuales.get(codigo))
        for codigo, precio in esperados.items()
        if actuales.get(codigo) != precio
    }


async def _cancelar_tarea(tarea):
    if tarea.done():
        return
    tarea.cancel()
    try:
        await tarea
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _click_boton_confirmacion_exportacion(page_like, log_fn) -> bool:
    """Confirma ventanas nuevas de exportacion de Tivendo si aparecen."""
    candidatos = []
    for texto in BOTONES_CONFIRMAR_EXPORTACION:
        candidatos.extend([
            page_like.get_by_role("button", name=texto, exact=False).first,
            page_like.get_by_role("link", name=texto, exact=False).first,
            page_like.locator(f"input[value*='{texto}']").first,
        ])

    for candidato in candidatos:
        try:
            await candidato.wait_for(state="visible", timeout=800)
            await candidato.click(force=True, timeout=3000)
            log_fn("  Ventana de exportacion confirmada")
            return True
        except Exception:
            pass
    return False


async def _esperar_descarga_en_popup(popup, log_fn, timeout: int = 300000):
    await esperar_carga_ligera(popup, timeout=15000)
    log_fn(f"  Ventana de exportacion abierta: {popup.url}")

    download_task = asyncio.create_task(popup.wait_for_event("download", timeout=timeout))
    try:
        for _ in range(12):
            if download_task.done():
                return await download_task

            confirmado = await _click_boton_confirmacion_exportacion(popup, log_fn)
            if confirmado:
                return await download_task

            for frame in popup.frames:
                if download_task.done():
                    return await download_task
                if frame == popup.main_frame:
                    continue
                if await _click_boton_confirmacion_exportacion(frame, log_fn):
                    return await download_task

            await pausa_corta(0.5)

        return await download_task
    finally:
        await _cancelar_tarea(download_task)


async def exportar_precios_tivendo(erp_page, report_frame, carpeta_descarga: str, log_fn):
    """Escucha ambas páginas antes del clic y cierra popups tras guardar el Excel."""
    exportar = report_frame.locator(
        "a:has-text('Exportar'), button:has-text('Exportar'), input[value='Exportar']"
    ).last
    await exportar.wait_for(state="visible", timeout=15000)
    recibido = asyncio.get_running_loop().create_future()
    popups = []
    confirmaciones = []

    def al_descargar(download):
        if not recibido.done():
            recibido.set_result(download)

    async def confirmar(popup):
        try:
            await esperar_carga_ligera(popup)
            limite = asyncio.get_running_loop().time() + 15
            while not recibido.done() and not popup.is_closed() and asyncio.get_running_loop().time() < limite:
                if await _click_boton_confirmacion_exportacion(popup, log_fn):
                    return
                for frame in popup.frames:
                    if frame != popup.main_frame and await _click_boton_confirmacion_exportacion(frame, log_fn):
                        return
                await pausa_corta(0.3)
        except Exception as error:
            log_fn(f"  Ventana de exportación todavía no lista: {type(error).__name__}")

    def al_abrir_popup(popup):
        popups.append(popup)
        popup.on("download", al_descargar)
        confirmaciones.append(asyncio.create_task(confirmar(popup)))

    erp_page.on("download", al_descargar)
    erp_page.on("popup", al_abrir_popup)
    try:
        log_fn("Pulsando Exportar; escuchando la página y cualquier ventana nueva...")
        await exportar.click(force=True)
        try:
            download = await asyncio.wait_for(recibido, timeout=300)
        except asyncio.TimeoutError as exc:
            detalle = ("la ventana no entregó el archivo" if popups
                       else "no comenzó la descarga ni apareció una ventana")
            raise RuntimeError(f"Lista de Precios: {detalle}") from exc
        log_fn("Descarga detectada; esperando que termine y comprobando el Excel...")
        return await guardar_descarga_validada(
            download, carpeta_descarga, "lista_precios.xls", log_fn,
            leer_precios_exportados, provisional=True,
        )
    finally:
        erp_page.remove_listener("download", al_descargar)
        erp_page.remove_listener("popup", al_abrir_popup)
        for popup in popups:
            popup.remove_listener("download", al_descargar)
        for tarea in confirmaciones:
            if not tarea.done():
                tarea.cancel()
        await asyncio.gather(*confirmaciones, return_exceptions=True)
        await asyncio.gather(*(popup.close() for popup in popups if not popup.is_closed()),
                             return_exceptions=True)
        if not recibido.done():
            recibido.cancel()


async def pausar_paso(mensaje: str):
    if not PAUSAR_ENTRE_PASOS:
        return
    print()
    input(f"  PAUSA: {mensaje}\n  Revisa el navegador y presiona ENTER para continuar...")
    print()


async def click_informes_ventas_clasico(page, log_fn):
    """Hace clic en el informe clasico, no en 'Informes de Ventas (Nuevo)'."""
    enlaces_exactos = page.get_by_role("link", name="Informes de Ventas", exact=True)
    cantidad = await enlaces_exactos.count()
    if cantidad > 0:
        destino = enlaces_exactos.last
        href = await destino.get_attribute("href")
        url_real = urljoin(page.url, href) if href else None
        if url_real and urlsplit(url_real).hostname != "erp.defontana.com":
            url_real = None
        await destino.scroll_into_view_if_needed()
        await destino.click()
        log_fn(f"  Informe clasico seleccionado por enlace exacto ({cantidad} encontrado(s))")
        return url_real

    resultado = await page.evaluate(
        """
        () => {
            const normalizar = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
            const visibles = Array.from(document.querySelectorAll('a, [role="link"], li, div, span'))
                .filter((el) => {
                    const texto = normalizar(el.innerText || el.textContent);
                    if (texto !== 'Informes de Ventas') return false;
                    const estilo = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return estilo.display !== 'none'
                        && estilo.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0;
                });
            const destino = visibles[visibles.length - 1];
            if (!destino) {
                return { ok: false, encontrados: visibles.length };
            }
            destino.scrollIntoView({ block: 'center', inline: 'nearest' });
            destino.click();
            return { ok: true, encontrados: visibles.length };
        }
        """
    )
    if not resultado.get("ok"):
        raise Exception("No se encontro el enlace clasico 'Informes de Ventas' (sin Nuevo).")
    log_fn(f"  Informe clasico seleccionado por texto visible ({resultado.get('encontrados')} encontrado(s))")
    return None


async def click_tarjeta_erp_digital(page, log_fn):
    """Abre la tarjeta ERP Digital del portal."""
    await click_tarjeta_defontana(page, "ERP Digital")
    log_fn("  Click tarjeta ERP Digital")


async def esperar_erp_digital_abierto(context, page, log_fn):
    """Espera señales reales del ERP; evita confundir el portal con el módulo ERP."""
    limite = asyncio.get_event_loop().time() + 45
    uso_url_directa = False
    while asyncio.get_event_loop().time() < limite:
        if "erp.defontana.com" in page.url:
            await esperar_carga_ligera(page)
            log_fn("✓ ERP Digital detectado por URL")
            return page

        for texto in ("Ecosistema Digital", "Clientes y Productos", "Listado de Documentos"):
            try:
                if await page.get_by_text(texto, exact=True).count() > 0:
                    log_fn(f"✓ ERP Digital detectado por menú: {texto}")
                    return page
            except Exception:
                pass

        # Si seguimos en el portal, a veces el primer clic solo enfoca la tarjeta.
        try:
            en_portal = "portal.defontana.com/dashboard" in page.url
            if en_portal:
                if not uso_url_directa and asyncio.get_event_loop().time() > limite - 25:
                    uso_url_directa = True
                    log_fn("  ERP sigue en portal; abriendo URL directa del ERP...")
                    await page.goto(ERP_DASHBOARD_URL, timeout=30000)
                    continue
                log_fn("  Aun en portal; reintentando click en tarjeta ERP Digital...")
                page, _ = await click_y_capturar_pagina(
                    context, page, lambda: click_tarjeta_erp_digital(page, log_fn), timeout=4000,
                )
                continue
        except Exception:
            pass

        await pausa_corta(0.5)

    raise Exception(f"ERP Digital no terminó de abrir. URL actual: {page.url}")


async def sincronizar():
    rotar_log(LOG_FILE)
    medidor = MedidorEtapas(log, "Precios")

    log("=" * 55)
    log("INICIO SINCRONIZACIÓN TIVENDO → MERCADOHOUSE")
    suc_activa = _cfg.sucursal_activa()
    log(f"🏬 Sucursal activa : {suc_activa['nombre']}")
    log(f"   Lista Tivendo   : {suc_activa['tivendo_lista_erp']}")
    log(f"   Local Mercadohouse : {suc_activa['mh_local']}")
    log("=" * 55)

    # Crear carpeta de descarga si no existe
    Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser, context, page = await crear_pagina_trabajo(
            p,
            mostrar_navegador=MOSTRAR_NAVEGADOR,
            carpeta_descarga=CARPETA_DESCARGA,
            incognito=True,
        )
        # Contexto incógnito: sin caché ni cookies previas, evita bugs de DNS
        try:
            # ── 1. LOGIN TIVENDO ───────────────────────────────────
            with medidor.etapa("Login Tivendo"):
                await login_tivendo(page, TIVENDO_EMAIL, TIVENDO_PASSWORD, log)
            await pausar_paso("Login Tivendo terminado")

            # ── 2. VERIFICAR EMPRESA + IR A ERP DIGITAL ───────────
            # Asegurarse de estar en MERCADO HOUSE SPA antes de entrar
            # al módulo. Si la sesión quedó en "NO USAR", se corrige aquí.
            with medidor.etapa("Verificar empresa"):
                await asegurar_empresa_mercadohouse(page, log, TIVENDO_EMAIL, TIVENDO_PASSWORD)
            await pausar_paso("Empresa verificada")

            with medidor.etapa("Abrir ERP Digital"):
                erp_page = await abrir_modulo_defontana(
                    context, page, "ERP Digital", "erp.defontana.com",
                    ("Inventario", "Ventas", "Clientes y Productos", "Listado de Documentos"),
                    log, url_directa=ERP_DASHBOARD_URL,
                )
            log(f"✓ En ERP Digital — {erp_page.url}")

            # ── 3. NAVEGAR A INFORMES DE VENTAS ───────────────────
            medidor.informar_etapa("Entrar a Informes de Ventas")
            log("Navegando a Ventas > Informes de Ventas...")
            # Clic en menú Ventas (primer nivel)
            await erp_page.locator("text=Ventas").first.click()
            await pausa_corta(0.2)
            # Clic en submenú Ventas
            ventas = erp_page.locator("text=Ventas")
            cnt = await ventas.count()
            if cnt >= 2:
                await ventas.nth(1).click()
            await pausa_corta(0.2)
            # Clic en "Informes de Ventas" — excluye "Informes de Ventas (Nuevo)"
            url_informe = await click_informes_ventas_clasico(erp_page, log)
            try:
                await erp_page.locator("text=Lista de Precios").last.wait_for(state="visible", timeout=25000)
            except Exception:
                if not url_informe:
                    raise
                log("El informe quedó incompleto; recuperando desde su enlace real...")
                await erp_page.goto(url_informe, wait_until="domcontentloaded", timeout=30000)
                await erp_page.locator("text=Lista de Precios").last.wait_for(state="visible", timeout=25000)
            log(f"✓ En Informes de Ventas — {erp_page.url}")

            # ── 3b. ABRIR PESTAÑA LISTA DE PRECIOS ────────────────
            medidor.informar_etapa("Abrir Lista de Precios")
            log("Abriendo pestaña Lista de Precios...")
            lista_tab = erp_page.locator("text=Lista de Precios").last
            await lista_tab.scroll_into_view_if_needed()
            await lista_tab.click()
            await pausa_corta(0.5)
            log("✓ Pestaña Lista de Precios abierta")

            # ── 4. SELECCIONAR LISTA DE LA SUCURSAL ACTIVA ─────────
            medidor.informar_etapa("Seleccionar lista configurada")
            log(f"Seleccionando lista {_cfg.sucursal_activa()['tivendo_lista_erp']}...")

            async def get_report_frame(base_page):
                """Devuelve el frame que contiene el formulario de Lista de Precios,
                o el page principal si no hay frames."""
                limite = asyncio.get_event_loop().time() + 15
                while asyncio.get_event_loop().time() < limite:
                    frames = base_page.frames
                    for f in frames:
                        try:
                            cnt = await f.locator("input[type='radio']").count()
                            if cnt > 0:
                                log(f"  Frames detectados: {len(frames)}")
                                log(f"    → Frame con {cnt} radios encontrado")
                                return f
                        except Exception:
                            pass
                    await pausa_corta(0.3)
                frames = base_page.frames
                log(f"  Frames detectados: {len(frames)}")
                log("  → Usando page principal (sin iframes con radios)")
                return base_page

            rframe = await get_report_frame(erp_page)

            # Clic en radio "Una en particular" usando JavaScript dentro del frame
            radio_js = """
                () => {
                    const radios = document.querySelectorAll('input[type="radio"]');
                    for (const r of radios) {
                        const txt = (r.parentElement?.innerText || r.labels?.[0]?.innerText || '');
                        if (txt.toLowerCase().includes('una en particular')) {
                            r.click();
                            return 'ok-text:' + txt.trim();
                        }
                    }
                    // fallback: segundo radio
                    if (radios.length >= 2) {
                        radios[1].click();
                        return 'ok-nth1-fallback';
                    }
                    return 'not-found:total=' + radios.length;
                }
            """
            result = await rframe.evaluate(radio_js)
            log(f"  Radio click: {result}")

            # Esperar el select con timeout generoso
            select_loc = rframe.locator("select").first
            try:
                await select_loc.wait_for(state="visible", timeout=10000)
            except Exception:
                log("  select no apareció vía wait_for, intentando igual...")

            suc = _cfg.sucursal_activa()
            lista_erp = suc["tivendo_lista_erp"]
            palabra = lista_erp.split()[1] if len(lista_erp.split()) > 1 else lista_erp
            try:
                await select_loc.locator("option").filter(
                    has_text=re.compile(re.escape(palabra), re.IGNORECASE)
                ).first.wait_for(state="attached", timeout=15000)
            except Exception:
                log("  La lista configurada aún no aparece; revisando opciones disponibles...")
            options = await rframe.evaluate(
                "() => Array.from(document.querySelectorAll('select option')).map(o => ({v: o.value, t: o.text.trim()}))"
            )
            log(f"  Opciones en select: {[o['t'] for o in options[:10]]}")
            match = next((o for o in options if lista_erp.upper() in o["t"].upper()), None)
            if not match:
                # fallback: buscar por cualquier palabra clave de la lista
                kw = lista_erp.split()[1] if len(lista_erp.split()) > 1 else lista_erp
                match = next((o for o in options if kw.upper() in o["t"].upper()), None)
            if not match:
                raise Exception(f"No se encontró '{lista_erp}' en el dropdown. Opciones: {[o['t'] for o in options]}")
            await select_loc.select_option(value=match["v"])
            await pausa_corta(0.2)
            log(f"✓ Lista seleccionada: {match['t']}")

            # ── 5. EXPORTAR Y VERIFICAR PROPAGACIÓN ────────────────
            max_exportaciones = 3 if PRECIOS_ESPERADOS else 1
            for intento_exportacion in range(1, max_exportaciones + 1):
                with medidor.etapa("Exportar precios Tivendo"):
                    log(f"Haciendo clic en Exportar... (intento {intento_exportacion}/{max_exportaciones})")
                    nombre_archivo, ruta_archivo = await exportar_precios_tivendo(
                        erp_page,
                        rframe,
                        CARPETA_DESCARGA,
                        log,
                    )

                archivo_precio = Path(ruta_archivo)
                if not PRECIOS_ESPERADOS:
                    break

                diferencias = comparar_precios_exportados(archivo_precio, PRECIOS_ESPERADOS)
                if not diferencias:
                    log(
                        f"✓ ERP confirmado: {len(PRECIOS_ESPERADOS)} precio(s) "
                        "coinciden con el cambio recién aplicado"
                    )
                    break

                log(
                    f"ERP aún no refleja {len(diferencias)} de "
                    f"{len(PRECIOS_ESPERADOS)} precio(s)."
                )
                for codigo, (esperado, actual) in list(diferencias.items())[:10]:
                    log(f"  {codigo}: esperado {esperado}, exportado {actual}")

                if intento_exportacion == max_exportaciones:
                    archivo_precio.unlink(missing_ok=True)
                    raise Exception(
                        "El ERP no actualizó los precios a tiempo; "
                        "se canceló la carga a Mercadohouse para no subir valores antiguos."
                    )

                archivo_precio.unlink(missing_ok=True)
                espera = 45
                log(f"Esperando {espera}s y volviendo a exportar la lista...")
                await asyncio.sleep(espera)

            archivo_final = Path(CARPETA_DESCARGA) / nombre_archivo
            os.replace(ruta_archivo, archivo_final)
            ruta_archivo = str(archivo_final)
            log(f"✓ Archivo descargado y comprobado: {nombre_archivo}")
            log(f"  Guardado en: {ruta_archivo}")
            archivo_precio = Path(ruta_archivo)
            if not archivo_precio.exists():
                raise Exception(f"El archivo exportado no existe antes de subirlo: {ruta_archivo}")
            modificado = datetime.fromtimestamp(archivo_precio.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            log(f"  Verificacion archivo a subir: {archivo_precio.name}")
            log(f"  Tamano: {archivo_precio.stat().st_size} bytes | Modificado: {modificado}")

            # ── 6. LOGIN MERCADOHOUSE ──────────────────────────────
            with medidor.etapa("Login Mercadohouse"):
                await login_mercadohouse(page, MH_EMAIL, MH_PASSWORD, log)

            # ── 7. IR A CONFIGURACIÓN → IMPORTADORES → LISTA DE PRECIOS ──
            await ir_a_importadores_mercadohouse(page, "LISTA DE PRECIOS", log)
            await page.get_by_role("combobox").wait_for(state="visible", timeout=15000)

            # ── 8. SELECCIONAR LOCAL DE LA SUCURSAL ACTIVA ─────────
            log(f"Seleccionando local {_cfg.sucursal_activa()['mh_local']}...")
            # Abrir el dropdown MUI y usar role=option para evitar strict mode violation
            await page.get_by_role("combobox").click()
            await pausa_corta(0.2)
            mh_local = _cfg.sucursal_activa()["mh_local"]
            await page.get_by_role("option", name=mh_local).click()
            await pausa_corta(0.2)
            log("✓ Local seleccionado")

            # ── 9. SUBIR EL ARCHIVO DESCARGADO ─────────────────────
            with medidor.etapa("Subir precios Mercadohouse"):
                archivo_precio = Path(ruta_archivo)
                if not archivo_precio.exists():
                    raise Exception(f"No existe el archivo que se intentara subir: {ruta_archivo}")
                modificado = datetime.fromtimestamp(archivo_precio.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                log(f"Subiendo archivo exacto: {archivo_precio}")
                log(f"Tamano: {archivo_precio.stat().st_size} bytes | Modificado: {modificado}")
                await subir_archivo_mercadohouse(
                    page,
                    ruta_archivo,
                    nombre_archivo,
                    "CARGAR LISTA DE PRECIOS",
                    log,
                )
                await esperar_confirmacion_carga_mercadohouse(page, log)
            log("✓ Lista cargada")

            log("=" * 55)
            log("✅ SINCRONIZACIÓN COMPLETADA EXITOSAMENTE")
            log(f"   Local:   {_cfg.sucursal_activa()['mh_local']}")
            log("=" * 55)

        except asyncio.CancelledError:
            if 'ruta_archivo' in locals() and str(ruta_archivo).endswith('.pending'):
                Path(ruta_archivo).unlink(missing_ok=True)
            await browser.close()
            raise
        except Exception as e:
            if 'ruta_archivo' in locals() and str(ruta_archivo).endswith('.pending'):
                Path(ruta_archivo).unlink(missing_ok=True)
            if isinstance(e, ErrorCredenciales):
                await browser.close()
                raise
            log(f"❌ ERROR: {e}")
            captura = erp_page if 'erp_page' in dir() else page
            await guardar_diagnostico(captura, e, log, "sync_precios", medidor)
            medidor.resumen()
            await browser.close()
            raise ErrorEtapaFuente("Precios", medidor.ultima_etapa or "Inicio", e) from e

        await browser.close()
        medidor.resumen()
        log("")


if __name__ == "__main__":
    asyncio.run(sincronizar())
