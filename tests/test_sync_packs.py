import unittest
from unittest.mock import patch

import sync_packs


class FakeFileInput:
    def __init__(self):
        self.files = []

    async def wait_for(self, **_kwargs):
        return None

    async def set_input_files(self, ruta):
        self.files.append(ruta)


class FakeButton:
    def __init__(self, name, visible=True):
        self.name = name
        self.visible = visible
        self.clicked = False

    async def wait_for(self, **_kwargs):
        if not self.visible:
            raise Exception(f"{self.name} no visible")

    async def click(self):
        self.clicked = True


class FakePage:
    def __init__(self, visible_buttons):
        self.file_input = FakeFileInput()
        self.buttons = {
            name: FakeButton(name, name in visible_buttons)
            for name in sync_packs.BOTONES_CARGA_PACKS
        }

    def locator(self, selector):
        if selector == 'input[type="file"]':
            return self.file_input
        raise AssertionError(f"Unexpected selector: {selector}")

    def get_by_role(self, role, name):
        if role != "button":
            raise AssertionError(f"Unexpected role: {role}")
        return self.buttons[name]


class SyncPacksTests(unittest.IsolatedAsyncioTestCase):
    async def test_subir_packs_uses_pack_button_first(self):
        page = FakePage({"CARGAR LISTA DE PACKS"})
        with patch.object(sync_packs, "pausa_corta", new=self._noop), \
                patch.object(sync_packs, "esperar_carga_ligera", new=self._noop):
            await sync_packs.subir_packs_mercadohouse(
                page,
                r"C:\Precios\descargas\packs.xlsx",
                "packs.xlsx",
                lambda _msg: None,
            )

        self.assertEqual(page.file_input.files, [r"C:\Precios\descargas\packs.xlsx"])
        self.assertTrue(page.buttons["CARGAR LISTA DE PACKS"].clicked)
        self.assertFalse(page.buttons["CARGAR LISTA DE ARTÍCULOS"].clicked)

    async def test_subir_packs_falls_back_to_article_button(self):
        page = FakePage({"CARGAR LISTA DE ARTÍCULOS"})
        logs = []

        with patch.object(sync_packs, "pausa_corta", new=self._noop), \
                patch.object(sync_packs, "esperar_carga_ligera", new=self._noop):
            await sync_packs.subir_packs_mercadohouse(
                page,
                r"C:\Precios\descargas\packs.xlsx",
                "packs.xlsx",
                logs.append,
            )

        self.assertEqual(page.file_input.files, [r"C:\Precios\descargas\packs.xlsx"])
        self.assertFalse(page.buttons["CARGAR LISTA DE PACKS"].clicked)
        self.assertTrue(page.buttons["CARGAR LISTA DE ARTÍCULOS"].clicked)
        self.assertTrue(any("CARGAR LISTA DE PACKS" in msg for msg in logs))

    async def test_subir_packs_raises_last_error_if_all_buttons_fail(self):
        page = FakePage(set())
        with patch.object(sync_packs, "pausa_corta", new=self._noop), \
                patch.object(sync_packs, "esperar_carga_ligera", new=self._noop):
            with self.assertRaisesRegex(Exception, "CARGAR LISTA DE ARTICULOS"):
                await sync_packs.subir_packs_mercadohouse(
                    page,
                    r"C:\Precios\descargas\packs.xlsx",
                    "packs.xlsx",
                    lambda _msg: None,
                )

        self.assertEqual(page.file_input.files, [r"C:\Precios\descargas\packs.xlsx"])
        self.assertFalse(any(button.clicked for button in page.buttons.values()))

    async def _noop(self, *_args, **_kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
