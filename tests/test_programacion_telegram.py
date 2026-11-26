import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook

import menu
import telegram_bot


class AvisoProgramacionTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_excel_can_schedule_same_windows_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            excel = carpeta / "precios.xlsx"
            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "RangoInicial1", "RangoFinal1", "Precio1",
                         "RangoInicial2", "RangoFinal2", "Precio2"])
            hoja.append(["A014819", 1, 9999, 990, None, None, None])
            libro.save(excel)
            @contextmanager
            def libre():
                yield True

            with patch.object(menu, "_es_administrador", return_value=True), \
                 patch.object(menu, "trabajo_exclusivo", libre), \
                 patch.object(menu, "datetime") as reloj, \
                 patch.object(menu, "date") as calendario, \
                 patch.object(menu.sys, "frozen", True, create=True), \
                 patch.object(menu, "_archivo_programado", return_value=carpeta / "excel_programado.txt"), \
                 patch.object(menu, "runtime_path", side_effect=lambda nombre: carpeta / "runtime" / nombre), \
                 patch.object(menu.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0)) as tarea, \
                 patch.object(menu.upload_precios, "detalles_productos_para_vista_previa",
                              return_value={"A014819": {"nombre": "PAPEL SCOTT"}}), \
                 patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock,
                              return_value=True) as enviar:
                reloj.now.return_value = datetime(2026, 11, 16, 12, 0)
                calendario.today.return_value = date(2026, 11, 16)
                ok, detalle = await menu.programar_desde_bot(excel, "22:00", True)
            self.assertTrue(ok, detalle)
            self.assertEqual((carpeta / "excel_programado.txt").read_text(encoding="utf-8"), str(excel))
            comando = tarea.call_args.args[0]
            self.assertIn("/F", comando)
            self.assertEqual(comando[comando.index("/TN") + 1], "SyncMercadohouseHoy")
            self.assertIn("--shutdown", comando[comando.index("/TR") + 1])
            mensaje = "\n".join(call.args[0] for call in enviar.await_args_list)
            self.assertIn("16/11/2026 a las 22:00", mensaje)
            self.assertIn("A014819 | PAPEL SCOTT | $990", mensaje)

    async def test_telegram_schedule_requires_elevated_bot(self):
        with patch.object(menu, "_es_administrador", return_value=False), \
             patch.object(menu.subprocess, "run") as tarea:
            ok, detalle = await menu.programar_desde_bot("precios.xlsx", "22:00", False)
        self.assertFalse(ok)
        self.assertIn("administrador", detalle)
        tarea.assert_not_called()

    async def test_bot_collects_excel_time_and_shutdown_before_confirming(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "precios.xlsx"
            ruta.write_bytes(b"excel simulado")
            config = {"token": "test", "chat_id": 10, "user_id": 20}
            programar = AsyncMock(return_value=(True, "Programado correctamente."))
            bot = telegram_bot.Bot(config, AsyncMock(), programar_excel=programar)

            def mensaje(texto):
                return {"message": {"chat": {"id": 10}, "from": {"id": 20}, "text": texto}}

            with patch.object(telegram_bot, "api", new_callable=AsyncMock), \
                 patch.object(telegram_bot, "recibir_excel", new_callable=AsyncMock,
                              return_value=(ruta, 1, [["Código", "RangoInicial1", "RangoFinal1", "Precio1"],
                                                     ["A014819", 1, 9999, 990]])), \
                 patch.object(telegram_bot, "enviar", new_callable=AsyncMock), \
                 patch.object(telegram_bot, "datetime") as reloj:
                reloj.now.return_value = datetime(2026, 11, 16, 12, 0)
                await bot.procesar({"callback_query": {"id": "1", "from": {"id": 20},
                                    "message": {"chat": {"id": 10}}, "data": "excel_programar"}})
                await bot.procesar({"message": {"chat": {"id": 10}, "from": {"id": 20},
                                                "document": {"file_id": "abc", "file_name": "precios.xlsx"}}})
                await bot.procesar(mensaje("22:00"))
                await bot.procesar(mensaje("sí"))
                identificador = bot.excel_pendiente["id"]
                await bot.procesar({"callback_query": {"id": "2", "from": {"id": 20},
                                    "message": {"chat": {"id": 10}},
                                    "data": f"excel_confirmar:{identificador}"}})
            programar.assert_awaited_once_with(ruta, "22:00", True)
            self.assertTrue(ruta.exists())
            self.assertIsNone(bot.excel_pendiente)

    async def test_schedule_confirmation_lists_date_time_code_name_and_clp_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            excel = carpeta / "precios.xlsx"
            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "RangoInicial1", "RangoFinal1", "Precio1",
                         "RangoInicial2", "RangoFinal2", "Precio2"])
            hoja.append(["A014819", 1, 9999, 990, None, None, None])
            hoja.append(["P000631", 1, 9999, 11880, None, None, None])
            libro.save(excel)

            salida = StringIO()
            with patch.object(menu, "_es_administrador", return_value=True), \
                 patch.object(menu, "titulo"), \
                 patch.object(menu, "mostrar_resumen_precios_y_confirmar", return_value=True), \
                 patch.object(menu, "datetime") as reloj, \
                 patch.object(menu, "date") as calendario, \
                 patch.object(menu.sys, "frozen", True, create=True), \
                 patch.object(menu, "_archivo_programado", return_value=carpeta / "excel_programado.txt"), \
                 patch.object(menu, "runtime_path", side_effect=lambda nombre: carpeta / "runtime" / nombre), \
                 patch.object(menu.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0, stderr="", stdout="")), \
                 patch.object(menu.upload_precios, "detalles_productos_para_vista_previa",
                              return_value={"A014819": {"nombre": "PAPEL SCOTT"},
                                            "P000631": {"nombre": "PACK DE PRUEBA"}}), \
                 patch("tkinter.Tk"), \
                 patch("tkinter.filedialog.askopenfilename", return_value=str(excel)), \
                 patch("builtins.input", side_effect=["22:00", "n"]), \
                 patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock,
                              return_value=True) as enviar, redirect_stdout(salida):
                reloj.now.return_value = datetime(2026, 11, 13, 12, 0)
                calendario.today.return_value = date(2026, 11, 13)
                await menu.programar_para_hoy()

            mensaje = "\n".join(call.args[0] for call in enviar.await_args_list)
            self.assertIn("programado (aún no ejecutado)", mensaje)
            self.assertIn("13/11/2026 a las 22:00", mensaje)
            self.assertIn("Precios previstos: 2", mensaje)
            self.assertIn("Al terminar: dejar el PC encendido.", mensaje)
            self.assertIn("A014819 | PAPEL SCOTT | $990", mensaje)
            self.assertIn("P000631 | PACK DE PRUEBA | $11.880", mensaje)
            self.assertNotIn("Código de barras", mensaje)
            self.assertEqual((carpeta / "excel_programado.txt").read_text(encoding="utf-8"), str(excel))

    async def test_schedule_confirmation_reports_shutdown_only_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            excel = carpeta / "precios.xlsx"
            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "RangoInicial1", "RangoFinal1", "Precio1",
                         "RangoInicial2", "RangoFinal2", "Precio2"])
            hoja.append(["A014819", 1, 9999, 990, None, None, None])
            libro.save(excel)

            with patch.object(menu, "_es_administrador", return_value=True), \
                 patch.object(menu, "titulo"), \
                 patch.object(menu, "mostrar_resumen_precios_y_confirmar", return_value=True), \
                 patch.object(menu, "datetime") as reloj, \
                 patch.object(menu, "date") as calendario, \
                 patch.object(menu.sys, "frozen", True, create=True), \
                 patch.object(menu, "_archivo_programado", return_value=carpeta / "excel_programado.txt"), \
                 patch.object(menu, "runtime_path", side_effect=lambda nombre: carpeta / "runtime" / nombre), \
                 patch.object(menu.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0, stderr="", stdout="")) as crear_tarea, \
                 patch("tkinter.Tk"), \
                 patch("tkinter.filedialog.askopenfilename", return_value=str(excel)), \
                 patch("builtins.input", side_effect=["22:00", "s"]), \
                 patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock,
                              return_value=True) as enviar, redirect_stdout(StringIO()):
                reloj.now.return_value = datetime(2026, 11, 13, 12, 0)
                calendario.today.return_value = date(2026, 11, 13)
                await menu.programar_para_hoy()

            mensaje = "\n".join(call.args[0] for call in enviar.await_args_list)
            self.assertIn("Al terminar: apagar el PC solo si todo finaliza sin errores.", mensaje)
            self.assertIn("A014819 |", mensaje)
            self.assertIn("--shutdown", crear_tarea.call_args.args[0][6])

    async def test_no_confirmation_is_sent_when_windows_rejects_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            excel = Path(tmp) / "precios.xlsx"
            excel.write_bytes(b"simulado")
            with patch.object(menu, "_es_administrador", return_value=True), \
                 patch.object(menu, "titulo"), \
                 patch.object(menu, "mostrar_resumen_precios_y_confirmar", return_value=True), \
                 patch.object(menu, "datetime") as reloj, \
                 patch.object(menu, "date") as calendario, \
                 patch.object(menu.sys, "frozen", True, create=True), \
                 patch.object(menu.subprocess, "run",
                              return_value=SimpleNamespace(returncode=1, stderr="falló", stdout="")), \
                 patch("tkinter.Tk"), \
                 patch("tkinter.filedialog.askopenfilename", return_value=str(excel)), \
                 patch("builtins.input", side_effect=["22:00", "n"]), \
                 patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock) as enviar, \
                 redirect_stdout(StringIO()):
                reloj.now.return_value = datetime(2026, 11, 13, 12, 0)
                calendario.today.return_value = date(2026, 11, 13)
                await menu.programar_para_hoy()
            enviar.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
