import asyncio
import signal
import tempfile
import unittest
import shutil
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch
from openpyxl import Workbook

import menu
import telegram_bot
import trabajo_lock
from utils import ErrorCredenciales, MedidorEtapas


@contextmanager
def libre():
    yield True


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_receives_excel_and_runs_manual_full_cycle_after_confirmation(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        filas = [["Código", "RangoInicial1", "RangoFinal1", "Precio1",
                  "RangoInicial2", "RangoFinal2", "Precio2"],
                 ["A014819", "1", "9999", "990", "", "", ""]]
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "precio.xlsx"
            ruta.write_bytes(b"excel simulado")
            ejecutar = AsyncMock(return_value=True)
            bot = telegram_bot.Bot(config, AsyncMock(), ejecutar_excel=ejecutar)
            with patch.object(telegram_bot, "api", new_callable=AsyncMock), \
                 patch.object(telegram_bot, "recibir_excel", new_callable=AsyncMock,
                              return_value=(ruta, 1, filas)), \
                 patch.object(menu.upload_precios, "detalles_productos_para_vista_previa",
                              return_value={"A014819": {"nombre": "PAPEL SCOTT"}}), \
                 patch.object(telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
                await bot.procesar({"callback_query": {"id": "1", "from": {"id": 20},
                                    "message": {"chat": {"id": 10}}, "data": "excel_completo"}})
                await bot.procesar({"message": {"chat": {"id": 10}, "from": {"id": 20},
                                    "document": {"file_id": "abc", "file_name": "precio.xlsx"}}})
                self.assertIn("PAPEL SCOTT", enviar.await_args.args[0])
                self.assertIn("$990", enviar.await_args.args[0])
                identificador = bot.excel_pendiente["id"]
                await bot.procesar({"callback_query": {"id": "2", "from": {"id": 20},
                                    "message": {"chat": {"id": 10}},
                                    "data": f"excel_confirmar:{identificador}"}})
                await bot.trabajo
                await asyncio.sleep(0)
            ejecutar.assert_awaited_once_with("completo", ruta)
            self.assertFalse(ruta.exists())

    async def test_bot_rejects_stale_excel_confirmation_and_other_chat_document(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        ejecutar = AsyncMock()
        bot = telegram_bot.Bot(config, AsyncMock(), ejecutar_excel=ejecutar)
        with patch.object(telegram_bot, "api", new_callable=AsyncMock), \
             patch.object(telegram_bot, "recibir_excel", new_callable=AsyncMock) as recibir, \
             patch.object(telegram_bot, "enviar", new_callable=AsyncMock):
            await bot.procesar({"message": {"chat": {"id": 99}, "from": {"id": 20},
                                "document": {"file_id": "abc", "file_name": "precio.xlsx"}}})
            await bot.procesar({"callback_query": {"id": "1", "from": {"id": 20},
                                "message": {"chat": {"id": 10}},
                                "data": "excel_confirmar:antiguo"}})
        recibir.assert_not_awaited()
        ejecutar.assert_not_awaited()

    async def test_received_excel_is_validated_before_bot_accepts_it(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        with tempfile.TemporaryDirectory() as tmp:
            fuente = Path(tmp) / "fuente.xlsx"
            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "RangoInicial1", "RangoFinal1", "Precio1",
                         "RangoInicial2", "RangoFinal2", "Precio2"])
            hoja.append(["A014819", 1, 9999, 990, None, None, None])
            libro.save(fuente)

            def descargar(_token, _ruta, destino):
                shutil.copyfile(fuente, destino)

            with patch.object(telegram_bot, "ARCHIVOS_DIR", Path(tmp) / "recibidos"), \
                 patch.object(telegram_bot, "api", new_callable=AsyncMock,
                              return_value={"file_path": "documents/abc.xlsx"}), \
                 patch.object(telegram_bot, "_descargar_archivo_telegram", side_effect=descargar):
                destino, total, filas = await telegram_bot.recibir_excel(
                    config, {"file_id": "abc", "file_name": "precio.xlsx", "file_size": fuente.stat().st_size})
            self.assertTrue(destino.exists())
            self.assertEqual(total, 1)
            self.assertEqual(filas[1][0], "A014819")

    async def test_bot_rejects_non_excel_document_before_download(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        with patch.object(telegram_bot, "api", new_callable=AsyncMock) as api:
            with self.assertRaisesRegex(ValueError, "documento .xlsx"):
                await telegram_bot.recibir_excel(
                    config, {"file_id": "abc", "file_name": "precios.txt", "file_size": 10})
        api.assert_not_awaited()

    async def test_excel_from_bot_uses_selected_file_without_opening_dialog(self):
        ruta = Path("precio_desde_telegram.xlsx")
        with patch.object(menu, "_tiene_upload", True), \
             patch.object(menu.upload_precios, "EXCEL_FORZADO", None), \
             patch.object(menu, "ejecutar_operacion", new_callable=AsyncMock,
                          return_value=True) as ejecutar:
            self.assertTrue(await menu.ejecutar_excel_desde_bot("upload", ruta))
            self.assertIs(ejecutar.await_args.args[1], menu.run_upload)
            self.assertEqual(menu.upload_precios.EXCEL_FORZADO, None)
            self.assertTrue(await menu.ejecutar_excel_desde_bot("completo", ruta))
            self.assertIs(ejecutar.await_args.args[1], menu.run_completo)

    async def test_scheduled_success_lists_all_accepted_prices_with_names(self):
        filas = [(f"A{i:06d}", str(990 + i), "") for i in range(1, 33)]
        resumen = {"total": 32, "ok": 32, "fallidos": [], "filas_precios": filas}

        async def operacion():
            menu._resultados_operacion.append(
                {"nombre": "Upload Precios", "ok": True, "detalle": ""})
            return True

        with patch.object(menu, "trabajo_exclusivo", libre), \
             patch.object(menu, "_recargar_credenciales_para_ejecucion"), \
             patch.object(menu, "_tiene_upload", True), \
             patch.object(menu.upload_precios, "ULTIMO_RESULTADO_PRECIOS", resumen), \
             patch.object(menu.upload_precios, "detalles_productos_para_vista_previa",
                          return_value={"A000001": {"nombre": "Producto de prueba"}}), \
             patch.object(menu.telegram_bot, "guardar_estado"), \
             patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
            self.assertTrue(await menu.ejecutar_operacion(
                "Ciclo programado", operacion, origen="Programado"))
        mensaje = "\n".join(call.args[0] for call in enviar.await_args_list)
        self.assertIn("completado con éxito", mensaje)
        self.assertIn("Se actualizaron 32 precios", mensaje)
        self.assertIn("A000001 | Producto de prueba | $991", mensaje)
        self.assertIn("A000032", mensaje)

    async def test_scheduled_failure_identifies_rejected_price(self):
        resumen = {"total": 3, "ok": 2, "fallidos": ["A000002"],
                   "filas_precios": [("A000001", "990", ""),
                                     ("A000002", "11880", ""),
                                     ("A000003", "1290", "")]}

        async def operacion():
            menu._resultados_operacion.append(
                {"nombre": "Upload Precios", "ok": False,
                 "detalle": "1 código rechazado por Tivendo"})
            return False

        with patch.object(menu, "trabajo_exclusivo", libre), \
             patch.object(menu, "_recargar_credenciales_para_ejecucion"), \
             patch.object(menu, "_tiene_upload", True), \
             patch.object(menu.upload_precios, "ULTIMO_RESULTADO_PRECIOS", resumen), \
             patch.object(menu.upload_precios, "detalles_productos_para_vista_previa",
                          return_value={"A000002": {"nombre": "Producto rechazado"}}), \
             patch.object(menu.telegram_bot, "guardar_estado"), \
             patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
            self.assertFalse(await menu.ejecutar_operacion(
                "Ciclo programado", operacion, origen="Programado"))
        mensaje = "\n".join(call.args[0] for call in enviar.await_args_list)
        self.assertIn("Tivendo aceptó 2 de 3 precios y rechazó 1", mensaje)
        self.assertIn("A000002 | Producto rechazado | $11.880", mensaje)
        self.assertIn("A000001 |", mensaje)
        self.assertIn("A000003 |", mensaje)

    async def test_scheduled_downstream_failure_reports_prices_accepted_by_tivendo(self):
        resumen = {"total": 1, "ok": 1, "fallidos": [],
                   "filas_precios": [("A000001", "990", "")]}

        async def operacion():
            menu._resultados_operacion.extend([
                {"nombre": "Upload Precios", "ok": True, "detalle": ""},
                {"nombre": "Sync Precios", "ok": False, "detalle": "No se pudo subir a MH"},
            ])
            return False

        with patch.object(menu, "trabajo_exclusivo", libre), \
             patch.object(menu, "_recargar_credenciales_para_ejecucion"), \
             patch.object(menu, "_tiene_upload", True), \
             patch.object(menu.upload_precios, "ULTIMO_RESULTADO_PRECIOS", resumen), \
             patch.object(menu.upload_precios, "detalles_productos_para_vista_previa",
                          return_value={}), \
             patch.object(menu.telegram_bot, "guardar_estado"), \
             patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
            self.assertFalse(await menu.ejecutar_operacion(
                "Ciclo programado", operacion, origen="Programado"))
        mensaje = "\n".join(call.args[0] for call in enviar.await_args_list)
        self.assertIn("Tivendo aceptó los precios, pero otra etapa del ciclo falló", mensaje)
        self.assertIn("Se actualizó 1 precio", mensaje)

    async def test_failed_upload_lists_planned_products_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            excel = Path(tmp) / "precios.xlsx"
            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "RangoInicial1", "RangoFinal1", "Precio1",
                         "RangoInicial2", "RangoFinal2", "Precio2"])
            hoja.append(["A014819", 1, 9999, 990, None, None, None])
            libro.save(excel)

            async def operacion():
                menu._resultados_operacion.append(
                    {"nombre": "Upload Precios", "ok": False, "detalle": "Login Tivendo falló"})
                return False

            with patch.object(menu, "trabajo_exclusivo", libre), \
                 patch.object(menu, "_recargar_credenciales_para_ejecucion"), \
                 patch.object(menu, "_tiene_upload", True), \
                 patch.object(menu.upload_precios, "ULTIMO_RESULTADO_PRECIOS", None), \
                 patch.object(menu.upload_precios, "EXCEL_FORZADO", str(excel)), \
                 patch.object(menu.upload_precios, "detalles_productos_para_vista_previa",
                              return_value={"A014819": {"nombre": "PAPEL SCOTT"}}), \
                 patch.object(menu.telegram_bot, "guardar_estado"), \
                 patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
                self.assertFalse(await menu.ejecutar_operacion(
                    "Ciclo programado", operacion, origen="Programado"))
            mensaje = "\n".join(call.args[0] for call in enviar.await_args_list)
            self.assertIn("No se pudo confirmar el cambio", mensaje)
            self.assertIn("A014819 | PAPEL SCOTT | $990", mensaje)

    def test_long_price_list_is_split_without_truncating_rows(self):
        lineas = [f"{i:03d}. A{i:06d} | " + "Producto " * 15 for i in range(100)]
        partes = menu._partir_mensaje_telegram(lineas)
        self.assertGreater(len(partes), 1)
        self.assertTrue(all(len(parte) <= 3900 for parte in partes))
        self.assertIn("A000099", "\n".join(partes))

    async def test_telegram_branch_change_is_visible_in_pc_menu(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(menu._cfg, "CONFIG_FILE", Path(tmp) / "sucursal.json"), \
             patch.object(menu, "trabajo_exclusivo", libre), \
             patch.object(telegram_bot, "api", new_callable=AsyncMock), \
             patch.object(telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
            bot = telegram_bot.Bot(config, AsyncMock(),
                                   cambiar_sucursal=menu.cambiar_sucursal_desde_bot)
            await bot.procesar({"callback_query": {"id": "1", "from": {"id": 20},
                                "message": {"chat": {"id": 10}}, "data": "sucursales"}})
            self.assertIn("Las Compañías", enviar.await_args.args[0])
            await bot.procesar({"callback_query": {"id": "2", "from": {"id": 20},
                                "message": {"chat": {"id": 10}}, "data": "sucursal:CANTERA"}})
            self.assertEqual(menu._cfg.sucursal_activa()["key"], "CANTERA")
            self.assertIn("La Cantera 3055", enviar.await_args.args[0])
            await bot.procesar({"message": {"chat": {"id": 10},
                                "from": {"id": 20}, "text": "/menu"}})
            self.assertIn("Sucursal: La Cantera 3055\nElige qué quieres hacer con MHSync:",
                          enviar.await_args.args[0])
            salida = StringIO()
            with patch.object(menu, "tarea_hoy_info", return_value=""), redirect_stdout(salida):
                menu.banner()
            self.assertIn("Sucursal: La Cantera 3055", salida.getvalue())
            opciones = telegram_bot.teclado_sucursales()["inline_keyboard"]
            self.assertIn("✅ La Cantera 3055", [item["text"] for fila in opciones for item in fila])

    async def test_telegram_rejects_unknown_branch(self):
        with patch.object(menu, "trabajo_exclusivo", libre):
            ok, mensaje = await menu.cambiar_sucursal_desde_bot("INVENTADA")
        self.assertFalse(ok)
        self.assertIn("desconocida", mensaje)

    async def test_telegram_group_buttons_run_menu_options_five_and_six(self):
        teclado = telegram_bot.teclado()["inline_keyboard"]
        callbacks = {boton["callback_data"] for fila in teclado for boton in fila}
        self.assertTrue({"art_precios", "packs_precios", "configuracion"} <= callbacks)
        with patch.object(menu, "ejecutar_operacion", new_callable=AsyncMock) as ejecutar:
            await menu.ejecutar_desde_bot("art_precios", "Sync Artículos + Precios")
            self.assertIs(ejecutar.await_args.args[1], menu.run_solo_sync)
            await menu.ejecutar_desde_bot("packs_precios", "Sync Packs + Precios")
            self.assertIs(ejecutar.await_args.args[1], menu.run_packs_y_precios)

    async def test_telegram_credential_flow_deletes_password_before_verifying(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        eventos = []

        async def api_falsa(_token, metodo, _payload):
            eventos.append(metodo)

        async def actualizar(servicio, correo, clave):
            eventos.append("verificar")
            self.assertEqual((servicio, correo, clave),
                             ("Tivendo", "nuevo@example.com", "ClaveSecreta"))
            return True, "Acceso comprobado"

        bot = telegram_bot.Bot(config, AsyncMock(), actualizar_credenciales=actualizar)
        with patch.object(telegram_bot, "api", side_effect=api_falsa), \
             patch.object(telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
            await bot.procesar({"callback_query": {"id": "1", "from": {"id": 20},
                                "message": {"chat": {"id": 10}}, "data": "cred_tivendo"}})
            await bot.procesar({"message": {"message_id": 2, "chat": {"id": 10},
                                "from": {"id": 20}, "text": "nuevo@example.com"}})
            await bot.procesar({"message": {"message_id": 3, "chat": {"id": 10},
                                "from": {"id": 20}, "text": "ClaveSecreta"}})
        self.assertLess(eventos.index("deleteMessage"), eventos.index("verificar"))
        self.assertIsNone(bot.edicion)
        self.assertNotIn("ClaveSecreta", " ".join(call.args[0] for call in enviar.await_args_list))

    async def test_telegram_does_not_save_password_if_delete_fails(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        actualizar = AsyncMock()
        bot = telegram_bot.Bot(config, AsyncMock(), actualizar_credenciales=actualizar)
        bot.edicion = {"servicio": "Mercadohouse", "correo": "mh@example.com", "etapa": "clave"}
        with patch.object(telegram_bot, "api", new_callable=AsyncMock,
                          side_effect=RuntimeError("No se pudo borrar")), \
             patch.object(telegram_bot, "enviar", new_callable=AsyncMock):
            await bot.procesar({"message": {"message_id": 4, "chat": {"id": 10},
                                "from": {"id": 20}, "text": "secreto"}})
        actualizar.assert_not_awaited()
        self.assertIsNone(bot.edicion)

    async def test_remote_credentials_are_verified_before_replacing_saved_values(self):
        anteriores = {"TIVENDO_EMAIL": "old@tv.cl", "TIVENDO_PASSWORD": "old-tv",
                     "MH_EMAIL": "old@mh.cl", "MH_PASSWORD": "old-mh"}
        with patch.object(menu, "trabajo_exclusivo", libre), \
             patch.object(menu, "verificar_credencial", new_callable=AsyncMock) as verificar, \
             patch("credenciales.cargar_credenciales", return_value=anteriores), \
             patch.object(menu, "_guardar_credenciales") as guardar, \
             patch.object(menu, "borrar_sesion_guardada") as borrar:
            ok, _ = await menu.actualizar_credenciales_desde_bot(
                "Mercadohouse", "nuevo@mh.cl", "nueva-clave")
        self.assertTrue(ok)
        verificar.assert_awaited_once_with("Mercadohouse", "nuevo@mh.cl", "nueva-clave")
        guardar.assert_called_once_with("old@tv.cl", "old-tv", "nuevo@mh.cl", "nueva-clave")
        borrar.assert_called_once()

    async def test_telegram_sync_todo_uses_semantic_command_after_menu_reorder(self):
        self.assertEqual(telegram_bot.COMMANDS["todo"][1], "todo")
        with patch.object(menu, "ejecutar_operacion", new_callable=AsyncMock) as ejecutar:
            await menu.ejecutar_desde_bot("todo", "Sync Todo")
        self.assertIs(ejecutar.await_args.args[1], menu.run_sync_todo)

    async def test_status_button_reports_current_stage_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(telegram_bot, "STATE_FILE", Path(tmp) / "estado.json"):
            telegram_bot.guardar_estado("En curso", tarea="Sync Artículos", origen="Telegram")
            medidor = MedidorEtapas(lambda _mensaje: None, "Artículos")
            with medidor.etapa("Exportar artículos"):
                self.assertEqual(telegram_bot.cargar_estado()["etapa"], "Exportar artículos")
            bot = telegram_bot.Bot({"token": "test", "chat_id": 10, "user_id": 20}, AsyncMock())
            with patch.object(telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
                await bot.procesar({"message": {"chat": {"id": 10}, "from": {"id": 20},
                                                 "text": "estado"}})
            self.assertIn("Artículos — Exportar artículos", enviar.await_args.args[0])
            telegram_bot.guardar_estado("Completado", tarea="Sync Artículos")
            self.assertNotIn("etapa", telegram_bot.cargar_estado())

    async def test_other_process_cannot_overwrite_current_stage(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(telegram_bot, "STATE_FILE", Path(tmp) / "estado.json"):
            telegram_bot.guardar_estado("En curso", tarea="Sync Artículos")
            with patch.object(telegram_bot.os, "getpid", return_value=-1):
                telegram_bot.actualizar_etapa("Packs", "Login Tivendo")
            self.assertNotIn("etapa", telegram_bot.cargar_estado())

    async def test_telegram_submenu_can_activate_and_return_without_stopping_bot(self):
        salida = StringIO()
        with patch.object(menu, "clear"), \
             patch.object(menu, "titulo"), \
             patch.object(menu, "bot_exclusivo", libre), \
             patch.object(menu, "activar_bot_en_segundo_plano", new_callable=AsyncMock) as activar, \
             patch("builtins.input", side_effect=["2", "", "8"]), \
             redirect_stdout(salida):
            await menu.menu_telegram()
        activar.assert_awaited_once()
        self.assertIn("8. Volver a Configuración", salida.getvalue())

    async def test_telegram_menu_groups_accounts_and_routes_bot_controls(self):
        salida = StringIO()
        with patch.object(menu, "clear"), \
             patch.object(menu, "bot_exclusivo", libre), \
             patch.object(menu, "menu_cuentas_telegram", new_callable=AsyncMock) as cuentas, \
             patch.object(menu, "detener_bot", new_callable=AsyncMock) as detener, \
             patch.object(menu, "configurar_inicio_bot") as inicio, \
             patch("builtins.input", side_effect=["1", "", "3", "", "4", "", "8"]), \
             redirect_stdout(salida):
            await menu.menu_telegram()
        cuentas.assert_awaited_once()
        detener.assert_awaited_once()
        inicio.assert_called_once()
        self.assertIn("CUENTAS", salida.getvalue())
        self.assertIn("BOT EN ESTE PC", salida.getvalue())
        self.assertIn("OTROS PC", salida.getvalue())

    async def test_stop_exits_bot_loop_cleanly(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        bot = telegram_bot.Bot(config, AsyncMock())

        async def responder(_token, metodo, *_args, **_kwargs):
            self.assertEqual(metodo, "getUpdates")
            bot.parar()
            return []

        with patch.object(telegram_bot, "enviar", new_callable=AsyncMock), \
             patch.object(telegram_bot, "api", side_effect=responder):
            await bot.servir()
        self.assertTrue(bot.detener.is_set())

    async def test_stop_request_from_menu_ends_background_listener(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        detener = False
        bot = telegram_bot.Bot(config, AsyncMock(), debe_detener=lambda: detener)

        async def responder(_token, metodo, *_args, **_kwargs):
            nonlocal detener
            self.assertEqual(metodo, "getUpdates")
            detener = True
            return []

        with patch.object(telegram_bot, "enviar", new_callable=AsyncMock), \
             patch.object(telegram_bot, "api", side_effect=responder):
            await bot.servir()
        self.assertTrue(detener)

    async def test_direct_bot_mode_restores_signal_handler(self):
        anterior = signal.getsignal(signal.SIGINT)

        class BotFalso:
            def __init__(self, *_args, **_kwargs):
                self.parado = False

            def parar(self):
                self.parado = True

            async def servir(self):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                self.parado = True

        with patch.object(menu.telegram_bot, "cargar_config", return_value={"token": "test"}), \
             patch("credenciales.cargar_credenciales", return_value={"a": "b"}), \
             patch.object(menu, "bot_exclusivo", libre), \
             patch.object(menu.telegram_bot, "registrar_proceso", return_value="test-run"), \
             patch.object(menu.telegram_bot, "limpiar_control"), \
             patch.object(menu.telegram_bot, "Bot", BotFalso):
            await menu.servir_bot()
        self.assertIs(signal.getsignal(signal.SIGINT), anterior)
        self.assertFalse(menu._bot_activo)

    async def test_option_16_starts_background_bot_and_returns_to_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            proceso = type("Proceso", (), {"pid": 123, "poll": lambda self: None})()
            with patch.object(menu.telegram_bot, "cargar_config", return_value={"token": "test"}), \
                 patch("credenciales.cargar_credenciales", return_value={"a": "b"}), \
                 patch.object(menu, "bot_exclusivo", libre), \
                 patch.object(menu.telegram_bot, "LOG_FILE", Path(tmp) / "bot.log"), \
                 patch.object(menu.telegram_bot, "cargar_control", return_value={"pid": 123}), \
                 patch.object(menu.subprocess, "Popen", return_value=proceso) as popen, \
                 patch.object(menu.sys, "frozen", True, create=True):
                ok = await menu.activar_bot_en_segundo_plano()
            self.assertTrue(ok)
            self.assertIn("--bot", popen.call_args.args[0])

    async def test_option_18_requests_graceful_stop(self):
        @contextmanager
        def ocupado():
            yield False

        with patch.object(menu, "bot_exclusivo", side_effect=[ocupado(), libre()]), \
             patch.object(menu.telegram_bot, "cargar_control", return_value={"run_id": "abc"}), \
             patch.object(menu.telegram_bot, "solicitar_detencion") as pedir:
            ok = await menu.detener_bot()
        self.assertTrue(ok)
        pedir.assert_called_once_with("abc")

    async def test_pairing_requires_code_private_chat_and_local_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "telegram_bot.json"
            replies = [
                {"username": "mi_mh_bot"},
                [{"update_id": 42, "message": {"text": "/vincular ABC123",
                                               "chat": {"id": 10, "type": "private"},
                                               "from": {"id": 20, "first_name": "Dueño"}}}],
            ]
            with patch.object(telegram_bot, "CONFIG_FILE", config_path), \
                 patch.object(telegram_bot.secrets, "token_hex", return_value="abc123"), \
                 patch.object(telegram_bot, "api", new_callable=AsyncMock, side_effect=replies), \
                 patch.object(telegram_bot, "copiar_comando_vinculacion", return_value=True) as copiar, \
                 patch.object(telegram_bot, "mostrar_qr_invitacion", return_value=True), \
                 patch.object(telegram_bot, "enviar", new_callable=AsyncMock) as enviar, \
                 patch("builtins.input", return_value="s"):
                ok = await telegram_bot.configurar(lambda _prompt: "token-de-prueba")
            self.assertTrue(ok)
            copiar.assert_called_once_with("ABC123")
            self.assertEqual(telegram_bot.json.loads(config_path.read_text(encoding="utf-8"))["user_id"], 20)
            enviar.assert_awaited_once()

    async def test_pairing_accepts_qr_start_and_cancels_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "telegram_bot.json"
            replies = [{"username": "mi_mh_bot"},
                       [{"update_id": 42, "message": {"text": "/start vincular_ABC123",
                                                      "chat": {"id": 10, "type": "private"},
                                                      "from": {"id": 20}}}]]
            with patch.object(telegram_bot, "CONFIG_FILE", config_path), \
                 patch.object(telegram_bot.secrets, "token_hex", return_value="abc123"), \
                 patch.object(telegram_bot, "api", new_callable=AsyncMock, side_effect=replies), \
                 patch.object(telegram_bot, "copiar_comando_vinculacion", return_value=True), \
                 patch.object(telegram_bot, "mostrar_qr_invitacion", return_value=True), \
                 patch.object(telegram_bot, "enviar", new_callable=AsyncMock), \
                 patch("builtins.input", return_value="s"):
                self.assertTrue(await telegram_bot.configurar(lambda _prompt: "token-de-prueba"))

        with patch.object(telegram_bot, "api", new_callable=AsyncMock,
                          side_effect=[{"username": "mi_mh_bot"}, asyncio.CancelledError]), \
             patch.object(telegram_bot, "copiar_comando_vinculacion", return_value=True), \
             patch.object(telegram_bot, "mostrar_qr_invitacion", return_value=True), \
             redirect_stdout(StringIO()):
            self.assertFalse(await telegram_bot.configurar(lambda _prompt: "token-de-prueba"))

    async def test_rejects_command_from_other_chat(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        ejecutar = AsyncMock()
        bot = telegram_bot.Bot(config, ejecutar)
        mensaje = {"message": {"chat": {"id": 999}, "from": {"id": 20}, "text": "articulos"}}
        with patch.object(telegram_bot, "enviar", new_callable=AsyncMock), \
             patch.object(telegram_bot, "guardar_estado"):
            await bot.procesar(mensaje)
        ejecutar.assert_not_awaited()

    async def test_starts_only_one_job_from_authorized_button(self):
        config = {"token": "test", "chat_id": 10, "user_id": 20}
        release = asyncio.Event()

        async def ejecutar(_opcion, _nombre):
            await release.wait()

        bot = telegram_bot.Bot(config, ejecutar)
        update = {"callback_query": {"id": "cb1", "from": {"id": 20},
                                     "message": {"chat": {"id": 10}}, "data": "articulos"}}
        with patch.object(telegram_bot, "api", new_callable=AsyncMock), \
             patch.object(telegram_bot, "enviar", new_callable=AsyncMock) as enviar, \
             patch.object(telegram_bot, "guardar_estado"):
            await bot.procesar(update)
            first = bot.trabajo
            await bot.procesar(update)
            self.assertIs(bot.trabajo, first)
            self.assertIn("Ya hay una tarea", enviar.await_args.args[0])
            release.set()
            await first

    async def test_partial_result_reports_each_step(self):
        async def operacion():
            menu._resultados_operacion.extend([
                {"nombre": "Artículos", "ok": True, "detalle": ""},
                {"nombre": "Precios", "ok": False, "detalle": "Etapa Exportar: TimeoutError"},
            ])
            return False

        with patch.object(menu, "trabajo_exclusivo", libre), \
             patch.object(menu.telegram_bot, "guardar_estado"), \
             patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
            ok = await menu.ejecutar_operacion("Sync Todo", operacion)
        self.assertFalse(ok)
        mensaje = enviar.await_args.args[0]
        self.assertIn("✅ Artículos", mensaje)
        self.assertIn("❌ Precios — Etapa Exportar: TimeoutError", mensaje)

    async def test_bot_does_not_wait_for_console_on_bad_password(self):
        run = AsyncMock(side_effect=ErrorCredenciales("Tivendo"))
        with patch.object(menu.sys, "argv", ["menu.py", "--bot"]), \
             patch.object(menu, "corregir_contrasena", new_callable=AsyncMock) as corregir:
            ok = await menu._ejecutar_con_reintentos_impl("Sync Artículos", lambda: True, run)
        self.assertFalse(ok)
        corregir.assert_not_awaited()

    async def test_bot_started_from_menu_also_avoids_password_prompt(self):
        run = AsyncMock(side_effect=ErrorCredenciales("Tivendo"))
        with patch.object(menu, "_bot_activo", True), \
             patch.object(menu.sys, "argv", ["menu.py"]), \
             patch.object(menu, "corregir_contrasena", new_callable=AsyncMock) as corregir:
            ok = await menu._ejecutar_con_reintentos_impl("Sync Artículos", lambda: True, run)
        self.assertFalse(ok)
        corregir.assert_not_awaited()

    async def test_partial_upload_is_reported_as_failure_and_excel_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            archivo = Path(tmp) / "precios.xlsx"
            archivo.write_bytes(b"excel")
            anterior = menu._resultados_operacion
            menu._resultados_operacion = [{"nombre": "Upload Precios", "ok": True, "detalle": ""}]
            try:
                with patch.object(menu, "ejecutar_con_reintentos", new_callable=AsyncMock, return_value=True), \
                     patch.object(menu.upload_precios, "ULTIMO_RESULTADO_PRECIOS", {"fallidos": ["A001"]}), \
                     patch.object(menu.upload_precios, "ULTIMO_EXCEL_USADO", str(archivo)):
                    ok = await menu.run_upload()
                self.assertFalse(ok)
                self.assertTrue(archivo.exists())
                self.assertIn("1 código", menu._resultados_operacion[-1]["detalle"])
            finally:
                menu._resultados_operacion = anterior

    async def test_scheduled_cycle_reports_missing_selected_excel_without_running(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("credenciales.cargar_credenciales", return_value={"tv": "a", "tvp": "b", "mh": "c", "mhp": "d"}), \
             patch.object(menu, "_archivo_programado", return_value=Path(tmp) / "excel_programado.txt"), \
             patch.object(menu, "runtime_path", side_effect=lambda name: Path(tmp) / name), \
             patch.object(menu, "run_completo", new_callable=AsyncMock) as completo, \
             patch.object(menu.telegram_bot, "guardar_estado"), \
             patch.object(menu.telegram_bot, "enviar", new_callable=AsyncMock) as enviar:
            await menu.modo_automatico()
        completo.assert_not_awaited()
        self.assertIn("falta el Excel", enviar.await_args.args[0])


class LockTests(unittest.TestCase):
    def test_second_process_slot_cannot_start_same_operation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(trabajo_lock, "USER_DATA_DIR", Path(tmp)):
            with trabajo_lock.trabajo_exclusivo() as first:
                with trabajo_lock.trabajo_exclusivo() as second:
                    self.assertTrue(first)
                    self.assertFalse(second)

    def test_startup_task_uses_current_exe_and_can_be_removed(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(menu.os.environ, {"APPDATA": tmp}), \
             patch.object(menu.sys, "frozen", True, create=True), \
             patch.object(menu.sys, "executable", str(Path(tmp) / "MHSync.exe")), \
             patch.object(menu, "USER_DATA_DIR", Path(tmp) / "datos"), \
             patch.object(menu, "_es_administrador", return_value=True), \
             patch.object(menu.telegram_bot, "cargar_config", return_value={"token": "hidden"}), \
             patch.object(menu.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
                 SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
                 SimpleNamespace(returncode=0)]) as comando, \
             patch("builtins.input", side_effect=["s", "d"]):
            menu.configurar_inicio_bot()
            crear = comando.call_args_list[1].args[0]
            self.assertIn("/IT", crear)
            self.assertEqual(crear[crear.index("/RL") + 1], "HIGHEST")
            self.assertEqual(crear[crear.index("/TR") + 1], f'"{(Path(tmp) / "MHSync.exe").resolve()}" --tray')
            self.assertNotIn("wscript", " ".join(crear).lower())
            menu.configurar_inicio_bot()
            self.assertIn("/Delete", comando.call_args_list[-1].args[0])

    def test_bot_startup_replaces_old_tray_task(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(menu.os.environ, {"APPDATA": tmp}), \
             patch.object(menu.sys, "frozen", True, create=True), \
             patch.object(menu.sys, "executable", str(Path(tmp) / "MHSync.exe")), \
             patch.object(menu, "USER_DATA_DIR", Path(tmp) / "datos"), \
             patch.object(menu, "_es_administrador", return_value=True), \
             patch.object(menu.telegram_bot, "cargar_config", return_value={"token": "test"}), \
             patch.object(menu.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
                 SimpleNamespace(returncode=0)]) as comando, \
             patch("builtins.input", return_value="s"):
            anterior = (Path(tmp) / "Microsoft" / "Windows" / "Start Menu"
                        / "Programs" / "Startup" / "MHSyncTelegramBot.vbs")
            anterior.parent.mkdir(parents=True)
            anterior.write_text("anterior", encoding="utf-8")
            menu.configurar_inicio_bot()
            self.assertFalse(anterior.exists())
            self.assertIn(" --tray", comando.call_args_list[1].args[0][-1])
            self.assertFalse((Path(tmp) / "datos" / "MHSyncBotAdmin.vbs").exists())
            self.assertIn("MHSyncBandeja", comando.call_args_list[2].args[0])

    def test_existing_bot_startup_can_be_upgraded_to_tray(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(menu.os.environ, {"APPDATA": tmp}), \
             patch.object(menu.sys, "frozen", True, create=True), \
             patch.object(menu.sys, "executable", str(Path(tmp) / "MHSync.exe")), \
             patch.object(menu, "USER_DATA_DIR", Path(tmp) / "datos"), \
             patch.object(menu, "_es_administrador", return_value=True), \
             patch.object(menu.telegram_bot, "cargar_config", return_value={"token": "test"}), \
             patch.object(menu.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=0), SimpleNamespace(returncode=0),
                 SimpleNamespace(returncode=1)]) as comando, \
             patch("builtins.input", return_value="a"):
            menu.configurar_inicio_bot()
            self.assertIn(" --tray", comando.call_args_list[1].args[0][-1])
            self.assertIn("/Create", comando.call_args_list[1].args[0])

    def test_startup_reports_task_scheduler_timeout(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(menu.os.environ, {"APPDATA": tmp}), \
             patch.object(menu.sys, "frozen", True, create=True), \
             patch.object(menu.sys, "executable", str(Path(tmp) / "MHSync.exe")), \
             patch.object(menu, "USER_DATA_DIR", Path(tmp) / "datos"), \
             patch.object(menu, "_es_administrador", return_value=True), \
             patch.object(menu.telegram_bot, "cargar_config", return_value={"token": "test"}), \
             patch.object(menu.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=1), menu.subprocess.TimeoutExpired("schtasks", 45)]), \
             patch("builtins.input", return_value="s"), redirect_stdout(StringIO()) as salida:
            menu.configurar_inicio_bot()
        self.assertIn("no respondió en 45 segundos", salida.getvalue())

    def test_tray_starts_bot_once_and_keeps_icon_available(self):
        with patch.object(menu, "_adquirir_instancia_unica", return_value=True), \
             patch.object(menu, "activar_bot_en_segundo_plano", new_callable=AsyncMock) as activar, \
             patch("pystray.Icon") as icono:
            menu.iniciar_bandeja()
        activar.assert_awaited_once()
        icono.return_value.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
