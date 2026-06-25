import unittest
from unittest.mock import patch

import menu


class MenuCredentialTests(unittest.TestCase):
    def test_guardar_credenciales_updates_runtime_values_for_all_modules(self):
        originales = {
            "upload": (menu.upload_precios.EMAIL, menu.upload_precios.PASSWORD),
            "articulos": (
                menu.sync_articulos.TIVENDO_EMAIL,
                menu.sync_articulos.TIVENDO_PASSWORD,
                menu.sync_articulos.MH_EMAIL,
                menu.sync_articulos.MH_PASSWORD,
            ),
            "precios": (
                menu.sync_precios.TIVENDO_EMAIL,
                menu.sync_precios.TIVENDO_PASSWORD,
                menu.sync_precios.MH_EMAIL,
                menu.sync_precios.MH_PASSWORD,
            ),
            "packs": (
                menu.sync_packs.TIVENDO_EMAIL,
                menu.sync_packs.TIVENDO_PASSWORD,
                menu.sync_packs.MH_EMAIL,
                menu.sync_packs.MH_PASSWORD,
            ),
        }
        try:
            with patch("credenciales.guardar_credenciales") as guardar:
                menu._guardar_credenciales(
                    "tv@example.com",
                    "tv-pass",
                    "mh@example.com",
                    "mh-pass",
                )

            guardar.assert_called_once_with(
                "tv@example.com",
                "tv-pass",
                "mh@example.com",
                "mh-pass",
            )
            self.assertEqual(menu.upload_precios.EMAIL, "tv@example.com")
            self.assertEqual(menu.upload_precios.PASSWORD, "tv-pass")
            self.assertEqual(menu.sync_articulos.TIVENDO_EMAIL, "tv@example.com")
            self.assertEqual(menu.sync_articulos.TIVENDO_PASSWORD, "tv-pass")
            self.assertEqual(menu.sync_articulos.MH_EMAIL, "mh@example.com")
            self.assertEqual(menu.sync_articulos.MH_PASSWORD, "mh-pass")
            self.assertEqual(menu.sync_precios.TIVENDO_EMAIL, "tv@example.com")
            self.assertEqual(menu.sync_precios.TIVENDO_PASSWORD, "tv-pass")
            self.assertEqual(menu.sync_precios.MH_EMAIL, "mh@example.com")
            self.assertEqual(menu.sync_precios.MH_PASSWORD, "mh-pass")
            self.assertEqual(menu.sync_packs.TIVENDO_EMAIL, "tv@example.com")
            self.assertEqual(menu.sync_packs.TIVENDO_PASSWORD, "tv-pass")
            self.assertEqual(menu.sync_packs.MH_EMAIL, "mh@example.com")
            self.assertEqual(menu.sync_packs.MH_PASSWORD, "mh-pass")
        finally:
            menu.upload_precios.EMAIL, menu.upload_precios.PASSWORD = originales["upload"]
            (
                menu.sync_articulos.TIVENDO_EMAIL,
                menu.sync_articulos.TIVENDO_PASSWORD,
                menu.sync_articulos.MH_EMAIL,
                menu.sync_articulos.MH_PASSWORD,
            ) = originales["articulos"]
            (
                menu.sync_precios.TIVENDO_EMAIL,
                menu.sync_precios.TIVENDO_PASSWORD,
                menu.sync_precios.MH_EMAIL,
                menu.sync_precios.MH_PASSWORD,
            ) = originales["precios"]
            (
                menu.sync_packs.TIVENDO_EMAIL,
                menu.sync_packs.TIVENDO_PASSWORD,
                menu.sync_packs.MH_EMAIL,
                menu.sync_packs.MH_PASSWORD,
            ) = originales["packs"]


if __name__ == "__main__":
    unittest.main()
