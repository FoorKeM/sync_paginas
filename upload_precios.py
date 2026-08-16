"""
SUBIDOR AUTOMÁTICO DE PRECIOS A TIVENDO
Flujo completo:
  1. Login en tivendoapp.defontana.com/login
  2. Verifica empresa MERCADO HOUSE SPA (auto-corrige si está en NO USAR)
  3. Clic en "Punto de Ventas"
  4. Navega a Configuración > Listas de precios
  5. Abre menú de la lista → "Importar precios"
  6. Sube el Excel más reciente → "Leer fichero" → "Guardar"
"""

import asyncio
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from app_paths import runtime_path, configure_playwright_browsers
configure_playwright_browsers()
from playwright.async_api import async_playwright
import config as _cfg
from utils import (
    asegurar_empresa_mercadohouse,
    asegurar_punto_ventas_abierto,
    click_y_capturar_pagina,
    crear_logger,
    crear_pagina_trabajo,
    esperar_carga_ligera,
    guardar_diagnostico,
    login_tivendo,
    MedidorEtapas,
    pausa_corta,
    rotar_log,
)
from credenciales import TIVENDO_EMAIL as EMAIL, TIVENDO_PASSWORD as PASSWORD

# ============================================================
#  CONFIGURACIÓN — edita credenciales en credenciales.py
# ============================================================

# Carpeta donde guardas los Excel de precios cada día.
# El script toma AUTOMÁTICAMENTE el archivo más reciente.
CARPETA_EXCEL = r"C:\Precios"

# True = ver el navegador, False = correr invisible en segundo plano
MOSTRAR_NAVEGADOR = False

# Ruta al Excel a subir; si está seteada, omite la búsqueda automática.
# La setea menu.py cuando el usuario elige el archivo en la opción 7.
EXCEL_FORZADO: str | None = None
# ============================================================

# Código de salida especial: el Excel no tiene el formato que espera Tivendo.
# No tiene sentido reintentar (el archivo seguirá mal), así que menu.py no debe reintentar con este código.
CODIGO_SALIDA_FORMATO_INVALIDO = 2

LOG_FILE = runtime_path("log_subida.txt")
log = crear_logger(LOG_FILE)
ULTIMO_RESULTADO_PRECIOS: dict | None = None
ULTIMO_EXCEL_USADO: str | None = None
ULTIMOS_PRECIOS_CONFIRMADOS: dict[str, int] = {}


def _col_letras_a_indice(letras: str) -> int:
    """Convierte la letra de columna de Excel a índice base 0: 'A' -> 0, 'B' -> 1, 'AA' -> 26."""
    idx = 0
    for ch in letras.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def resumen_excel_precios(excel_path: Path) -> tuple[int, list[list[str]]]:
    """Lee todas las filas del xlsx sin depender de librerias externas.

    Excel omite las celdas vacías del XML, así que hay que ubicar cada celda
    por su columna real (atributo 'r', ej. 'C5') en vez de por su posición en
    la lista — si no, una celda vacía al inicio de la fila desalinea todo el resto.
    """
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(excel_path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("main:si", ns):
                partes = [t.text or "" for t in si.findall(".//main:t", ns)]
                shared.append("".join(partes))

        sheet_name = "xl/worksheets/sheet1.xml"
        root = ET.fromstring(zf.read(sheet_name))
        filas = root.findall(".//main:sheetData/main:row", ns)
        datos = []
        for row in filas:
            celdas: dict[int, str] = {}
            col_max = -1
            for cell in row.findall("main:c", ns):
                col_letras = "".join(ch for ch in cell.attrib.get("r", "") if ch.isalpha())
                col_idx = _col_letras_a_indice(col_letras) if col_letras else col_max + 1
                tipo = cell.attrib.get("t")
                if tipo == "inlineStr":
                    is_el = cell.find("main:is", ns)
                    texto = "" if is_el is None else "".join(t.text or "" for t in is_el.findall(".//main:t", ns))
                else:
                    value = cell.find("main:v", ns)
                    texto = "" if value is None or value.text is None else value.text
                    if tipo == "s" and texto.isdigit():
                        idx = int(texto)
                        texto = shared[idx] if idx < len(shared) else texto
                celdas[col_idx] = texto.strip() if isinstance(texto, str) else texto
                col_max = max(col_max, col_idx)
            datos.append([celdas.get(i, "") for i in range(col_max + 1)])
        return max(0, len(datos) - 1), datos


def _valor_fila(fila: list[str], indice: int) -> str:
    return fila[indice].strip() if indice < len(fila) and fila[indice] is not None else ""


def describir_cambio_precio(fila: list[str]) -> str:
    codigo = _valor_fila(fila, 0)
    rango_ini_1 = _valor_fila(fila, 1)
    rango_fin_1 = _valor_fila(fila, 2)
    precio_1 = _valor_fila(fila, 3)
    rango_ini_2 = _valor_fila(fila, 4)
    rango_fin_2 = _valor_fila(fila, 5)
    precio_2 = _valor_fila(fila, 6)

    partes = [f"{codigo} | Rango 1: {rango_ini_1}-{rango_fin_1} | Precio 1: {precio_1}"]
    if rango_ini_2 or rango_fin_2 or precio_2:
        partes.append(f"Rango 2: {rango_ini_2}-{rango_fin_2} | Precio 2: {precio_2}")
    return " | ".join(partes)


ENCABEZADO_ESPERADO = ["CODIGO", "RANGOINICIAL1", "RANGOFINAL1", "PRECIO1", "RANGOINICIAL2", "RANGOFINAL2", "PRECIO2"]


def _normalizar_encabezado(texto: str) -> str:
    """Quita tildes/espacios para comparar encabezados sin depender de la codificación exacta."""
    import unicodedata
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]", "", sin_tildes.upper())


