import unittest
from unittest.mock import AsyncMock, patch

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


class MenuAutomaticSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_completo_marks_partial_price_import_as_failure(self):
        resultado_anterior = menu.upload_precios.ULTIMO_RESULTADO_PRECIOS
        try:
            menu.upload_precios.ULTIMO_RESULTADO_PRECIOS = {
                "total": 2,
                "ok": 1,
                "fallidos": ["A000012"],
            }
            with (
                patch.object(menu, "run_upload", new=AsyncMock(return_value=True)),
                patch.object(menu, "run_articulos", new=AsyncMock(return_value=True)),
                patch.object(menu, "run_packs", new=AsyncMock(return_value=True)),
                patch.object(menu, "run_precios", new=AsyncMock(return_value=True)),
                patch.object(menu, "imprimir_resumen_upload_precios"),
            ):
                ok = await menu.run_completo(interactivo=False)

            self.assertFalse(ok)
            self.assertFalse(menu.ULTIMO_RESUMEN_CICLO["ok"])
            self.assertEqual(
                menu.ULTIMO_RESUMEN_CICLO["etapa_fallida"],
                "Upload Precios → Tivendo POS",
            )
        finally:
            menu.upload_precios.ULTIMO_RESULTADO_PRECIOS = resultado_anterior

    async def test_modo_automatico_no_notifica_si_todo_sale_bien(self):
        resumen_anterior = menu.ULTIMO_RESUMEN_CICLO
        try:
            menu.ULTIMO_RESUMEN_CICLO = {
                "ok": True,
                "sucursal": "LIBERTADOR 1476",
                "pasos": [],
                "precios": {},
                "duracion": "1m",
                "hora_fin": "21:31",
            }
            with (
                patch.object(menu, "run_completo", new=AsyncMock(return_value=True)),
                patch.object(
                    menu.notificaciones_whatsapp,
                    "enviar_notificacion",
                    new=AsyncMock(),
                ) as enviar,
                patch.object(menu.sys, "argv", ["MercadohouseSync.exe", "--auto"]),
            ):
                await menu.modo_automatico()

            enviar.assert_not_awaited()
        finally:
            menu.ULTIMO_RESUMEN_CICLO = resumen_anterior


if __name__ == "__main__":
    unittest.main()
