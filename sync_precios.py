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
import sys
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime
from app_paths import runtime_path, configure_playwright_browsers, DESCARGA_DIR
configure_playwright_browsers()
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
import config as _cfg
from utils import (
    asegurar_empresa_mercadohouse,
    click_y_capturar_pagina,
    crear_logger,
    crear_pagina_trabajo,
    esperar_confirmacion_carga_mercadohouse,
    esperar_carga_ligera,
    guardar_diagnostico,
    ir_a_importadores_mercadohouse,
    limpiar_carpeta_archivos,
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
    """Lee el .xls HTML exportado por Defontana sin librerías externas."""
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
    """Exporta precios aunque Tivendo intercale una ventana nueva antes de descargar."""
    exportar = report_frame.locator(
        "a:has-text('Exportar'), button:has-text('Exportar'), input[value='Exportar']"
    ).last

    download_task = asyncio.create_task(erp_page.wait_for_event("download", timeout=300000))
    popup_task = asyncio.create_task(erp_page.wait_for_event("popup", timeout=0))
    try:
        await exportar.click(force=True)
        done, _pending = await asyncio.wait(
            {download_task, popup_task},
            timeout=35,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if download_task in done:
            download = await download_task
        elif popup_task in done:
            popup = await popup_task
            await _cancelar_tarea(download_task)
            download = await _esperar_descarga_en_popup(popup, log_fn)
        else:
            await _cancelar_tarea(popup_task)
            download = await download_task

        nombre_archivo = download.suggested_filename or "lista_precios.xlsx"
        ruta_archivo = str(Path(carpeta_descarga) / nombre_archivo)
        await download.save_as(ruta_archivo)
        return nombre_archivo, ruta_archivo
    except PlaywrightTimeoutError as exc:
        raise Exception(
            "Tivendo no inicio la descarga de Lista de Precios. "
            "Si quedo una ventana abierta, revisa si cambio el boton de confirmacion."
        ) from exc
    finally:
        await _cancelar_tarea(download_task)
        await _cancelar_tarea(popup_task)


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
        await destino.scroll_into_view_if_needed()
        await destino.click()
        log_fn(f"  Informe clasico seleccionado por enlace exacto ({cantidad} encontrado(s))")
        return

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


async def click_tarjeta_erp_digital(page, log_fn):
    """Hace click en la tarjeta completa del portal, no solo en el texto."""
    resultado = await page.evaluate(
        """
        () => {
            const normalizar = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
            const visibles = Array.from(document.querySelectorAll('a, button, mat-card, [role="button"], div, span'))
                .filter((el) => {
                    const texto = normalizar(el.innerText || el.textContent);
                    if (texto !== 'ERP Digital') return false;
                    const estilo = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return estilo.display !== 'none'
                        && estilo.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0;
                });
            const texto = visibles[visibles.length - 1];
            if (!texto) return { ok: false, encontrados: visibles.length };

            let destino = texto.closest('a, button, [role="button"], mat-card, .mat-mdc-card, .mat-card');
            if (!destino) {
                destino = texto;
                for (let i = 0; i < 4 && destino.parentElement; i++) {
                    destino = destino.parentElement;
                    const cursor = window.getComputedStyle(destino).cursor;
                    if (cursor === 'pointer' || destino.onclick) break;
                }
            }
            destino.scrollIntoView({ block: 'center', inline: 'nearest' });
            destino.click();
            return { ok: true, tag: destino.tagName, encontrados: visibles.length };
        }
        """
    )
    if not resultado.get("ok"):
        raise Exception("No se encontro la tarjeta 'ERP Digital' en el portal.")
    log_fn(f"  Click tarjeta ERP Digital ({resultado.get('tag')}, {resultado.get('encontrados')} encontrado(s))")


async def esperar_erp_digital_abierto(page, log_fn):
    """Espera señales reales del ERP; evita confundir el portal con el módulo ERP."""
    limite = asyncio.get_event_loop().time() + 45
    uso_url_directa = False
    while asyncio.get_event_loop().time() < limite:
        if "erp.defontana.com" in page.url:
            await esperar_carga_ligera(page)
            log_fn("âœ“ ERP Digital detectado por URL")
            return

        for texto in ("Ecosistema Digital", "Clientes y Productos", "Listado de Documentos"):
            try:
                if await page.get_by_text(texto, exact=True).count() > 0:
                    log_fn(f"✓ ERP Digital detectado por menú: {texto}")
                    return
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
                await click_tarjeta_erp_digital(page, log_fn)
                continue
            erp_card = page.get_by_text("ERP Digital", exact=True).last
            if en_portal and await erp_card.count() > 0:
                log_fn("  Aún en portal; reintentando clic en ERP Digital...")
                await erp_card.click()
        except Exception:
            pass

        await pausa_corta(0.5)

    raise Exception(f"ERP Digital no terminó de abrir. URL actual: {page.url}")


async def sincronizar():
    rotar_log(LOG_FILE)
    medidor = MedidorEtapas(log)

    log("=" * 55)
    log("INICIO SINCRONIZACIÓN TIVENDO → MERCADOHOUSE")
    log("=" * 55)

    # Crear carpeta de descarga si no existe
    Path(CARPETA_DESCARGA).mkdir(parents=True, exist_ok=True)
    try:
        n = limpiar_carpeta_archivos(CARPETA_DESCARGA)
        if n:
            log(f"Carpeta de descarga limpiada antes de exportar precios: {n} archivo(s)")
    except Exception as e:
        log(f"No se pudo limpiar la carpeta antes de exportar precios: {e}")

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
                log("Haciendo clic en ERP Digital...")
                erp_page, nueva_pestana = await click_y_capturar_pagina(
                    context,
                    page,
                    page.get_by_text("ERP Digital", exact=True).last,
                )

            # Capturar nueva pestaña si abrió, o usar la misma
            if nueva_pestana:
                log("ERP Digital abrió en pestaña nueva")
            else:
                log("ERP Digital navegó en la misma pestaña")

            await esperar_erp_digital_abierto(erp_page, log)
            log(f"✓ En ERP Digital — {erp_page.url}")

            # ── 3. NAVEGAR A INFORMES DE VENTAS ───────────────────
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
            await click_informes_ventas_clasico(erp_page, log)
            await erp_page.locator("text=Lista de Precios").last.wait_for(state="visible", timeout=25000)
            log(f"✓ En Informes de Ventas — {erp_page.url}")

            # ── 3b. ABRIR PESTAÑA LISTA DE PRECIOS ────────────────
            log("Abriendo pestaña Lista de Precios...")
            lista_tab = erp_page.locator("text=Lista de Precios").last
            await lista_tab.scroll_into_view_if_needed()
            await lista_tab.click()
            await pausa_corta(0.5)
            log("✓ Pestaña Lista de Precios abierta")

            # ── 4. SELECCIONAR LISTA DE LA SUCURSAL ACTIVA ─────────
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

            # Obtener opciones para diagnóstico y selección robusta
            options = await rframe.evaluate(
                "() => Array.from(document.querySelectorAll('select option')).map(o => ({v: o.value, t: o.text.trim()}))"
            )
            log(f"  Opciones en select: {[o['t'] for o in options[:10]]}")

            suc = _cfg.sucursal_activa()
            lista_erp = suc["tivendo_lista_erp"]
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
            max_exportaciones = 5 if PRECIOS_ESPERADOS else 1
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
                    raise Exception(
                        "El ERP no actualizó los precios a tiempo; "
                        "se canceló la carga a Mercadohouse para no subir valores antiguos."
                    )

                archivo_precio.unlink(missing_ok=True)
                espera = 45
                log(f"Esperando {espera}s y volviendo a exportar la lista...")
                await asyncio.sleep(espera)

            log(f"✓ Archivo descargado: {nombre_archivo}")
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

            # ── 11. LIMPIAR CARPETA DE DESCARGAS ───────────────────
            try:
                n = limpiar_carpeta_archivos(CARPETA_DESCARGA)
                log(f"🗑️  Carpeta limpiada: {n} archivo(s) eliminado(s)")
            except Exception as e:
                log(f"⚠️  No se pudo limpiar la carpeta: {e}")

            log("=" * 55)
            log("✅ SINCRONIZACIÓN COMPLETADA EXITOSAMENTE")
            log(f"   Local:   {_cfg.sucursal_activa()['mh_local']}")
            log("=" * 55)

        except Exception as e:
            log(f"❌ ERROR: {e}")
            captura = erp_page if 'erp_page' in dir() else page
            await guardar_diagnostico(captura, e, log, "sync_precios", medidor)
            medidor.resumen()
            await browser.close()
            sys.exit(1)

        await browser.close()
        medidor.resumen()
        log("")


if __name__ == "__main__":
    asyncio.run(sincronizar())