def validar_encabezado_precios(filas_excel: list[list[str]]) -> list[str]:
    """Verifica que la primera fila tenga los encabezados que espera Tivendo.

    Un encabezado corrupto (ej. 'C—digo' en vez de 'Código', típico de un
    problema de codificación al generar el archivo) puede hacer que Tivendo
    no reconozca la columna y falle o cargue datos en el campo equivocado.
    """
    if not filas_excel:
        return ["El archivo está vacío, no tiene ni encabezado"]

    encabezado = filas_excel[0]
    errores = []
    for col, esperado in enumerate(ENCABEZADO_ESPERADO):
        valor_crudo = _valor_fila(encabezado, col)
        if _normalizar_encabezado(valor_crudo) != esperado:
            errores.append(
                f"Encabezado columna {col + 1}: dice {valor_crudo!r}, "
                f"debería decir '{describir_encabezado(esperado)}' — revisa cómo se genera el archivo"
            )
    return errores


ENCABEZADOS_LEGIBLES = {
    "CODIGO": "Código",
    "RANGOINICIAL1": "RangoInicial1",
    "RANGOFINAL1": "RangoFinal1",
    "PRECIO1": "Precio1",
    "RANGOINICIAL2": "RangoInicial2",
    "RANGOFINAL2": "RangoFinal2",
    "PRECIO2": "Precio2",
}


def describir_encabezado(clave: str) -> str:
    return ENCABEZADOS_LEGIBLES.get(clave, clave)


