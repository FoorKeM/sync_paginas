import unittest
import os
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import AsyncMock, patch

import menu
from utils import ErrorCredenciales, _contrasena_rechazada


class PasswordInputTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "La lectura visible usa la consola de Windows")
    def test_asterisks_follow_typing_and_backspace_without_exposing_password(self):
        output = StringIO()
        with patch("sys.stdin", MagicMock(isatty=lambda: True)), \
             patch("msvcrt.getwch", side_effect=["a", "b", "\b", "c", "\r"]), \
             redirect_stdout(output):
            password = menu.leer_contrasena("Clave: ")
        self.assertEqual(password, "ac")
        self.assertEqual(output.getvalue(), "Clave: **\b \b*\n")
        self.assertNotIn("ac", output.getvalue())

    @unittest.skipUnless(os.name == "nt", "La lectura visible usa la consola de Windows")
    def test_tab_toggles_visibility_during_entry(self):
        output = StringIO()
        with patch("sys.stdin", MagicMock(isatty=lambda: True)), \
             patch("msvcrt.getwch", side_effect=["a", "b", "\t", "c", "\t", "\r"]), \
             redirect_stdout(output):
            password = menu.leer_contrasena("Clave: ")
        self.assertEqual(password, "abc")
        self.assertIn("**\b\bab", output.getvalue())
        self.assertTrue(output.getvalue().endswith("\b\b\b***\n"))

class SavedPasswordVisibilityTests(unittest.TestCase):
    def test_editor_reveals_saved_passwords_only_when_selected(self):
        import credenciales
        output = StringIO()
        with patch("importlib.reload", side_effect=lambda module: module), \
             patch.object(credenciales, "TIVENDO_EMAIL", "tv@example.com"), \
             patch.object(credenciales, "TIVENDO_PASSWORD", "tv-secret"), \
             patch.object(credenciales, "MH_EMAIL", "mh@example.com"), \
             patch.object(credenciales, "MH_PASSWORD", "mh-secret"), \
             patch.object(menu, "clear"), \
             patch.object(menu, "titulo"), \
             patch("builtins.input", side_effect=["5", "5", "6"]), \
             redirect_stdout(output):
            menu.editar_credenciales()
        rendered = output.getvalue()
        self.assertEqual(rendered.count("Clave Tivendo guardada: tv-secret"), 1)
        self.assertEqual(rendered.count("Clave Mercadohouse guardada: mh-secret"), 1)


class CredentialFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_setup_checks_both_services_before_saving(self):
        empty = dict.fromkeys(("TIVENDO_EMAIL", "TIVENDO_PASSWORD", "MH_EMAIL", "MH_PASSWORD"), "")
        output = StringIO()
        with patch("credenciales.cargar_credenciales", return_value=empty), \
             patch("builtins.input", side_effect=["tv@example.com", "tv-key", "mh@example.com", "mh-key"]), \
             patch.object(menu, "leer_contrasena") as hidden_input, \
             patch.object(menu, "verificar_credencial", new_callable=AsyncMock) as verify, \
             patch.object(menu, "_guardar_credenciales") as save, \
             redirect_stdout(output):
            self.assertTrue(await menu.configurar_credenciales_iniciales())
        self.assertEqual([call.args for call in verify.call_args_list], [
            ("Tivendo", "tv@example.com", "tv-key"),
            ("Mercadohouse", "mh@example.com", "mh-key"),
        ])
        save.assert_called_once_with("tv@example.com", "tv-key", "mh@example.com", "mh-key")
        hidden_input.assert_not_called()
        rendered = output.getvalue()
        self.assertLess(rendered.index("INGRESA CREDENCIALES TIVENDO"),
                        rendered.index("Acceso a Tivendo comprobado."))
        self.assertLess(rendered.index("Acceso a Tivendo comprobado."),
                        rendered.index("INGRESA CREDENCIALES MERCADO HOUSE"))

    async def test_saved_credentials_skip_first_setup(self):
        saved = {"TIVENDO_EMAIL": "tv", "TIVENDO_PASSWORD": "a", "MH_EMAIL": "mh", "MH_PASSWORD": "b"}
        with patch("credenciales.cargar_credenciales", return_value=saved), \
             patch.object(menu, "verificar_credencial", new_callable=AsyncMock) as verify, \
             patch.object(menu, "_guardar_credenciales") as save:
            self.assertTrue(await menu.configurar_credenciales_iniciales())
        verify.assert_not_awaited()
        save.assert_not_called()

    async def test_rejected_initial_password_is_corrected_before_saving(self):
        empty = dict.fromkeys(("TIVENDO_EMAIL", "TIVENDO_PASSWORD", "MH_EMAIL", "MH_PASSWORD"), "")
        with patch("credenciales.cargar_credenciales", return_value=empty), \
             patch("builtins.input", side_effect=["bad@example.com", "wrong", "tv@example.com", "correct", "mh@example.com", "mh-key"]), \
             patch.object(menu, "verificar_credencial", new_callable=AsyncMock,
                          side_effect=[ErrorCredenciales("Tivendo"), None, None]) as verify, \
             patch.object(menu, "_guardar_credenciales") as save:
            self.assertTrue(await menu.configurar_credenciales_iniciales())
        self.assertEqual(verify.await_count, 3)
        save.assert_called_once_with("tv@example.com", "correct", "mh@example.com", "mh-key")

    async def test_sync_retries_after_password_correction(self):
        run = AsyncMock(side_effect=[ErrorCredenciales("Tivendo"), None])
        with patch.object(menu, "corregir_contrasena", new_callable=AsyncMock, return_value=True), \
             patch.object(menu, "borrar_sesion_guardada"), \
             patch("sys.argv", ["MHSync.exe"]):
            self.assertTrue(await menu.ejecutar_con_reintentos("Sync Packs", lambda: True, run))
        self.assertEqual(run.await_count, 2)

    async def test_only_explicit_password_message_is_classified(self):
        page = unittest.mock.MagicMock()
        page.locator.return_value.inner_text = AsyncMock(return_value="Correo o contraseña incorrectos")
        self.assertTrue(await _contrasena_rechazada(page))
        page.locator.return_value.inner_text = AsyncMock(return_value="Ocurrió un error de conexión")
        self.assertFalse(await _contrasena_rechazada(page))


if __name__ == "__main__":
    unittest.main()
