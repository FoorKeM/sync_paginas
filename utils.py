"""
utils.py — Helpers compartidos para todos los scripts de sincronización.
"""

import asyncio
import json
import os
import re
import sys
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from app_paths import DATA_DIR, DIAG_DIR


EMPRESA_CORRECTA = "MERCADO HOUSE"
SESSION_STATE_FILE = DATA_DIR / "browser_session.json"
MODO_LIMPIO_ENV = "MERCADOHOUSE_SYNC_LIMPIO"

# ── Rotación de logs ──────────────────────────────────────────────────────────
_MAX_LOG_BYTES = 512 * 1024  # 512 KB


def rotar_log(log_path: Path) -> None:
    """Rota log_path si supera 512 KB: mueve el archivo actual a .old y lo vacía."""
    log_path = Path(log_path)
    if log_path.exists() and log_path.stat().st_size > _MAX_LOG_BYTES:
        old_path = log_path.with_suffix(log_path.suffix + ".old")
        if old_path.exists():
            old_path.unlink()
        log_path.rename(old_path)


def crear_logger(log_path: Path):
    """Crea un logger simple para consola + archivo."""
    log_path = Path(log_path)

    def log(msg: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        linea = f"[{timestamp}] {msg}"
        print(linea)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(linea + "\n")

    return log


def limpiar_carpeta_archivos(carpeta: str) -> int:
    """Borra archivos directos de una carpeta, sin fallar si alguno esta bloqueado."""
    carpeta_path = Path(carpeta)
    if not carpeta_path.exists():
        return 0
    borrados = 0
    for archivo in carpeta_path.iterdir():
        if archivo.is_file():
            try:
                archivo.unlink()
                borrados += 1
            except Exception:
                pass
    return borrados


def borrar_sesion_guardada(log_fn=None) -> bool:
    """Borra cookies/sesion guardada para partir limpio en el siguiente intento."""
    if SESSION_STATE_FILE.exists():
        try:
            SESSION_STATE_FILE.unlink()
            if log_fn:
                log_fn("Sesion guardada borrada.")
            return True
        except Exception as exc:
            if log_fn:
                log_fn(f"No se pudo borrar la sesion guardada: {exc}")
    return False


def usar_modo_limpio() -> bool:
    """Indica si el intento actual debe ignorar cookies/cache guardada."""
    return os.environ.get(MODO_LIMPIO_ENV) == "1"


async def guardar_sesion_contexto(context, log_fn=None) -> None:
    """Guarda cookies/localStorage del contexto actual para acelerar futuros logins."""
    try:
        await context.storage_state(path=str(SESSION_STATE_FILE))
        if log_fn:
            log_fn("Sesion guardada actualizada.")
    except Exception as exc:
        if log_fn:
            log_fn(f"No se pudo guardar la sesion: {exc}")


class MedidorEtapas:
    """Registra duracion de etapas sin usar esos tiempos para decidir esperas."""

    def __init__(self, log_fn, fuente="Sync"):
        self.log_fn = log_fn
        self.fuente = fuente
        self.resultados = []
        self.etapa_actual = ""
        self.ultima_etapa = ""

    def etapa(self, nombre: str):
        return _EtapaMedida(self, nombre)

    def informar_etapa(self, nombre: str):
        self.ultima_etapa = nombre
        try:
            import telegram_bot
            telegram_bot.actualizar_etapa(self.fuente, nombre)
        except Exception:
            pass  # El avance no debe detener una sincronización.

    def _agregar(self, nombre: str, segundos: float) -> None:
        self.resultados.append((nombre, segundos))

    def resumen(self) -> None:
        if not self.resultados:
            return
        total = sum(segundos for _, segundos in self.resultados)
        self.log_fn("Tiempos por etapa:")
        for nombre, segundos in self.resultados:
            self.log_fn(f"  - {nombre}: {segundos:.1f}s")
        self.log_fn(f"  Total medido: {total:.1f}s")


class ErrorEtapaFuente(RuntimeError):
    def __init__(self, fuente, etapa, error):
        self.fuente = fuente
        self.etapa = etapa
        super().__init__(f"{fuente}: falló en «{etapa}»: {error}")


class _EtapaMedida:
    def __init__(self, medidor: MedidorEtapas, nombre: str):
        self.medidor = medidor
        self.nombre = nombre
        self.inicio = None

    def __enter__(self):
        self.inicio = time.perf_counter()
        self.medidor.etapa_actual = self.nombre
        self.medidor.informar_etapa(self.nombre)
        self.medidor.log_fn(f"Inicio etapa: {self.nombre}")
        return self

    def __exit__(self, exc_type, exc, tb):
        segundos = time.perf_counter() - self.inicio
        estado = "OK" if exc_type is None else "ERROR"
        self.medidor._agregar(self.nombre, segundos)
        self.medidor.log_fn(f"Fin etapa: {self.nombre} ({estado}, {segundos:.1f}s)")
        if self.medidor.etapa_actual == self.nombre:
            self.medidor.etapa_actual = ""
        return False


async def guardar_diagnostico(page, error, log_fn, proceso: str, medidor: MedidorEtapas = None) -> Path:
    """Guarda reporte, JSON y screenshot del fallo fuera de la carpeta portable."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_proceso = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in proceso)
    base = DIAG_DIR / f"{ts}_{safe_proceso}"
    paso = ""
    if medidor:
        paso = medidor.etapa_actual or medidor.ultima_etapa

    url = ""
    title = ""
    visible_text = ""
    screenshot_path = base.with_suffix(".png")
    try:
        url = getattr(page, "url", "") or ""
    except Exception:
        pass
    try:
        title = await page.title()
    except Exception:
        pass
    try:
        visible_text = await page.locator("body").inner_text(timeout=3000)
        visible_text = visible_text[:2500]
    except Exception:
        pass
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        screenshot_path = None

    data = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "proceso": proceso,
        "paso": paso,
        "url": url,
        "titulo": title,
        "error": str(error),
        "screenshot": str(screenshot_path) if screenshot_path else "",
        "texto_visible": visible_text,
        "tiempos": medidor.resultados if medidor else [],
    }
    json_path = base.with_suffix(".json")
    txt_path = base.with_suffix(".txt")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                f"Fecha: {data['fecha']}",
                f"Proceso: {proceso}",
                f"Paso: {paso}",
                f"URL: {url}",
                f"Titulo: {title}",
                f"Error: {error}",
                f"Screenshot: {data['screenshot']}",
                "",
                "Texto visible:",
                visible_text,
            ]
        ),
        encoding="utf-8",
    )
    log_fn(f"Diagnostico guardado: {txt_path}")
    return txt_path


async def reintentar_accion(nombre: str, accion, log_fn=None, intentos: int = 3, espera: float = 0.8):
    """Reintenta acciones puntuales, no el proceso completo."""
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            return await accion()
        except Exception as exc:
            ultimo_error = exc
            if intento >= intentos:
                break
            if log_fn:
                log_fn(f"Reintentando accion '{nombre}' ({intento + 1}/{intentos})...")
            await asyncio.sleep(espera)
    raise ultimo_error


async def launch_chromium(p, headless=True, downloads_path=None, args=None):
    """Lanza Chromium; en exe prefiere el navegador controlado por Playwright."""
    kwargs = {"headless": headless}
    if downloads_path is not None:
        kwargs["downloads_path"] = downloads_path
    if args is not None:
        kwargs["args"] = args

    if getattr(sys, "frozen", False):
        ultimo_error = None
        try:
            return await p.chromium.launch(**kwargs)
        except Exception as exc:
            ultimo_error = exc
        for channel in ("msedge", "chrome"):
            try:
                return await p.chromium.launch(channel=channel, **kwargs)
            except Exception as exc:
                ultimo_error = exc
        if ultimo_error:
            raise Exception(
                "No se pudo abrir Microsoft Edge ni Google Chrome. "
                "Instala Edge/Chrome o usa la version portable con ms-playwright."
            ) from ultimo_error

    return await p.chromium.launch(**kwargs)


async def crear_pagina_trabajo(
    p,
    mostrar_navegador=False,
    carpeta_descarga=None,
    incognito=False,
    accept_downloads=True,
    bypass_csp=True,
    usar_sesion_guardada=False,
):
    """Abre browser/context/page con las opciones comunes de los sincronizadores."""
    incognito = incognito or usar_modo_limpio()
    args = []
    if incognito:
        args.append("--incognito")
    browser = await launch_chromium(
        p,
        headless=not mostrar_navegador,
        downloads_path=carpeta_descarga,
        args=args or None,
    )
    context_kwargs = {
        "accept_downloads": accept_downloads,
        "bypass_csp": bypass_csp,
    }
    sesion_usada = not incognito and usar_sesion_guardada and SESSION_STATE_FILE.exists()
    if sesion_usada:
        context_kwargs["storage_state"] = str(SESSION_STATE_FILE)
    try:
        context = await browser.new_context(**context_kwargs)
    except Exception:
        if sesion_usada:
            borrar_sesion_guardada()
            context_kwargs.pop("storage_state", None)
            context = await browser.new_context(**context_kwargs)
        else:
            raise
    page = await context.new_page()
    return browser, context, page


async def pausa_corta(segundos: float = 0.2) -> None:
    await asyncio.sleep(segundos)


async def esperar_carga_ligera(page, timeout: int = 15000) -> None:
    """Espera lo justo para que el DOM este listo sin bloquearse por llamadas de fondo."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass


async def click_y_capturar_pagina(context, page, locator, timeout: int = 3500):
    """Click que puede abrir pestana nueva; si no, devuelve la misma pagina."""
    accion = locator if callable(locator) else locator.click
    try:
        async with context.expect_page(timeout=timeout) as page_info:
            await reintentar_accion("click que abre modulo", accion)
        nueva = await page_info.value
        await esperar_carga_ligera(nueva)
        return nueva, True
    except PlaywrightTimeoutError:
        await esperar_carga_ligera(page)
        return page, False


async def click_tarjeta_defontana(page, nombre: str) -> None:
    """Busca el módulo por nombre en Recientes o Todos y pulsa su tarjeta."""
    nombres = [nombre]
    if nombre == "Punto de Ventas":
        nombres.append("Punto de Venta")

    async def marcar_tarjeta():
        return await page.evaluate(
        """(nombres) => {
            const normalizar = (valor) => (valor || '').normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '').replace(/\\s+/g, ' ')
                .trim().toLocaleLowerCase('es');
            const opciones = nombres.map(normalizar);
            const visibles = [...document.querySelectorAll('a, button, [role="button"], h1, h2, h3, h4, p, div, span')]
                .filter((el) => {
                    if (!opciones.includes(normalizar(el.innerText || el.textContent))) return false;
                    const estilo = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return estilo.display !== 'none' && estilo.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0;
                });
            const texto = visibles[visibles.length - 1];
            if (!texto) return false;
            let destino = texto.closest('a, button, [role="button"], mat-card, .mat-card, .mat-mdc-card');
            if (!destino) {
                destino = texto;
                for (let i = 0; i < 4 && destino.parentElement; i++) {
                    destino = destino.parentElement;
                    const estilo = getComputedStyle(destino);
                    if (estilo.cursor === 'pointer' || destino.onclick) break;
                }
            }
            document.querySelectorAll('[data-mh-modulo]').forEach((el) => el.removeAttribute('data-mh-modulo'));
            destino.setAttribute('data-mh-modulo', nombres[0]);
            return true;
        }""",
        nombres,
        )

    resultado = await marcar_tarjeta()
    if not resultado:
        try:
            await page.get_by_text("Todos", exact=True).first.click(timeout=5000)
            await esperar_carga_ligera(page)
        except Exception:
            pass
        resultado = await marcar_tarjeta()
    if not resultado:
        raise Exception(
            f"No se encontró '{nombre}' en Módulos recientes ni en Todos del portal Defontana. "
            "Revisa si Defontana cambió el nombre del módulo."
        )
    await page.locator('[data-mh-modulo]').click(timeout=10000)


def _en_modulo(page, host: str) -> bool:
    url = urlsplit(page.url)
    return url.hostname == host and "/login" not in (url.path + url.fragment).lower()


async def abrir_modulo_defontana(context, portal, nombre, host, controles, log_fn,
                                url_directa=None, timeout=45000):
    """Reutiliza la pestaña del módulo y confirma host más controles visibles."""
    loop = asyncio.get_running_loop()
    inicio = loop.time()
    ultimo_clic = inicio - 10
    paginas_iniciales = set(context.pages)
    directa_usada = False
    ultimo_error = None
    while (loop.time() - inicio) * 1000 < timeout:
        candidatas = [p for p in reversed(context.pages) if not p.is_closed()]
        paginas_modulo = [p for p in candidatas if _en_modulo(p, host)]
        for candidata in paginas_modulo:
            for control in controles:
                try:
                    if await candidata.get_by_text(control, exact=True).first.is_visible():
                        log_fn(f"✓ {nombre} confirmado en {host}: {control}")
                        return candidata
                except Exception as exc:
                    ultimo_error = exc
        # Una pestaña nueva todavía puede estar en about:blank mientras carga.
        pestaña_pendiente = any(p not in paginas_iniciales and p.url in ("about:blank", "")
                               for p in candidatas)
        if not paginas_modulo and not pestaña_pendiente and loop.time() - ultimo_clic >= 8:
            origen = next((p for p in candidatas if "portal.defontana.com" == urlsplit(p.url).hostname), None)
            if origen:
                ultimo_clic = loop.time()
                log_fn(f"Abriendo {nombre} desde el portal...")
                try:
                    await click_tarjeta_defontana(origen, nombre)
                except Exception as exc:
                    ultimo_error = exc
        if url_directa and not directa_usada and loop.time() - inicio >= 12 and not paginas_modulo:
            directa_usada = True
            log_fn(f"{nombre} sigue en el portal; abriendo dirección conocida en la misma sesión...")
            try:
                await portal.goto(url_directa, wait_until="domcontentloaded", timeout=15000)
            except Exception as exc:
                ultimo_error = exc
        await asyncio.sleep(0.4)
    detalle = f" Último error: {ultimo_error}" if ultimo_error else ""
    raise RuntimeError(f"No se confirmó {nombre} en {host} con su menú visible.{detalle}")


async def abrir_seccion_pos(page, nombre, log_fn, timeout=30000):
    """Entra al listado y recupera su pantalla usando el href real del menú."""
    enlace = page.get_by_role("link", name=nombre, exact=True)
    if await enlace.count() == 0 or not await enlace.first.is_visible():
        log_fn("Abriendo menú Artículos...")
        await page.get_by_text("Artículos", exact=True).first.click()
    await enlace.wait_for(state="visible", timeout=15000)
    href = await enlace.get_attribute("href")
    destino = urljoin(page.url, href) if href else None
    if destino and urlsplit(destino).hostname != "tivendoapp.defontana.com":
        destino = None
    log_fn(f"Entrando al listado {nombre}...")
    await enlace.click()
    exportar = page.get_by_role("button", name="Exportar")
    try:
        if destino:
            await page.wait_for_url(destino, timeout=15000)
        log_fn("Esperando Exportar...")
        await exportar.wait_for(state="visible", timeout=timeout)
    except Exception:
        if not destino:
            raise
        log_fn(f"No apareció Exportar; recuperando {nombre} desde su enlace real...")
        await page.goto(destino, wait_until="domcontentloaded", timeout=30000)
        await exportar.wait_for(state="visible", timeout=timeout)
    return page


def validar_excel_exportado(archivo: Path, nombre: str, leer_precios=None) -> None:
    """Rechaza descargas vacías o truncadas antes de reemplazar un Excel previo."""
    if not archivo.is_file() or archivo.stat().st_size < 100:
        raise ValueError(f"{nombre}: Defontana entregó un archivo vacío o incompleto")
    extension = Path(nombre).suffix.lower()
    if extension == ".xlsx":
        from openpyxl import load_workbook
        with zipfile.ZipFile(archivo) as z:
            if z.testzip() is not None:
                raise ValueError(f"{nombre}: Excel comprimido incompleto")
        with archivo.open("rb") as handle:
            workbook = load_workbook(handle, read_only=True, data_only=True)
            try:
                valido = False
                for hoja in workbook.worksheets:
                    filas = (fila for fila in hoja.iter_rows(values_only=True)
                             if any(valor is not None and str(valor).strip() for valor in fila))
                    if next(filas, None) is not None and next(filas, None) is not None:
                        valido = True
                        break
                if not valido:
                    raise ValueError(f"{nombre}: Excel sin filas de datos")
            finally:
                workbook.close()
    elif extension == ".xls":
        with archivo.open("rb") as handle:
            cabecera = handle.read(8192).lower()
        if b"<table" not in cabecera and not cabecera.startswith(b"\xd0\xcf\x11\xe0"):
            raise ValueError(f"{nombre}: formato XLS no reconocido")
    else:
        raise ValueError(f"{nombre}: se esperaba un archivo Excel")
    if leer_precios is not None and not leer_precios(archivo):
        raise ValueError(f"{nombre}: lista de precios sin artículos válidos")


async def guardar_descarga_validada(download, carpeta, nombre_respaldo, log_fn,
                                   leer_precios=None, provisional=False):
    """Espera todos los bytes y sustituye el archivo anterior solo tras validarlo."""
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = Path(download.suggested_filename or nombre_respaldo).name
    temporal = carpeta / f".mh_{uuid.uuid4().hex}.part"
    destino = (carpeta / f".mh_{uuid.uuid4().hex}.pending") if provisional else carpeta / nombre
    try:
        fallo = await asyncio.wait_for(download.failure(), timeout=300)
        if fallo:
            raise RuntimeError(f"Defontana interrumpió la descarga: {fallo}")
        log_fn(f"Comprobando archivo {nombre}...")
        await asyncio.wait_for(download.save_as(str(temporal)), timeout=300)
        validar_excel_exportado(temporal, nombre, leer_precios)
        os.replace(temporal, destino)
        log_fn(f"✓ Excel validado: {nombre}")
        return nombre, str(destino)
    finally:
        temporal.unlink(missing_ok=True)


async def exportar_excel_pos(page, carpeta, nombre_respaldo, log_fn):
    """Escucha la página y los popups antes de Exportar; guarda tras validar."""
    boton = page.get_by_role("button", name="Exportar")
    await boton.wait_for(state="visible", timeout=30000)
    recibido = asyncio.get_running_loop().create_future()
    ventanas = []
    tareas = []

    def descarga_lista(download):
        if not recibido.done():
            recibido.set_result(download)

    async def confirmar_ventana(ventana):
        limite = asyncio.get_running_loop().time() + 15
        while not recibido.done() and not ventana.is_closed() and asyncio.get_running_loop().time() < limite:
            try:
                confirmar = ventana.get_by_role("button", name="Exportar").first
                if await confirmar.is_visible():
                    await confirmar.click()
                    return
            except Exception:
                pass
            await asyncio.sleep(0.3)

    def ventana_abierta(ventana):
        ventanas.append(ventana)
        ventana.on("download", descarga_lista)
        tareas.append(asyncio.create_task(confirmar_ventana(ventana)))

    page.on("download", descarga_lista)
    page.on("popup", ventana_abierta)
    try:
        log_fn("Pulsando Exportar; esperando descarga directa o ventana de exportación...")
        await boton.click()
        download = await asyncio.wait_for(recibido, timeout=300)
        log_fn("Descarga detectada; esperando y comprobando el Excel...")
        return await guardar_descarga_validada(download, carpeta, nombre_respaldo, log_fn)
    finally:
        page.remove_listener("download", descarga_lista)
        page.remove_listener("popup", ventana_abierta)
        for ventana in ventanas:
            ventana.remove_listener("download", descarga_lista)
        for tarea in tareas:
            tarea.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
        await asyncio.gather(*(v.close() for v in ventanas if not v.is_closed()),
                             return_exceptions=True)
        if not recibido.done():
            recibido.cancel()


async def asegurar_punto_ventas_abierto(context, page, log_fn):
    """Abre Tivendo POS desde el portal y verifica que la navegacion realmente ocurrio.

    Sin esta verificacion, un clic en 'Punto de Ventas' que no navega a tiempo deja
    la pagina en el portal; un locator de texto no exacto para 'Articulos' puede dar
    falso positivo ahi (el portal tiene 'Revisa articulos de ayuda...' en el pie de
    pagina), haciendo creer que ya se entro a Tivendo POS cuando no es asi.
    """
    return await abrir_modulo_defontana(
        context, page, "Punto de Ventas", "tivendoapp.defontana.com", ("Artículos",), log_fn
    )


async def ir_a_importadores_mercadohouse(page, nombre_tab: str, log_fn) -> None:
    """Navega en Mercadohouse hasta Configuracion > Importadores > tab indicada."""
    log_fn("Navegando a ConfiguraciÃ³n...")
    await reintentar_accion(
        "abrir configuracion Mercadohouse",
        lambda: page.goto("https://mercadohouse.cl/dashboard/configuracion", timeout=30000),
        log_fn,
    )
    await reintentar_accion(
        "esperar Importadores",
        lambda: page.get_by_role("heading", name="Importadores").wait_for(state="visible", timeout=20000),
        log_fn,
    )

    log_fn("Haciendo clic en Importadores...")
    await reintentar_accion("click Importadores", lambda: page.get_by_role("heading", name="Importadores").click(), log_fn)
    await reintentar_accion(
        f"esperar pestana {nombre_tab}",
        lambda: page.get_by_role("tab", name=nombre_tab).wait_for(state="visible", timeout=15000),
        log_fn,
    )
    log_fn("âœ“ Dentro de Importadores")

    await reintentar_accion(f"click pestana {nombre_tab}", lambda: page.get_by_role("tab", name=nombre_tab).click(), log_fn)


async def subir_archivo_mercadohouse(page, ruta_archivo: str, nombre_archivo: str, boton_carga: str, log_fn) -> None:
    """Adjunta archivo y presiona el boton de carga en Mercadohouse."""
    await reintentar_accion(
        "esperar selector de archivo",
        lambda: page.locator('input[type="file"]').wait_for(state="attached", timeout=15000),
        log_fn,
    )
    log_fn(f"Subiendo archivo: {nombre_archivo}")
    file_input = page.locator('input[type="file"]')
    await reintentar_accion("adjuntar archivo", lambda: file_input.set_input_files(ruta_archivo), log_fn)
    await pausa_corta(0.3)
    log_fn("âœ“ Archivo cargado")

    log_fn(f"Haciendo clic en '{boton_carga}'...")
    await reintentar_accion(f"click {boton_carga}", lambda: page.get_by_role("button", name=boton_carga).click(), log_fn)
    await esperar_carga_ligera(page)
    await pausa_corta(1)


async def esperar_confirmacion_carga_mercadohouse(page, log_fn, timeout: int = 180000) -> str:
    """Espera un toast verde de exito; falla si aparece toast rojo."""
    log_fn("Esperando aviso verde de Mercadohouse...")
    try:
        limite = asyncio.get_event_loop().time() + (timeout / 1000)
        while asyncio.get_event_loop().time() < limite:
            toast = await page.evaluate(
                """
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    const parseColor = (color) => {
                        const m = /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(color || '');
                        return m ? m.slice(1).map(Number) : null;
                    };
                    const esVerde = (color) => {
                        const rgb = parseColor(color);
                        if (!rgb) return false;
                        const [r, g, b] = rgb;
                        return g >= 100 && g > r * 1.15 && g > b * 1.15;
                    };
                    const esRojo = (color) => {
                        const rgb = parseColor(color);
                        if (!rgb) return false;
                        const [r, g, b] = rgb;
                        return r >= 150 && r > g * 1.2 && r > b * 1.2;
                    };
                    const candidatos = Array.from(document.querySelectorAll(
                        '[role="alert"], .Toastify__toast, .toast, .MuiAlert-root, .SnackbarItem-message, .notistack-Snackbar, .snackbar, .alert, div'
                    )).filter((el) => {
                        if (!visible(el)) return false;
                        const rect = el.getBoundingClientRect();
                        if (rect.top > 180) return false;
                        if (rect.width < 220 || rect.height < 35) return false;
                        const texto = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (texto.length === 0 || texto.length > 300) return false;
                        if (/cerrar sesi[oó]n|hola,|\\bdb\\b|\\bredis\\b|productos por vencer/i.test(texto)) return false;
                        return true;
                    });
                    for (const el of candidatos) {
                        const texto = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        const s = window.getComputedStyle(el);
                        const coloresContenedor = [s.backgroundColor, s.borderColor];
                        if (coloresContenedor.some(esRojo)) return { estado: 'error', texto };
                        if (coloresContenedor.some(esVerde)) return { estado: 'ok', texto };
                    }
                    return null;
                }
                """
            )
            if toast:
                mensaje = toast.get("texto") or "Aviso de Mercadohouse sin texto"
                if toast.get("estado") == "error":
                    raise Exception(f"Mercadohouse mostro aviso rojo: {mensaje}")
                log_fn(f"Confirmacion Mercadohouse: {mensaje}")
                await pausa_corta(2)
                await esperar_carga_ligera(page, timeout=15000)
                return mensaje
            await pausa_corta(1)
        raise Exception("No aparecio aviso verde de confirmacion")
    except Exception as e:
        raise Exception(f"Mercadohouse no confirmo la carga: {e}")


# ── Login helpers ─────────────────────────────────────────────────────────────

class ErrorCredenciales(Exception):
    def __init__(self, servicio):
        self.servicio = servicio
        super().__init__(f"Contraseña incorrecta para {servicio}")


async def _contrasena_rechazada(page):
    """Reconoce mensajes explícitos de credenciales incorrectas."""
    try:
        texto = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    patrones = (
        r"(?:contrasena|clave|password|credenciales).{0,55}(?:incorrect|invalid|errone|equivocad)",
        r"(?:contrasena|clave|password|credenciales).{0,55}(?:no (?:son|es) correct|no (?:son|es) valid|no coinciden)",
        r"(?:incorrect|invalid|errone|equivocad).{0,55}(?:contrasena|clave|password|credenciales)",
        r"(?:usuario|correo|email).{0,55}(?:contrasena|clave|password).{0,55}(?:incorrect|invalid)",
        r"(?:invalid|incorrect) credentials",
        r"datos de acceso.{0,55}incorrect",
    )
    return any(re.search(patron, texto) for patron in patrones)


async def login_tivendo(page, email: str, password: str, log_fn) -> None:
    """Login en tivendoapp.defontana.com. Lanza Exception si falla."""
    log_fn("Abriendo login de Tivendo...")
    await page.goto("https://tivendoapp.defontana.com/login", timeout=30000)
    try:
        await page.wait_for_selector("input", state="visible", timeout=10000)
    except Exception:
        await esperar_carga_ligera(page)
        if "login" not in page.url.lower():
            log_fn("Sesion Tivendo reutilizada")
            await guardar_sesion_contexto(page.context, log_fn)
            return
        await page.wait_for_selector("input", state="visible", timeout=20000)
    log_fn("Ingresando credenciales Tivendo...")
    inputs = page.locator("input")
    await inputs.nth(0).click()
    await inputs.nth(0).press_sequentially(email, delay=20)
    await pausa_corta()
    await inputs.nth(1).click()
    await inputs.nth(1).press_sequentially(password, delay=20)
    await pausa_corta()
    await page.get_by_role("button", name="Iniciar Sesión").click()
    log_fn("Esperando redirección...")
    try:
        await page.wait_for_url(lambda url: "login" not in url.lower(), timeout=20000)
    except Exception:
        pass
    await esperar_carga_ligera(page)
    if "login" in page.url.lower():
        if await _contrasena_rechazada(page):
            raise ErrorCredenciales("Tivendo")
        raise Exception("Login Tivendo fallido")
    await guardar_sesion_contexto(page.context, log_fn)
    log_fn("✓ Login Tivendo exitoso")


async def login_mercadohouse(page, email: str, password: str, log_fn) -> None:
    """Login en mercadohouse.cl. Lanza Exception si falla."""
    log_fn("Abriendo login de Mercadohouse...")
    await page.goto("https://mercadohouse.cl/login", timeout=30000)
    try:
        await page.wait_for_selector("input", state="visible", timeout=8000)
    except Exception:
        await esperar_carga_ligera(page)
        if "login" not in page.url.lower():
            log_fn("Sesion Mercadohouse reutilizada")
            await guardar_sesion_contexto(page.context, log_fn)
            return
        await page.wait_for_selector("input", state="visible", timeout=15000)
    log_fn("Ingresando credenciales Mercadohouse...")
    inputs = page.locator("input")
    await inputs.nth(0).click()
    await inputs.nth(0).fill("")
    await inputs.nth(0).press_sequentially(email, delay=20)
    await pausa_corta()
    await inputs.nth(1).click()
    await inputs.nth(1).fill("")
    await inputs.nth(1).press_sequentially(password, delay=20)
    await pausa_corta()
    botones_login = ("ENTRAR", "Entrar", "Iniciar sesion", "Iniciar sesión", "Iniciar Sesión", "Ingresar")
    ultimo_error = None
    for boton in botones_login:
        try:
            await page.get_by_role("button", name=boton).click(timeout=3000)
            break
        except Exception as exc:
            ultimo_error = exc
    else:
        raise ultimo_error
    try:
        await page.wait_for_url(lambda url: "login" not in url.lower(), timeout=20000)
    except Exception:
        pass
    await esperar_carga_ligera(page)
    if "login" in page.url.lower():
        if await _contrasena_rechazada(page):
            raise ErrorCredenciales("Mercadohouse")
        try:
            texto = (await page.locator("body").inner_text(timeout=3000)).strip()
            resumen = " ".join(texto.split())[:220]
        except Exception:
            resumen = ""
        detalle = f": {resumen}" if resumen else ""
        raise Exception(f"Login Mercadohouse fallido{detalle}")
    await guardar_sesion_contexto(page.context, log_fn)
    log_fn("✓ Login Mercadohouse exitoso")


# ── Verificación de empresa ───────────────────────────────────────────────────

async def asegurar_empresa_mercadohouse(page, log_fn,
                                        tivendo_email=None,
                                        tivendo_password=None) -> None:
    """
    Verifica que la empresa activa en Defontana sea MERCADO HOUSE SPA.

    Si está en "NO USAR":
      1. Hace clic en el selector de empresa (flecha ▼ en el header)
      2. Selecciona MERCADO HOUSE SPA
      3. Espera la recarga automática de la página
      4. Si la recarga pidió login de nuevo (pasa en modo incógnito),
         vuelve a loguearse con tivendo_email / tivendo_password
      5. Verifica que el header ahora diga MERCADO HOUSE

    Parámetros opcionales:
      tivendo_email    — email de Tivendo (para re-login si la sesión cae)
      tivendo_password — contraseña de Tivendo
    """
    log_fn("Verificando empresa activa en Defontana...")

    # ── Helpers internos ──────────────────────────────────────────────────────

    async def _leer_empresa() -> str:
        """Lee el texto del selector de empresa en el header."""
        for texto in ("MERCADO HOUSE SPA", "MERCADO HOUSE"):
            try:
                loc = page.get_by_text(texto, exact=False).first
                await loc.wait_for(state="visible", timeout=1200)
                txt = (await loc.inner_text()).strip().upper()
                if "MERCADO HOUSE" in txt:
                    return txt
            except Exception:
                continue

        SELECTORES = [
            "header p:has-text('MERCADO HOUSE')",
            "header p:has-text('NO USAR')",
            "header span:has-text('MERCADO HOUSE')",
            "header span:has-text('NO USAR')",
            "header div:has-text('MERCADO HOUSE')",
            "header div:has-text('NO USAR')",
            "header [class*='company']",
            "header [class*='empresa']",
        ]
        for sel in SELECTORES:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=900)
                txt = (await loc.inner_text()).strip().upper()
                if "MERCADO HOUSE" in txt or "NO USAR" in txt:
                    return txt
            except Exception:
                continue
        # Fallback más amplio
        for texto in ["MERCADO HOUSE", "NO USAR"]:
            try:
                loc = page.locator(f"text={texto}").first
                if await loc.count() > 0:
                    txt = (await loc.inner_text()).strip().upper()
                    if txt:
                        return txt
            except Exception:
                continue
        return ""

    async def _reloguear_tivendo() -> None:
        """Re-hace el login en Tivendo (para cuando la recarga de empresa lo cierra)."""
        if not tivendo_email or not tivendo_password:
            raise Exception(
                "La sesión se cerró tras cambiar de empresa y no hay credenciales "
                "para volver a loguearse. Pasa tivendo_email y tivendo_password a "
                "asegurar_empresa_mercadohouse()."
            )
        log_fn("   Sesión cerrada tras cambio de empresa → re-logueando en Tivendo...")
        await login_tivendo(page, tivendo_email, tivendo_password, log_fn)

    # ── 1. Leer empresa activa ────────────────────────────────────────────────
    nombre_actual = await _leer_empresa()

    if not nombre_actual:
        log_fn("⚠️  No se encontró el selector de empresa en el header. Continuando de todas formas...")
        return

    log_fn(f"   Empresa activa: '{nombre_actual}'")

    if EMPRESA_CORRECTA in nombre_actual:
        log_fn(f"✓ Empresa correcta: {nombre_actual}")
        return

    # ── 2. Empresa incorrecta → abrir el selector (clic en flecha ▼) ─────────
    log_fn(f"⚠️  Empresa incorrecta ('{nombre_actual}'). Cambiando a MERCADO HOUSE SPA...")

    clic_hecho = False
    for selector_clic in [
        "header p:has-text('NO USAR')",
        "header span:has-text('NO USAR')",
        "header div:has-text('NO USAR')",
        "text=NO USAR",
    ]:
        try:
            loc = page.locator(selector_clic).first
            if await loc.count() > 0:
                await loc.click()
                clic_hecho = True
                break
        except Exception:
            continue

    if not clic_hecho:
        raise Exception("No se pudo hacer clic en el selector de empresa en el header de Defontana.")

    await pausa_corta(0.3)

    # ── 3. Esperar el panel "Empresas" y seleccionar MERCADO HOUSE SPA ────────
    log_fn("   Panel 'Empresas' abierto. Seleccionando MERCADO HOUSE SPA...")
    try:
        await page.wait_for_selector("text=MERCADO HOUSE SPA", state="visible", timeout=10000)
    except Exception:
        try:
            await page.wait_for_selector("text=MERCADO HOUSE", state="visible", timeout=10000)
        except Exception:
            raise Exception("No apareció el panel 'Empresas' después de hacer clic en el selector.")

    clic_ok = False
    for fn in [
        lambda: page.get_by_text("MERCADO HOUSE SPA", exact=True).click(),
        lambda: page.locator("text=MERCADO HOUSE SPA").first.click(),
        lambda: page.locator("text=MERCADO HOUSE").last.click(),
    ]:
        try:
            await fn()
            clic_ok = True
            break
        except Exception:
            continue

    if not clic_ok:
        raise Exception("No se pudo hacer clic en 'MERCADO HOUSE SPA' en el panel de empresas.")

    log_fn("   MERCADO HOUSE SPA seleccionada. Esperando recarga de página...")

    # ── 4. Esperar recarga y manejar posible re-login ─────────────────────────
    try:
        await esperar_carga_ligera(page, timeout=20000)
    except Exception:
        pass
    await pausa_corta(0.5)

    # Si el modo incógnito cerró la sesión, la recarga lleva al login
    if "login" in page.url.lower():
        await _reloguear_tivendo()

    # ── 5. Verificar que ahora dice MERCADO HOUSE ─────────────────────────────
    nombre_nuevo = await _leer_empresa()

    if EMPRESA_CORRECTA in nombre_nuevo:
        log_fn(f"✓ Empresa cambiada correctamente a: {nombre_nuevo}")
    else:
        raise Exception(
            f"No se pudo confirmar el cambio de empresa. "
            f"Empresa activa tras el cambio: '{nombre_nuevo}'. "
            "Revisa manualmente el portal Defontana."
        )