def _indice_a_col_letras(idx: int) -> str:
    """Inverso de _col_letras_a_indice: 0 -> 'A', 1 -> 'B', 26 -> 'AA'."""
    idx += 1
    letras = ""
    while idx > 0:
        idx, resto = divmod(idx - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def corregir_encabezado_excel(excel_path: Path, encabezado_actual: list[str]) -> bool:
    """Corrige en el mismo archivo las celdas del encabezado que no coincidan con lo
    que espera Tivendo (ej. un símbolo raro en vez de 'Código' por un problema de
    codificación al generar el archivo). Solo toca esas celdas puntuales de la fila 1,
    nunca las filas de datos. Devuelve True si corrigió algo.
    """
    referencias_a_corregir: dict[str, str] = {}
    for col_idx, esperado in enumerate(ENCABEZADO_ESPERADO):
        actual = encabezado_actual[col_idx] if col_idx < len(encabezado_actual) else ""
        if _normalizar_encabezado(actual) != esperado:
            ref = f"{_indice_a_col_letras(col_idx)}1"
            referencias_a_corregir[ref] = describir_encabezado(esperado)

    if not referencias_a_corregir:
        return False

    sheet_name = "xl/worksheets/sheet1.xml"
    with zipfile.ZipFile(excel_path) as zf:
        sheet_xml = zf.read(sheet_name).decode("utf-8")

    for ref, texto_nuevo in referencias_a_corregir.items():
        patron = re.compile(
            rf'<c\s+[^>]*?r="{ref}"[^>]*?/>|<c\s+[^>]*?r="{ref}"[^>]*?>.*?</c>'
        )
        reemplazo = f'<c r="{ref}" t="inlineStr"><is><t>{texto_nuevo}</t></is></c>'
        sheet_xml, encontrado = patron.subn(reemplazo, sheet_xml, count=1)
        if not encontrado:
            return False  # no se encontró la celda tal cual se esperaba; no arriesgar una reescritura parcial

    tmp_path = excel_path.with_name(f".{excel_path.stem}_tmp{excel_path.suffix}")
    with zipfile.ZipFile(excel_path) as zf_in, zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for item in zf_in.infolist():
            datos = zf_in.read(item.filename)
            if item.filename == sheet_name:
                datos = sheet_xml.encode("utf-8")
            zf_out.writestr(item, datos)
    os.replace(tmp_path, excel_path)
    return True


def _es_numero(texto: str) -> bool:
    """Acepta enteros y decimales (ej. '0.001' para productos que se venden por kilo)."""
    try:
        float(texto)
        return True
    except (TypeError, ValueError):
        return False


def validar_estructura_precios(filas_excel: list[list[str]]) -> list[str]:
    """Verifica que el Excel siga el formato que espera Tivendo:
    codigo, rangoInicial1, rangoFinal1, precio1, [rangoInicial2, rangoFinal2, precio2].
    Los rangos aceptan decimales (productos por kilo, ej. 0.001-1000).
    Devuelve una lista de errores; vacía si el archivo está bien hecho.
    """
    errores: list[str] = []
    codigos_vistos: set[str] = set()

    for idx, fila in enumerate(filas_excel[1:], start=1):
        codigo = _valor_fila(fila, 0).lstrip("'").strip().upper()
        rango_ini_1 = _valor_fila(fila, 1)
        rango_fin_1 = _valor_fila(fila, 2)
        precio_1 = _valor_fila(fila, 3)
        rango_ini_2 = _valor_fila(fila, 4)
        rango_fin_2 = _valor_fila(fila, 5)
        precio_2 = _valor_fila(fila, 6)
        if not codigo:
            continue  # fila sin código (leyenda/basura): Tivendo la ignora, no se valida

        etiqueta = f"Fila {idx} ({codigo})"

        if not re.match(r"^[A-Z]\d+$", codigo):
            errores.append(f"{etiqueta}: código con formato inválido, debe ser una letra seguida de números (ej. A001696 artículo, P002281 pack)")
            continue
        if codigo in codigos_vistos:
            errores.append(f"{etiqueta}: código repetido en el archivo")
        codigos_vistos.add(codigo)

        if not (_es_numero(rango_ini_1) and _es_numero(rango_fin_1)):
            errores.append(f"{etiqueta}: Rango 1 no es numérico ({rango_ini_1}-{rango_fin_1})")
        elif float(rango_ini_1) > float(rango_fin_1):
            errores.append(f"{etiqueta}: Rango 1 invertido ({rango_ini_1}-{rango_fin_1})")

        if not _es_numero(precio_1) or float(precio_1) <= 0:
            errores.append(f"{etiqueta}: Precio 1 inválido ({precio_1!r})")

        tiene_rango_2 = any([rango_ini_2, rango_fin_2, precio_2])
        if tiene_rango_2:
            if not (_es_numero(rango_ini_2) and _es_numero(rango_fin_2)):
                errores.append(f"{etiqueta}: Rango 2 no es numérico ({rango_ini_2}-{rango_fin_2})")
            elif float(rango_ini_2) > float(rango_fin_2):
                errores.append(f"{etiqueta}: Rango 2 invertido ({rango_ini_2}-{rango_fin_2})")
            elif _es_numero(rango_fin_1) and float(rango_ini_2) <= float(rango_fin_1):
                errores.append(f"{etiqueta}: Rango 2 se superpone con Rango 1")
            if not _es_numero(precio_2) or float(precio_2) <= 0:
                errores.append(f"{etiqueta}: Precio 2 inválido ({precio_2!r})")

    return errores


def borrar_excel_usado(excel_path: Path) -> None:
    try:
        if excel_path.exists():
            excel_path.unlink()
            log(f"🗑️  Archivo eliminado: {excel_path.name}")
        else:
            log(f"Archivo ya no existe para borrar: {excel_path.name}")
    except Exception as _e:
        log(f"⚠️  No se pudo eliminar el archivo: {_e}")


async def esperar_guardado_tivendo(page, log_fn, timeout: int = 60000) -> None:
    """Espera a que Tivendo termine de guardar antes de cerrar el navegador."""
    log_fn("Esperando confirmación de guardado en Tivendo...")
    limite = asyncio.get_event_loop().time() + (timeout / 1000)
    modal_cerrado_desde = None
    textos_ok = ("guardado", "importado", "actualizado", "correctamente", "éxito", "exito")
    textos_error = ("error", "inválido", "invalido", "no se pudo", "falló", "fallo")

    while asyncio.get_event_loop().time() < limite:
        try:
            texto = (await page.locator("body").inner_text(timeout=1000)).lower()
        except Exception:
            texto = ""

        if any(t in texto for t in textos_error):
            raise Exception("Tivendo mostró un mensaje de error al guardar precios.")

        guardar_visible = False
        try:
            guardar_visible = await page.get_by_role("button", name="Guardar").is_visible(timeout=500)
        except Exception:
            guardar_visible = False

        if any(t in texto for t in textos_ok) and not guardar_visible:
            log_fn("✓ Tivendo confirmó el guardado de precios")
            return

        if not guardar_visible:
            if modal_cerrado_desde is None:
                modal_cerrado_desde = asyncio.get_event_loop().time()
            elif asyncio.get_event_loop().time() - modal_cerrado_desde >= 3:
                log_fn("✓ Modal de importación cerrado; guardado finalizado")
                return
        else:
            modal_cerrado_desde = None

        await pausa_corta(0.5)

    raise Exception("Tivendo no confirmó el guardado de precios dentro del tiempo esperado.")


async def _leer_pagina_resultado_importacion(page) -> list[dict]:
    return await page.evaluate(
        """
        () => {
            const normalizar = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const panelResultado = () => {
                const aceptar = Array.from(document.querySelectorAll('button'))
                    .find((b) => visible(b) && normalizar(b.innerText || b.textContent) === 'Aceptar');
                if (!aceptar) return document;
                return aceptar.closest('[role="dialog"], .mat-mdc-dialog-container, .mat-dialog-container, .cdk-overlay-pane')
                    || aceptar.closest('div')
                    || document;
            };
            const colorEsRojo = (color) => {
                const m = /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(color || '');
                if (!m) return false;
                const [r, g, b] = m.slice(1).map(Number);
                return r > 170 && g < 150 && b < 170;
            };
            const colorEsVerde = (color) => {
                const m = /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(color || '');
                if (!m) return false;
                const [r, g, b] = m.slice(1).map(Number);
                return g > 120 && r < 170;
            };
            const panel = panelResultado();
            return Array.from(panel.querySelectorAll('table tbody tr'))
                .map((row) => {
                    if (!visible(row)) return null;
                    const cells = Array.from(row.querySelectorAll('td'));
                    if (cells.length < 3) return null;
                    const code = normalizar(cells[0]?.innerText || cells[0]?.textContent);
                    const range = normalizar(cells[1]?.innerText || cells[1]?.textContent);
                    if (!/^A\\d+$/i.test(code)) return null;
                    const statusCell = cells[cells.length - 1];
                    const nodos = [statusCell, ...Array.from(statusCell.querySelectorAll('*'))];
                    const tokens = nodos.map((el) => [
                        el.innerText,
                        el.textContent,
                        el.getAttribute?.('aria-label'),
                        el.getAttribute?.('title'),
                        el.getAttribute?.('data-icon'),
                        el.className?.toString?.(),
                    ].filter(Boolean).join(' ')).join(' ').toLowerCase();
                    const html = (statusCell.innerHTML || '').toLowerCase();
                    const colores = nodos.flatMap((el) => {
                        const s = window.getComputedStyle(el);
                        return [s.color, s.backgroundColor, s.fill, s.stroke];
                    });
                    const esFallo = /\\b(close|cancel|error|fail|x)\\b|highlight_off|cancel/.test(tokens + ' ' + html)
                        || colores.some(colorEsRojo);
                    const esOk = /check|done|success|ok|check_circle/.test(tokens + ' ' + html)
                        || colores.some(colorEsVerde);
                    return { code, range, status: esFallo ? 'fallo' : (esOk ? 'ok' : 'desconocido') };
                })
                .filter((row) => row && row.code);
        }
        """
    )


async def _rango_paginador(page) -> str:
    try:
        return await page.evaluate(
            """
            () => {
                const normalizar = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0;
                };
                const aceptar = Array.from(document.querySelectorAll('button'))
                    .find((b) => visible(b) && normalizar(b.innerText || b.textContent) === 'Aceptar');
                const panel = aceptar?.closest('[role="dialog"], .mat-mdc-dialog-container, .mat-dialog-container, .cdk-overlay-pane')
                    || document;
                const texto = panel.innerText || '';
                const match = texto.match(/\\b\\d+\\s*-\\s*\\d+\\s+de\\s+\\d+\\b/);
                return match ? match[0].replace(/\\s+/g, ' ').trim() : '';
            }
            """
        )
    except Exception:
        return ""


async def _click_siguiente_resultados(page) -> bool:
    return await page.evaluate(
        """
        () => {
            const normalizar = (txt) => (txt || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const habilitado = (b) =>
                b
                && visible(b)
                && !b.disabled
                && b.getAttribute('aria-disabled') !== 'true'
                && !b.classList.contains('mat-button-disabled')
                && !b.classList.contains('mat-mdc-button-disabled');
            const aceptar = Array.from(document.querySelectorAll('button'))
                .find((b) => visible(b) && normalizar(b.innerText || b.textContent) === 'Aceptar');
            const panel = aceptar?.closest('[role="dialog"], .mat-mdc-dialog-container, .mat-dialog-container, .cdk-overlay-pane')
                || document;
            const botones = Array.from(panel.querySelectorAll('button')).filter(habilitado);
            let boton = botones.find((b) =>
                b.classList.contains('mat-mdc-paginator-navigation-next')
                || b.classList.contains('mat-paginator-navigation-next')
                || (b.getAttribute('aria-label') || '').toLowerCase().includes('next page')
                || (b.getAttribute('aria-label') || '').toLowerCase().includes('siguiente')
            );
            if (!boton) {
                const rango = Array.from(panel.querySelectorAll('*'))
                    .find((el) => visible(el) && /\\b\\d+\\s*-\\s*\\d+\\s+de\\s+\\d+\\b/.test(el.innerText || el.textContent || ''));
                if (rango) {
                    const rangoRect = rango.getBoundingClientRect();
                    boton = botones
                        .map((b) => ({ b, rect: b.getBoundingClientRect() }))
                        .filter(({ rect }) => rect.left > rangoRect.right && Math.abs(rect.top - rangoRect.top) < 45)
                        .sort((a, b) => a.rect.left - b.rect.left)[0]?.b || null;
                }
            }
            if (!boton) return false;
            boton.click();
            return true;
        }
        """
    )


async def leer_resultado_final_importacion(page, log_fn, total_esperado: int | None = None) -> dict:
    """Recorre el resultado final y devuelve conteos y codigos con problemas."""
    log_fn("Leyendo resultado final de importación...")
    await page.get_by_role("button", name="Aceptar").wait_for(state="visible", timeout=60000)

    vistos = set()
    filas: list[dict] = []
    rangos_visitados = set()

    for _ in range(30):
        await pausa_corta(0.3)
        rango = await _rango_paginador(page)
        if rango and rango in rangos_visitados:
            break
        if rango:
            rangos_visitados.add(rango)

        pagina = await _leer_pagina_resultado_importacion(page)
        for item in pagina:
            key = (item.get("code"), item.get("range"))
            if key not in vistos:
                vistos.add(key)
                filas.append(item)

        if not await _click_siguiente_resultados(page):
            break

        rango_anterior = rango
        for _espera in range(20):
            await pausa_corta(0.2)
            nuevo_rango = await _rango_paginador(page)
            if nuevo_rango != rango_anterior:
                break

    total = total_esperado or len(filas)
    fallidos = [f["code"] for f in filas if f.get("status") == "fallo"]
    ok = max(0, total - len(fallidos))

    log_fn("-" * 50)
    log_fn(f"SE INGRESARON {total} CAMBIOS DE PRECIOS")
    log_fn(f"EXITO {ok}")
    log_fn(f"FALLIDOS {len(fallidos)}")
    if fallidos:
        log_fn("CODIGOS FALLIDOS:")
        for codigo in fallidos:
            log_fn(f"  {codigo}")
    log_fn("-" * 50)

    log_fn("Cerrando resultado con 'Aceptar'...")
    await page.get_by_role("button", name="Aceptar").click()
    await esperar_carga_ligera(page)
    await pausa_corta(0.5)

    return {
        "total": total,
        "ok": ok,
        "fallidos": fallidos,
    }


def buscar_excel_mas_reciente(carpeta: str) -> Path:
    carpeta_path = Path(carpeta)
    if not carpeta_path.exists():
        log(f"ERROR: La carpeta no existe: {carpeta}")
        sys.exit(1)
    archivos = list(carpeta_path.glob("*.xlsx"))
    if not archivos:
        log(f"ERROR: No hay archivos .xlsx en: {carpeta}")
        sys.exit(1)
    archivos.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return archivos[0]


async def subir_precios():
    global ULTIMO_RESULTADO_PRECIOS, ULTIMO_EXCEL_USADO, ULTIMOS_PRECIOS_CONFIRMADOS
    ULTIMO_RESULTADO_PRECIOS = None
    ULTIMO_EXCEL_USADO = None
    ULTIMOS_PRECIOS_CONFIRMADOS = {}
    rotar_log(LOG_FILE)
    medidor = MedidorEtapas(log)

    log("=" * 50)
    log("INICIO DEL PROCESO DE SUBIDA A TIVENDO")
    suc_activa = _cfg.sucursal_activa()
    log(f"🏬 Sucursal activa   : {suc_activa['nombre']}")
    log(f"   Lista de precios : {suc_activa['tivendo_lista_erp']}")
    log("=" * 50)

    if EXCEL_FORZADO:
        excel_path = Path(EXCEL_FORZADO)
        if not excel_path.exists():
            log(f"ERROR: El archivo programado no existe: {EXCEL_FORZADO}")
            sys.exit(1)
    else:
        excel_path = buscar_excel_mas_reciente(CARPETA_EXCEL)
    ULTIMO_EXCEL_USADO = str(excel_path)
    log(f"Archivo seleccionado : {excel_path.name}")
    log(f"Ruta completa        : {excel_path}")
    log(f"Tamaño               : {excel_path.stat().st_size} bytes")
    log(f"Modificado           : {datetime.fromtimestamp(excel_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')}")
    try:
        total_filas, filas_excel = resumen_excel_precios(excel_path)
    except Exception as e:
        log(f"❌ ERROR: El archivo no se pudo leer, no tiene el formato esperado por Tivendo: {e}")
        sys.exit(CODIGO_SALIDA_FORMATO_INVALIDO)

    log(f"Filas de precios     : {total_filas}")

    avisos_encabezado = validar_encabezado_precios(filas_excel)
    if avisos_encabezado:
        log(f"⚠️  Encabezado con {len(avisos_encabezado)} inconsistencia(s):")
        for aviso in avisos_encabezado:
            log(f"  - {aviso}")
        try:
            if corregir_encabezado_excel(excel_path, filas_excel[0]):
                log("✓ Encabezado corregido automáticamente en el archivo antes de subir")
                total_filas, filas_excel = resumen_excel_precios(excel_path)
            else:
                log("⚠️  No se pudo corregir el encabezado automáticamente; se sube igual (no bloquea)")
        except Exception as e:
            log(f"⚠️  No se pudo corregir el encabezado automáticamente ({e}); se sube igual (no bloquea)")

    errores_formato = validar_estructura_precios(filas_excel)
    if errores_formato:
        log(f"❌ El archivo tiene {len(errores_formato)} error(es) y no se subirá:")
        for err in errores_formato:
            log(f"  - {err}")
        sys.exit(CODIGO_SALIDA_FORMATO_INVALIDO)

    log("✓ Archivo verificado: formato correcto")
    log("Cambios de precios a subir:")
    for idx, fila in enumerate(filas_excel[1:], start=1):
        log(f"  {idx:02d}. {describir_cambio_precio(fila)}")

    async with async_playwright() as p:
        browser, context, page = await crear_pagina_trabajo(
            p,
            mostrar_navegador=MOSTRAR_NAVEGADOR,
            incognito=True,
            accept_downloads=False,
            bypass_csp=False,
        )

        try:
            # ── 1. LOGIN ───────────────────────────────────────────
            with medidor.etapa("Login Tivendo"):
                await login_tivendo(page, EMAIL, PASSWORD, log)

            # ── 2. PORTAL DEFONTANA → VERIFICAR EMPRESA + PUNTO DE VENTAS ───
            # Después del login puede redirigir a portal.defontana.com.
            # IMPORTANTE: verificar que la empresa activa sea MERCADO HOUSE SPA
            # antes de entrar a cualquier módulo.
            if "portal.defontana.com" in page.url:
                await asegurar_empresa_mercadohouse(page, log, EMAIL, PASSWORD)
                page = await asegurar_punto_ventas_abierto(context, page, log)
                if "login" in page.url.lower():
                    log("Punto de Ventas pidio login nuevamente; reingresando...")
                    await login_tivendo(page, EMAIL, PASSWORD, log)

            # ── 3. NAVEGAR A LISTAS DE PRECIOS ─────────────────────
            with medidor.etapa("Cargar listas de precios Tivendo"):
                log("Navegando a Listas de precios...")
                await page.goto(
                    "https://tivendoapp.defontana.com/configuracion/listas_precios",
                    timeout=30000
                )
                await esperar_carga_ligera(page)
                if "login" in page.url.lower():
                    log("Listas de precios redirigio a login; reingresando y reintentando...")
                    await login_tivendo(page, EMAIL, PASSWORD, log)
                    await page.goto(
                        "https://tivendoapp.defontana.com/configuracion/listas_precios",
                        timeout=30000
                    )
                    await esperar_carga_ligera(page)
                if "login" in page.url.lower():
                    raise Exception("No se pudo entrar a Listas de precios: Tivendo volvio al login")
                await page.locator("table tbody tr").first.wait_for(state="visible", timeout=45000)

            # ── 4. ENCONTRAR LA FILA DE LA LISTA CORRECTA ──────────
            suc = _cfg.sucursal_activa()
            lista_erp = suc["tivendo_lista_erp"]
            lista_num = suc["tivendo_lista_num"]
            log(f"Buscando lista '{lista_erp}'...")
            filas = page.locator("table tbody tr")
            count = await filas.count()
            log(f"  Filas encontradas: {count}")

            fila_objetivo = None
            for i in range(count):
                celdas = filas.nth(i).locator("td")
                primera = await celdas.first.inner_text()
                desc_raw = await celdas.nth(1).inner_text()
                desc = desc_raw.strip().upper()
                match_num = lista_num and primera.strip() == str(lista_num)
                match_nombre = lista_erp.upper() in desc
                if match_num or match_nombre:
                    fila_objetivo = filas.nth(i)
                    log(f"✓ Lista encontrada: {desc_raw.strip()}")
                    break

            if fila_objetivo is None:
                log(f"ERROR: No se encontró la lista '{lista_erp}'")
                raise Exception("Lista no encontrada")

            # ── 5. ABRIR MENÚ DE TRES PUNTOS ───────────────────────
            log("Abriendo menú de opciones (tres puntos)...")
            boton_menu = fila_objetivo.locator("button").last
            await boton_menu.click()
            await page.get_by_text("Importar precios").wait_for(state="visible", timeout=10000)

            # ── 6. CLIC EN "IMPORTAR PRECIOS" ──────────────────────
            log("Seleccionando 'Importar precios'...")
            await page.get_by_text("Importar precios").click()

            # ── 7. ESPERAR MODAL Y ADJUNTAR ARCHIVO ────────────────
            log("Esperando modal de importación...")
            await page.wait_for_selector("text=Importar precios", timeout=10000)

            log(f"Adjuntando archivo: {excel_path.name}")
            file_input = page.locator('input[type="file"]')
            await file_input.set_input_files(str(excel_path))
            await pausa_corta(0.2)

            # ── 8. CLIC EN "LEER FICHERO" ──────────────────────────
            log("Haciendo clic en 'Leer fichero'...")
            await page.get_by_role("button", name="Leer fichero").click()
            await page.get_by_role("button", name="Guardar").wait_for(state="visible", timeout=30000)
            log("✓ Fichero procesado — tabla de precios visible")
            try:
                filas_preview = await page.locator("table tbody tr").count()
                log(f"  Filas visibles tras leer fichero: {filas_preview}")
            except Exception as e:
                log(f"  No se pudo contar filas procesadas en Tivendo: {e}")

            # ── 9. CLIC EN "GUARDAR" ───────────────────────────────
            log("Haciendo clic en 'Guardar'...")
            guardar = page.get_by_role("button", name="Guardar")
            await guardar.wait_for(state="visible", timeout=15000)
            await guardar.click()
            resultado = await leer_resultado_final_importacion(page, log, total_filas if "total_filas" in locals() else None)
            total_resultado = resultado["total"]
            ok_resultado = resultado["ok"]
            fallidos_resultado = resultado["fallidos"]
            ULTIMO_RESULTADO_PRECIOS = {
                "total": total_resultado,
                "ok": ok_resultado,
                "fallidos": fallidos_resultado,
                "archivo": excel_path.name,
                "lista": _cfg.sucursal_activa()["tivendo_lista_erp"],
            }
            fallidos_normalizados = {codigo.strip().upper() for codigo in fallidos_resultado}
            for fila in filas_excel[1:]:
                codigo = _valor_fila(fila, 0).lstrip("'").strip().upper()
                precio = _valor_fila(fila, 3).replace(".", "").replace(",", "").strip()
                if codigo and codigo not in fallidos_normalizados and precio.isdigit():
                    ULTIMOS_PRECIOS_CONFIRMADOS[codigo] = int(precio)

            log("=" * 50)
            if fallidos_resultado:
                log("⚠️  PRECIOS IMPORTADOS CON OBSERVACIONES")
                log(f"   SE INGRESARON {total_resultado} CAMBIOS DE PRECIOS")
                log(f"   EXITO {ok_resultado}")
                log(f"   FALLIDOS {len(fallidos_resultado)}")
                log("   CODIGOS FALLIDOS:")
                for codigo in fallidos_resultado:
                    log(f"     {codigo}")
            else:
                log("✅ PRECIOS IMPORTADOS EXITOSAMENTE")
                log(f"   SE INGRESARON {total_resultado} CAMBIOS DE PRECIOS")
                log(f"   EXITO {ok_resultado}")
                log("   FALLIDOS 0")
            log(f"   Archivo : {excel_path.name}")
            log(f"   Lista   : {_cfg.sucursal_activa()['tivendo_lista_erp']}")
            log("=" * 50)

            borrar_excel_usado(excel_path)

        except Exception as e:
            log(f"❌ ERROR: {e}")
            await guardar_diagnostico(page, e, log, "upload_precios", medidor)
            medidor.resumen()
            await browser.close()
            sys.exit(1)

        await browser.close()
        medidor.resumen()
        log("")


if __name__ == "__main__":
    asyncio.run(subir_precios())
