"""Notificaciones nocturnas mediante una sesion persistente de WhatsApp Web."""

import asyncio
import json
from datetime import datetime

from playwright.async_api import async_playwright

from app_paths import (
    DIAG_DIR,
    WHATSAPP_CONFIG_FILE,
    WHATSAPP_PROFILE_DIR,
    runtime_path,
)
from utils import crear_logger


LOG_FILE = runtime_path("log_whatsapp.txt")
log = crear_logger(LOG_FILE)
WHATSAPP_URL = "https://web.whatsapp.com/"


def cargar_configuracion() -> dict:
    if not WHATSAPP_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(WHATSAPP_CONFIG_FILE.read_text(encoding="utf-8"))
        grupo = str(data.get("grupo", "")).strip()
        return {"grupo": grupo} if grupo else {}
    except Exception:
        return {}


def guardar_configuracion(grupo: str) -> None:
    WHATSAPP_CONFIG_FILE.write_text(
        json.dumps({"grupo": grupo}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _abrir_contexto_persistente(playwright, visible: bool):
    opciones = {
        "user_data_dir": str(WHATSAPP_PROFILE_DIR),
        "headless": not visible,
        "viewport": {"width": 1280, "height": 850},
        "args": ["--disable-notifications"],
    }
    ultimo_error = None
    for channel in ("msedge", "chrome"):
        try:
            return await playwright.chromium.launch_persistent_context(
                channel=channel,
                **opciones,
            )
        except Exception as exc:
            ultimo_error = exc
    raise RuntimeError("No se pudo abrir Edge ni Chrome para WhatsApp Web") from ultimo_error


async def _esperar_sesion(page, timeout: int) -> None:
    selectores = (
        "#pane-side",
        '[aria-label="Chat list"]',
        '[data-testid="chat-list"]',
    )
    limite = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < limite:
        for selector in selectores:
            try:
                if await page.locator(selector).count() > 0:
                    return
            except Exception:
                pass
        await asyncio.sleep(1)
    raise RuntimeError(
        "La sesion de WhatsApp no esta iniciada. Abre la opcion 14 y escanea el QR nuevamente."
    )


async def _nombre_chat_abierto(page) -> str:
    textos = []
    selectores = (
        'header [data-testid="conversation-info-header-chat-title"]',
        'header span[dir="auto"]',
        '#pane-side [aria-selected="true"] span[title]',
    )
    for selector in selectores:
        candidatos = page.locator(selector)
        for indice in range(await candidatos.count()):
            candidato = candidatos.nth(indice)
            texto = (
                (await candidato.get_attribute("title") or "")
                or (await candidato.inner_text() or "")
            ).strip()
            if texto and texto not in textos:
                textos.append(texto)

    for texto in textos:
        if not _parece_lista_participantes(texto):
            return texto
    raise RuntimeError("No se pudo reconocer con seguridad el nombre del grupo abierto")


def _parece_lista_participantes(texto: str) -> bool:
    normalizado = texto.lower()
    return (
        texto.count(",") >= 2
        or texto.count("+56") >= 2
        or (", tú" in normalizado)
        or (", tu" in normalizado)
    )


async def _buscar_y_abrir_grupo(page, grupo: str) -> None:
    buscadores = (
        'div[contenteditable="true"][data-tab="3"]',
        '[aria-label*="Search"]',
        '[aria-label*="Buscar"]',
    )
    buscador = None
    for selector in buscadores:
        locator = page.locator(selector).first
        try:
            if await locator.count() > 0 and await locator.is_visible():
                buscador = locator
                break
        except Exception:
            pass
    if buscador is None:
        raise RuntimeError("No se encontro el buscador de chats de WhatsApp")

    await buscador.click()
    await buscador.press("Control+A")
    await buscador.fill(grupo)
    await asyncio.sleep(2)

    chat = page.get_by_title(grupo, exact=True).first
    if await chat.count() == 0:
        raise RuntimeError(f"No se encontro el grupo configurado: {grupo}")
    await chat.click()
    await asyncio.sleep(1)


async def _enviar_en_chat_abierto(page, mensaje: str) -> None:
    cajas = (
        'footer div[contenteditable="true"]',
        'div[contenteditable="true"][data-tab="10"]',
        'div[contenteditable="true"][data-tab="11"]',
    )
    caja = None
    for selector in cajas:
        locator = page.locator(selector).last
        try:
            if await locator.count() > 0 and await locator.is_visible():
                caja = locator
                break
        except Exception:
            pass
    if caja is None:
        raise RuntimeError("No se encontro la caja para escribir el mensaje")

    await caja.click()
    lineas = mensaje.splitlines()
    for indice, linea in enumerate(lineas):
        if linea:
            await caja.press_sequentially(linea, delay=4)
        if indice < len(lineas) - 1:
            await caja.press("Shift+Enter")
    await caja.press("Enter")
    await page.wait_for_function(
        "(elemento) => !(elemento.innerText || elemento.textContent || '').trim()",
        arg=await caja.element_handle(),
        timeout=15000,
    )
    await asyncio.sleep(1)


async def configurar_whatsapp() -> bool:
    """Abre WhatsApp visible, guarda el grupo abierto y envia una prueba."""
    async with async_playwright() as playwright:
        context = await _abrir_contexto_persistente(playwright, visible=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(WHATSAPP_URL, wait_until="domcontentloaded", timeout=60000)

            print()
            print("  WhatsApp Web se abrio en el navegador.")
            print("  Si aparece un QR, escanealo con el telefono de Sistemas.")
            print("  Luego abre el grupo que recibira las notificaciones.")
            await _esperar_sesion(page, timeout=300)
            input("  Cuando el grupo este abierto, presiona ENTER aqui...")

            try:
                grupo = await _nombre_chat_abierto(page)
                print(f"  Grupo detectado: {grupo}")
            except Exception:
                print("  WhatsApp no mostro claramente el nombre del grupo.")
                grupo = input("  Escribe el nombre exacto del grupo abierto: ").strip()
                if not grupo or _parece_lista_participantes(grupo):
                    raise RuntimeError("El nombre ingresado no parece un nombre de grupo valido")

            confirmar = input("  Guardar este grupo y enviar mensaje de prueba? (s/n): ").strip().lower()
            if confirmar != "s":
                print("  Configuracion cancelada.")
                return False

            mensaje = (
                "✅ NOTIFICACIONES ACTIVADAS\n"
                "MercadohouseSync quedo configurado para informar aqui "
                "el resultado de las ejecuciones nocturnas."
            )
            await _enviar_en_chat_abierto(page, mensaje)
            guardar_configuracion(grupo)
            print("  Mensaje de prueba enviado correctamente.")
            return True
        finally:
            await context.close()


async def enviar_notificacion(mensaje: str, intentos: int = 3, espera: int = 20) -> bool:
    """Envia un mensaje al grupo configurado. Nunca propaga el error al sync."""
    config = cargar_configuracion()
    grupo = config.get("grupo")
    if not grupo:
        log("WhatsApp no configurado; usa la opcion 14 del menu.")
        return False

    for intento in range(1, intentos + 1):
        context = None
        try:
            log(f"Enviando notificacion a '{grupo}' (intento {intento}/{intentos})...")
            async with async_playwright() as playwright:
                context = await _abrir_contexto_persistente(playwright, visible=False)
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(WHATSAPP_URL, wait_until="domcontentloaded", timeout=60000)
                await _esperar_sesion(page, timeout=45)
                await _buscar_y_abrir_grupo(page, grupo)
                await _enviar_en_chat_abierto(page, mensaje)
                await context.close()
                log("Notificacion de WhatsApp enviada correctamente.")
                return True
        except Exception as exc:
            log(f"Fallo notificacion WhatsApp: {exc}")
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if intento < intentos:
                await asyncio.sleep(espera)

    diagnostico = DIAG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_whatsapp_error.txt"
    diagnostico.write_text(
        "No se pudo enviar la notificacion de WhatsApp despues de 3 intentos.\n"
        "Revisa la opcion 14 y vuelve a vincular la sesion si es necesario.\n",
        encoding="utf-8",
    )
    log(f"Diagnostico WhatsApp guardado: {diagnostico}")
    return False


def construir_mensaje(resumen: dict) -> str:
    correcto = bool(resumen.get("ok"))
    encabezado = (
        "✅ SINCRONIZACIÓN NOCTURNA CORRECTA"
        if correcto
        else "❌ FALLÓ SINCRONIZACIÓN NOCTURNA"
    )
    lineas = [
        encabezado,
        f"Sucursal: {resumen.get('sucursal', 'Sin información')}",
    ]

    precios = resumen.get("precios") or {}
    if precios:
        lineas.append(
            f"Precios: {precios.get('ok', 0)} de {precios.get('total', 0)}"
        )

    etiquetas = {
        "Sync Artículos Tivendo POS → MH": "Artículos",
        "Sync Packs     Tivendo POS → MH": "Packs",
        "Sync Precios   Tivendo ERP → MH": "Mercadohouse",
    }
    for paso in resumen.get("pasos", []):
        etiqueta = etiquetas.get(paso.get("nombre"))
        if etiqueta:
            lineas.append(f"{etiqueta}: {'OK' if paso.get('ok') else 'FALLÓ'}")

    fallidos = precios.get("fallidos") or []
    if fallidos:
        lineas.append("Fallidos:")
        lineas.extend(str(codigo) for codigo in fallidos)

    if not correcto and resumen.get("etapa_fallida"):
        lineas.append(f"Etapa: {resumen['etapa_fallida']}")
    lineas.append(f"Duración: {resumen.get('duracion', '0s')}")
    lineas.append(f"Hora: {resumen.get('hora_fin', '')}")
    return "\n".join(lineas)
