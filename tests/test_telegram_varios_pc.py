import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import menu
import telegram_bot


class TelegramVariosPcTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_pc_sends_to_same_chat_without_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "telegram_bot.json"
            respuestas = [{"username": "mi_bot"}, {"type": "private"},
                         {"type": "private"}, {"message_id": 1}, {"message_id": 2}]
            with patch.object(telegram_bot, "CONFIG_FILE", ruta), \
                 patch.object(telegram_bot, "api", new_callable=AsyncMock,
                              side_effect=respuestas) as api, \
                 patch.object(telegram_bot.socket, "gethostname", return_value="PC-CIERRE-2"), \
                 redirect_stdout(StringIO()):
                self.assertTrue(await telegram_bot.configurar_solo_avisos(
                    lambda _: "token", lambda _: "12345, 67890"))
                config = telegram_bot.cargar_config()
                self.assertEqual(config["modo"], "avisos")
                self.assertEqual(config["chat_id"], 12345)
                self.assertEqual(config["chat_ids"], [12345, 67890])
                self.assertEqual([c.args[1] for c in api.await_args_list],
                                 ["getMe", "getChat", "getChat", "sendMessage", "sendMessage"])
                self.assertEqual(api.await_args_list[-1].args[2]["chat_id"], 67890)
                self.assertIn("PC: PC-CIERRE-2", api.await_args_list[-1].args[2]["text"])
                with patch.object(menu.telegram_bot, "cargar_config", return_value=config), \
                     patch.object(menu.telegram_bot, "Bot") as bot, \
                     redirect_stdout(StringIO()):
                    await menu.servir_bot()
                    self.assertFalse(await menu.activar_bot_en_segundo_plano())
                bot.assert_not_called()

    async def test_failed_connection_preserves_previous_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "telegram_bot.json"
            anterior = {"token": "original", "chat_id": 55, "user_id": 55}
            ruta.write_text(json.dumps(anterior), encoding="utf-8")
            with patch.object(telegram_bot, "CONFIG_FILE", ruta), \
                 patch.object(telegram_bot, "api", new_callable=AsyncMock,
                              side_effect=[{"username": "mi_bot"}, {"type": "group"}]), \
                 redirect_stdout(StringIO()):
                self.assertFalse(await telegram_bot.configurar_solo_avisos(
                    lambda _: "nuevo-token", lambda _: "12345"))
            self.assertEqual(json.loads(ruta.read_text(encoding="utf-8")), anterior)

    async def test_multiple_accounts_can_join_and_removed_account_loses_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "telegram_bot.json"
            invite_path = Path(tmp) / "telegram_vinculacion.json"
            config = {"token": "test", "chat_id": 10, "user_id": 20}
            bot = telegram_bot.Bot(config, AsyncMock())
            with patch.object(telegram_bot, "CONFIG_FILE", config_path), \
                 patch.object(telegram_bot, "VINCULACION_FILE", invite_path), \
                 patch.object(telegram_bot.secrets, "token_hex", return_value="abc12345"), \
                 patch.object(telegram_bot, "api", new_callable=AsyncMock) as api:
                codigo = telegram_bot.crear_invitacion()
                await bot.procesar({"message": {"chat": {"id": 30, "type": "private"},
                                                "from": {"id": 40, "first_name": "Ana"},
                                                "text": f"/vincular {codigo}"}})
                codigo = telegram_bot.crear_invitacion()
                await bot.procesar({"message": {"chat": {"id": 50, "type": "private"},
                                                "from": {"id": 60, "first_name": "Luis"},
                                                "text": f"/start vincular_{codigo}"}})
                self.assertEqual(telegram_bot.destinatarios(config), [10, 30, 50])
                self.assertFalse(invite_path.exists())
                api.reset_mock()
                await telegram_bot.enviar("Cambio programado", config)
                ids = [call.args[2]["chat_id"] for call in api.await_args_list]
                self.assertEqual(ids, [10, 30, 50])
                self.assertEqual(config_path.exists(), True)

                self.assertTrue(telegram_bot.eliminar_cuenta(40))
                self.assertEqual(telegram_bot.destinatarios(telegram_bot.cargar_config()), [10, 50])
                with patch.object(bot, "_iniciar", new_callable=AsyncMock) as iniciar:
                    await bot.procesar({"message": {"chat": {"id": 30, "type": "private"},
                                                    "from": {"id": 40}, "text": "precios"}})
                    iniciar.assert_not_awaited()
                    await bot.procesar({"message": {"chat": {"id": 50, "type": "private"},
                                                    "from": {"id": 60}, "text": "precios"}})
                    iniciar.assert_awaited_once_with("precios")

                self.assertTrue(telegram_bot.eliminar_cuenta(20))
                self.assertEqual(telegram_bot.cargar_config()["user_id"], 60)
                self.assertFalse(telegram_bot.eliminar_cuenta(60))

                qr_salida = StringIO()
                with redirect_stdout(qr_salida):
                    self.assertTrue(telegram_bot.mostrar_qr_invitacion("mi_bot", "ABC12345"))
                self.assertIn("https://t.me/mi_bot?start=vincular_ABC12345", qr_salida.getvalue())
                filas = [re.sub(r"\x1b\[[0-9;]*m", "", linea)
                         for linea in qr_salida.getvalue().splitlines() if "▀" in linea]
                self.assertTrue(filas)
                self.assertLess(len(filas), 25)
                self.assertLess(len(filas[0]), 50)

            with patch.object(bot, "_iniciar", new_callable=AsyncMock) as iniciar:
                await bot.procesar({"message": {"chat": {"id": 50, "type": "private"},
                                                "from": {"id": 60}, "text": "precios"}})
                iniciar.assert_awaited_once_with("precios")
                await bot.procesar({"message": {"chat": {"id": 31, "type": "private"},
                                                "from": {"id": 40}, "text": "precios"}})
                iniciar.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
