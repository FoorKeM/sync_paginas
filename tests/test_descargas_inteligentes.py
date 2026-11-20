import asyncio
import tempfile
import unittest
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook

import menu
import sync_precios
import utils


def excel_articulos():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Código", "Descripción"])
    sheet.append(["A001", "Artículo válido"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


class DescargaFalsa:
    def __init__(self, nombre, contenido):
        self.suggested_filename = nombre
        self.contenido = contenido

    async def failure(self):
        return None

    async def save_as(self, destino):
        Path(destino).write_bytes(self.contenido)


class GuardadoSeguroTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_excel_replaces_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "articulos.xlsx"
            old.write_bytes(b"previous")
            contenido = excel_articulos()
            name, saved = await utils.guardar_descarga_validada(
                DescargaFalsa("articulos.xlsx", contenido), tmp, "articulos.xlsx", lambda _: None
            )
            self.assertEqual(name, "articulos.xlsx")
            self.assertEqual(Path(saved).read_bytes(), contenido)

    async def test_corrupt_excel_keeps_previous_file_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "articulos.xlsx"
            old.write_bytes(b"previous")
            with self.assertRaises(Exception):
                await utils.guardar_descarga_validada(
                    DescargaFalsa("articulos.xlsx", b"broken" * 100), tmp,
                    "articulos.xlsx", lambda _: None,
                )
            self.assertEqual(old.read_bytes(), b"previous")
            self.assertFalse(list(Path(tmp).glob("*.part")))

    async def test_provisional_price_keeps_previous_file_until_business_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path(tmp) / "precios.xls"
            previous.write_bytes(b"previous valid export")
            html = b"<html><table><tr><td>Cabecera</td></tr><tr><td></td><td>A001</td><td>Rango</td><td>2500</td></tr></table></html>"
            _, provisional = await utils.guardar_descarga_validada(
                DescargaFalsa("precios.xls", html), tmp, "precios.xls", lambda _: None,
                sync_precios.leer_precios_exportados, provisional=True,
            )
            self.assertEqual(previous.read_bytes(), b"previous valid export")
            self.assertEqual(sync_precios.leer_precios_exportados(Path(provisional)), {"A001": 2500})


class LocatorFalso:
    def __init__(self, page=None):
        self.page = page
        self.first = self
        self.last = self

    async def count(self):
        return 1

    async def get_attribute(self, _name):
        return "/articulos/listado_articulos"

    async def click(self, **_kwargs):
        if self.page and self.page.on_click:
            self.page.on_click()

    async def wait_for(self, **_kwargs):
        if self.page and self is self.page.export and not self.page.recovered:
            raise TimeoutError("Exportar todavía no aparece")

    async def is_visible(self):
        return True


class PaginaFalsa:
    def __init__(self, url):
        self.url = url
        self.recovered = False
        self.on_click = None
        self.visited = []
        self.link = LocatorFalso(self)
        self.export = LocatorFalso(self)

    def is_closed(self):
        return False

    def get_by_role(self, role, **_kwargs):
        return self.link if role == "link" else self.export

    def get_by_text(self, *_args, **_kwargs):
        return LocatorFalso()

    async def wait_for_url(self, *_args, **_kwargs):
        raise TimeoutError("El clic no navegó")

    async def goto(self, url, **_kwargs):
        self.visited.append(url)
        self.url = url
        self.recovered = True


class NavegacionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_existing_module_tab_without_clicking_portal_again(self):
        portal = PaginaFalsa("https://portal.defontana.com/dashboard")
        pos = PaginaFalsa("https://tivendoapp.defontana.com/")
        context = type("Contexto", (), {"pages": [portal, pos]})()
        with patch.object(utils, "click_tarjeta_defontana", new_callable=AsyncMock) as click:
            found = await utils.abrir_modulo_defontana(
                context, portal, "Punto de Ventas", "tivendoapp.defontana.com",
                ("Artículos",), lambda _: None,
            )
        self.assertIs(found, pos)
        click.assert_not_awaited()

    async def test_recovers_listing_from_actual_link_url(self):
        page = PaginaFalsa("https://tivendoapp.defontana.com/")
        await utils.abrir_seccion_pos(page, "Listado de artículos", lambda _: None)
        self.assertEqual(page.visited, ["https://tivendoapp.defontana.com/articulos/listado_articulos"])


class EmisorFalso:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, value):
        for callback in tuple(self.listeners.get(event, [])):
            callback(value)


class PopupFalso(EmisorFalso):
    def __init__(self):
        super().__init__()
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class ExportLocatorFalso:
    def __init__(self, page, popup, download):
        self.page = page
        self.popup = popup
        self.download = download
        self.last = self

    async def wait_for(self, **_kwargs):
        pass

    async def click(self, **_kwargs):
        self.page.emit("popup", self.popup)
        self.page.emit("download", self.download)


class PaginaExportFalsa(EmisorFalso):
    def __init__(self, popup, download):
        super().__init__()
        self.popup = popup
        self.download = download

    def get_by_role(self, _role, **_kwargs):
        return self

    async def wait_for(self, **_kwargs):
        pass

    async def click(self, **_kwargs):
        self.emit("popup", self.popup)
        self.emit("download", self.download)


class PrecioPopupTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_original_page_listener_after_popup_and_closes_popup_after_save(self):
        html = b"<html><table><tr><td>Cabecera</td></tr><tr><td></td><td>A001</td><td>Rango</td><td>2,500</td></tr></table></html>"
        page, popup = EmisorFalso(), PopupFalso()
        download = DescargaFalsa("precios.xls", html)
        locator = ExportLocatorFalso(page, popup, download)
        frame = type("Frame", (), {"locator": lambda _self, _selector: locator})()
        with tempfile.TemporaryDirectory() as tmp:
            name, path = await sync_precios.exportar_precios_tivendo(page, frame, tmp, lambda _: None)
            self.assertEqual(name, "precios.xls")
            self.assertEqual(sync_precios.leer_precios_exportados(Path(path)), {"A001": 2500})
        self.assertTrue(popup.closed)
        self.assertEqual(page.listeners["download"], [])
        self.assertEqual(page.listeners["popup"], [])

    async def test_pos_export_keeps_page_listener_when_popup_opens(self):
        popup = PopupFalso()
        page = PaginaExportFalsa(popup, DescargaFalsa("packs.xlsx", excel_articulos()))
        with tempfile.TemporaryDirectory() as tmp:
            _, path = await utils.exportar_excel_pos(page, tmp, "packs.xlsx", lambda _: None)
            self.assertTrue(Path(path).is_file())
        self.assertTrue(popup.closed)
        self.assertEqual(page.listeners["download"], [])


class ReintentoTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_error_retries_once_and_reports_attempt_number(self):
        output = StringIO()
        run = AsyncMock(side_effect=[utils.ErrorEtapaFuente("Artículos", "Descargar", "sin archivo"), None])
        with patch.object(menu.asyncio, "sleep", new_callable=AsyncMock), \
             patch.object(menu, "borrar_sesion_guardada") as clear, \
             redirect_stdout(output):
            ok = await menu.ejecutar_con_reintentos("Sync Artículos", lambda: True, run)
        self.assertTrue(ok)
        self.assertEqual(run.await_count, 2)
        clear.assert_not_called()
        self.assertIn("falló. Reintentando… intento 2 de 3", output.getvalue())
        self.assertIn("Descargar", output.getvalue())


if __name__ == "__main__":
    unittest.main()
