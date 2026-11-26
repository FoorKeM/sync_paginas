import unittest
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from unittest.mock import AsyncMock, patch

import menu
import credenciales


class ConfiguracionMenuTests(unittest.TestCase):
    def test_main_and_submenu_boxes_use_one_width_without_cutting_values(self):
        salidas = []
        salida = StringIO()
        with patch.object(menu, "tarea_hoy_info", return_value=""), redirect_stdout(salida):
            menu.banner()
        salidas.append(salida.getvalue())

        salida = StringIO()
        with patch.object(menu, "clear"), patch("builtins.input", return_value="4"), \
             redirect_stdout(salida):
            menu.seleccionar_sucursal()
        salidas.append(salida.getvalue())

        salida = StringIO()
        correo_largo = "nombre.muy.largo.para.probar.el.ajuste@example.com"
        with patch("importlib.reload", side_effect=lambda modulo: modulo), \
             patch.object(credenciales, "TIVENDO_EMAIL", correo_largo), \
             patch.object(credenciales, "TIVENDO_PASSWORD", "clave"), \
             patch.object(credenciales, "MH_EMAIL", "mh@example.com"), \
             patch.object(credenciales, "MH_PASSWORD", "clave"), \
             patch.object(menu, "clear"), patch("builtins.input", return_value="6"), \
             redirect_stdout(salida):
            menu.editar_credenciales()
        salidas.append(salida.getvalue())
        self.assertIn("nombre.muy.largo.para.probar.el.ajuste", salidas[-1])
        self.assertIn("@example.com", salidas[-1])

        for texto in salidas:
            lineas = [linea for linea in texto.splitlines() if linea.startswith("  ║") or linea.startswith("  ╔") or linea.startswith("  ╠") or linea.startswith("  ╚")]
            self.assertTrue(lineas)
            self.assertEqual({len(linea) for linea in lineas}, {menu.MENU_ANCHO + 4})

    def test_main_menu_groups_settings_and_places_exit_last(self):
        salida = StringIO()
        with patch.object(menu, "tarea_hoy_info", return_value=""), redirect_stdout(salida):
            menu.banner()
        texto = salida.getvalue()
        self.assertLess(texto.index("11.  CONFIGURACIÓN"), texto.index("12.  OCULTAR EN BANDEJA"))
        self.assertLess(texto.index("12.  OCULTAR EN BANDEJA"), texto.index("13.  SALIR"))
        self.assertNotIn("Telegram", texto)
        self.assertNotIn("Borrar sesión", texto)

    def test_sync_groups_are_ordered_and_descriptions_removed(self):
        salida = StringIO()
        with patch.object(menu, "tarea_hoy_info", return_value=""), redirect_stdout(salida):
            menu.banner()
        texto = salida.getvalue()
        opciones = ["1.  UPLOAD PRECIOS", "2.  SYNC ARTÍCULOS", "3.  SYNC PRECIOS",
                    "4.  SYNC PACKS", "5.  SYNC ART + PRECIOS", "6.  SYNC PACKS + PRECIOS",
                    "7.  SYNC TODO", "8.  COMPLETO MANUAL", "9.  PROGRAMAR CAMBIO DE PRECIOS",
                    "10.  AJUSTAR STOCK NEGATIVO", "11.  CONFIGURACIÓN",
                    "12.  OCULTAR EN BANDEJA", "13.  SALIR"]
        posiciones = [texto.index(opcion) for opcion in opciones]
        self.assertEqual(posiciones, sorted(posiciones))
        self.assertNotIn("Tivendo → MH", texto)
        self.assertNotIn("(1 → 2 → 4 → 3)", texto)


class ConfiguracionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_and_telegram_boxes_match_main_menu_width(self):
        for mostrar, salir in ((menu.menu_configuracion, "5"),
                               (menu.menu_telegram, "8")):
            salida = StringIO()
            with patch.object(menu, "clear"), \
                 patch.object(menu, "bot_exclusivo", return_value=nullcontext(True)), \
                 patch("builtins.input", return_value=salir), redirect_stdout(salida):
                await mostrar()
            lineas = [linea for linea in salida.getvalue().splitlines()
                      if linea.startswith(("  ║", "  ╔", "  ╠", "  ╚"))]
            self.assertTrue(lineas)
            self.assertEqual({len(linea) for linea in lineas}, {menu.MENU_ANCHO + 4})

    async def test_settings_submenu_reaches_each_action_and_returns(self):
        salida = StringIO()
        with patch.object(menu, "clear"), patch.object(menu, "titulo"), \
             patch.object(menu, "seleccionar_sucursal") as sucursal, \
             patch.object(menu, "editar_credenciales") as credenciales, \
             patch.object(menu, "borrar_sesion_guardada", return_value=True) as borrar, \
             patch.object(menu, "menu_telegram", new_callable=AsyncMock) as telegram, \
             patch("builtins.input", side_effect=["1", "2", "3", "", "4", "5"]), \
             redirect_stdout(salida):
            await menu.menu_configuracion()
        sucursal.assert_called_once()
        credenciales.assert_called_once()
        borrar.assert_called_once()
        telegram.assert_awaited_once()
        self.assertIn("5. Volver al menú principal", salida.getvalue())


class PrincipalDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_renumbered_options_run_the_correct_groups(self):
        with patch.object(menu, "clear"), patch.object(menu, "banner"), \
             patch.object(menu, "esperar"), \
             patch.object(menu, "configurar_credenciales_iniciales", new_callable=AsyncMock), \
             patch.object(menu, "ejecutar_operacion", new_callable=AsyncMock) as ejecutar, \
             patch("builtins.input", side_effect=["5", "6", "7", "8", "13"]):
            await menu.menu_principal()
        funciones = [llamada.args[1] for llamada in ejecutar.await_args_list]
        self.assertEqual(funciones, [menu.run_solo_sync, menu.run_packs_y_precios,
                                     menu.run_sync_todo, menu.run_completo_manual_con_excel])

    async def test_main_menu_opens_configuration_then_exits(self):
        with patch.object(menu, "clear"), patch.object(menu, "banner"), \
             patch.object(menu, "configurar_credenciales_iniciales", new_callable=AsyncMock), \
             patch.object(menu, "menu_configuracion", new_callable=AsyncMock) as config, \
             patch("builtins.input", side_effect=["11", "13"]):
            await menu.menu_principal()
        config.assert_awaited_once()

    async def test_send_to_tray_keeps_bot_active_and_exits_menu(self):
        with patch.object(menu, "clear"), patch.object(menu, "banner"), \
             patch.object(menu, "configurar_credenciales_iniciales", new_callable=AsyncMock), \
             patch.object(menu, "iniciar_icono_bandeja", return_value=True) as icono, \
             patch("builtins.input", return_value="12"):
            await menu.menu_principal()
        icono.assert_called_once()


if __name__ == "__main__":
    unittest.main()
