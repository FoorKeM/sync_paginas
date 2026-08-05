"""
utils.py — Helpers compartidos para todos los scripts de sincronización.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime
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

    def __init__(self, log_fn):
        self.log_fn = log_fn
        self.resultados = []
        self.etapa_actual = ""
        self.ultima_etapa = ""

    def etapa(self, nombre: str):
        return _EtapaMedida(self, nombre)

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


class _EtapaMedida:
    def __init__(self, medidor: MedidorEtapas, nombre: str):
        self.medidor = medidor
        self.nombre = nombre
        self.inicio = None

    def __enter__(self):
        self.inicio = time.perf_counter()
        self.medidor.etapa_actual = self.nombre
        self.medidor.ultima_etapa = self.nombre
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
    try:
        async with context.expect_page(timeout=timeout) as page_info:
            await reintentar_accion("click que abre modulo", locator.click)
        nueva = await page_info.value
        await esperar_carga_ligera(nueva)
        return nueva, True
    except PlaywrightTimeoutError:
        await esperar_carga_ligera(page)
        return page, False


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
