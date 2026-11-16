"""Control remoto y avisos de MHSync mediante un chat privado de Telegram."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.request
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from app_paths import USER_DATA_DIR
from trabajo_lock import configuracion_telegram_exclusiva


CONFIG_FILE = USER_DATA_DIR / "telegram_bot.json"
VINCULACION_FILE = USER_DATA_DIR / "telegram_vinculacion.json"
STATE_FILE = USER_DATA_DIR / "telegram_estado.json"
CONTROL_FILE = USER_DATA_DIR / "telegram_control.json"
STOP_FILE = USER_DATA_DIR / "telegram_detener.json"
LOG_FILE = USER_DATA_DIR / "telegram_bot.log"
ARCHIVOS_DIR = USER_DATA_DIR / "telegram_precios"
MAX_EXCEL_BYTES = 20 * 1024 * 1024
COMMANDS = {
    "articulos": ("Sync Artículos", "articulos"),
    "packs": ("Sync Packs", "packs"),
    "precios": ("Sync Precios", "precios"),
    "todo": ("Sync Todo", "todo"),
    "art_precios": ("Sync Artículos + Precios", "art_precios"),
    "packs_precios": ("Sync Packs + Precios", "packs_precios"),
}


def _guardar_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporal = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporal.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporal.replace(path)


def cargar_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if (data.get("token") and isinstance(data.get("chat_id"), int)
                and (data.get("modo") == "avisos" or isinstance(data.get("user_id"), int))):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def destinatarios(config):
    """Devuelve los chats autorizados, incluidos los de instalaciones antiguas."""
    ids = ([usuario["chat_id"] for usuario in config["usuarios"]]
           if config.get("usuarios") else config.get("chat_ids") or [config["chat_id"]])
    return list(dict.fromkeys(ids))


def usuarios_autorizados(config):
    return config.get("usuarios") or [{"chat_id": config["chat_id"],
                                       "user_id": config["user_id"],
                                       "nombre": config.get("nombre_usuario", "Cuenta principal")}]


def _modificar_config(cambio, alternativa=None):
    """Actualiza la configuración sin pisar cambios hechos por otro proceso."""
    for _ in range(20):
        with configuracion_telegram_exclusiva() as libre:
            if libre:
                actual = cargar_config() or alternativa
                if not actual:
                    return None
                resultado = cambio(actual)
                if resultado is not False:
                    _guardar_json(CONFIG_FILE, actual)
                return actual if resultado is not False else None
        time.sleep(0.05)
    raise OSError("No se pudo bloquear la configuración de Telegram")


def eliminar_cuenta(user_id):
    """Elimina una cuenta; conserva siempre al menos una con acceso."""
    def cambio(config):
        if config.get("modo") == "avisos":
            return False
        usuarios = usuarios_autorizados(config)
        restantes = [u for u in usuarios if u["user_id"] != user_id]
        if len(restantes) == len(usuarios) or not restantes:
            return False
        config["usuarios"] = restantes
        config["chat_ids"] = [u["chat_id"] for u in restantes]
        config["chat_id"] = restantes[0]["chat_id"]
        config["user_id"] = restantes[0]["user_id"]
        config["nombre_usuario"] = restantes[0].get("nombre", "")
        return True

    return _modificar_config(cambio) is not None


def crear_invitacion():
    codigo = secrets.token_hex(4).upper()
    _guardar_json(VINCULACION_FILE, {"codigo": codigo,
                                      "vence": datetime.now().timestamp() + 600})
    return codigo


def mostrar_qr_invitacion(bot_username, codigo):
    """Muestra un enlace de invitación escaneable sin incluir el token del bot."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username or ""):
        return False
    enlace = f"https://t.me/{bot_username}?start=vincular_{codigo}"
    print(f"  Enlace para abrir el bot: {enlace}")
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                           box_size=1, border=2)
        qr.add_data(enlace)
        qr.make(fit=True)
        print("  Escanea este QR con la cámara del celular y pulsa Iniciar:")
        matriz = qr.get_matrix()
        colores = {
            (False, False): "\x1b[97;107m▀",
            (True, True): "\x1b[30;40m▀",
            (True, False): "\x1b[30;107m▀",
            (False, True): "\x1b[97;40m▀",
        }
        for indice in range(0, len(matriz), 2):
            arriba = matriz[indice]
            abajo = matriz[indice + 1] if indice + 1 < len(matriz) else [False] * len(arriba)
            print("  " + "".join(colores[superior, inferior]
                                 for superior, inferior in zip(arriba, abajo)) + "\x1b[0m")
    except ImportError:
        print("  El QR no está disponible en esta instalación; usa el enlace.")
    return True


def copiar_comando_vinculacion(codigo):
    """Deja el comando listo para pegar sin usar Ctrl+C dentro de la consola."""
    if os.name != "nt":
        return False
    try:
        resultado = subprocess.run(
            ["clip.exe"], input=f"/vincular {codigo}", text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return resultado.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def guardar_estado(estado, **datos):
    if estado == "En curso":
        datos.setdefault("pid", os.getpid())
    _guardar_json(STATE_FILE, {"estado": estado, "fecha": datetime.now().isoformat(timespec="seconds"), **datos})


def actualizar_etapa(fuente, etapa):
    """Actualiza el avance consultable sin enviar mensajes adicionales."""
    actual = cargar_estado()
    if actual.get("estado") != "En curso" or actual.get("pid") != os.getpid():
        return
    actual["fuente"] = fuente
    actual["etapa"] = etapa
    actual["etapa_desde"] = datetime.now().strftime("%H:%M:%S")
    try:
        _guardar_json(STATE_FILE, actual)
    except OSError:
        pass  # El seguimiento no debe impedir la tarea principal.


def cargar_estado():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"estado": "Sin ejecuciones registradas"}


def cargar_control():
    try:
        return json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def registrar_proceso(version=None):
    run_id = uuid.uuid4().hex
    _guardar_json(CONTROL_FILE, {"pid": os.getpid(), "run_id": run_id,
                                "version": version})
    return run_id


def solicitar_detencion(run_id):
    _guardar_json(STOP_FILE, {"run_id": run_id})


def detencion_solicitada(run_id):
    try:
        return json.loads(STOP_FILE.read_text(encoding="utf-8")).get("run_id") == run_id
    except (OSError, ValueError):
        return False


def limpiar_control(run_id):
    if cargar_control().get("run_id") == run_id:
        CONTROL_FILE.unlink(missing_ok=True)
    if detencion_solicitada(run_id):
        STOP_FILE.unlink(missing_ok=True)


def _api_sync(token, metodo, payload, timeout=35):
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except Exception:
        # urllib incluye el token en la URL de algunos errores: no propagarlo.
        raise RuntimeError("No se pudo conectar con Telegram; revisa internet y el token") from None
    if not result.get("ok"):
        raise RuntimeError("Telegram rechazó la solicitud; revisa la configuración del bot")
    return result.get("result")


async def api(token, metodo, payload=None, timeout=35):
    return await asyncio.to_thread(_api_sync, token, metodo, payload or {}, timeout)


async def configurar_acceso_menu(token):
    """Deja /menu a mano junto al cuadro de escritura del chat."""
    await api(token, "setMyCommands", {"commands": [
        {"command": "menu", "description": "Abrir opciones de MHSync"},
    ]})
    await api(token, "setChatMenuButton", {"menu_button": {"type": "commands"}})


def teclado():
    return {"inline_keyboard": [
        [{"text": "1. Subir precios (Excel)", "callback_data": "excel_upload"}],
        [{"text": "Artículos", "callback_data": "articulos"},
         {"text": "Packs", "callback_data": "packs"}],
        [{"text": "Precios", "callback_data": "precios"},
         {"text": "Sync todo", "callback_data": "todo"}],
        [{"text": "Artículos + Precios", "callback_data": "art_precios"},
         {"text": "Packs + Precios", "callback_data": "packs_precios"}],
        [{"text": "8. Completo manual (Excel)", "callback_data": "excel_completo"}],
        [{"text": "9. Programar precios (Excel)", "callback_data": "excel_programar"}],
        [{"text": "Ver estado", "callback_data": "estado"}],
        [{"text": "Configuración", "callback_data": "configuracion"}],
    ]}


def teclado_configuracion():
    return {"inline_keyboard": [
        [{"text": "Cambiar sucursal", "callback_data": "sucursales"}],
        [{"text": "Editar acceso Tivendo", "callback_data": "cred_tivendo"}],
        [{"text": "Editar acceso Mercadohouse", "callback_data": "cred_mercadohouse"}],
        [{"text": "Volver", "callback_data": "menu"}],
    ]}


def teclado_sucursales():
    import config as sucursales
    activa = sucursales.sucursal_activa()["key"]
    filas = [[{"text": f"{'✅ ' if key == activa else ''}{data['nombre']}",
               "callback_data": f"sucursal:{key}"}]
             for key, data in sucursales.SUCURSALES.items()]
    filas.append([{"text": "Volver a Configuración", "callback_data": "configuracion"}])
    return {"inline_keyboard": filas}


def teclado_confirmar_excel(identificador):
    return {"inline_keyboard": [
        [{"text": "Confirmar", "callback_data": f"excel_confirmar:{identificador}"},
         {"text": "Cancelar", "callback_data": f"excel_cancelar:{identificador}"}],
    ]}


def texto_menu():
    import config as sucursales
    return (f"Sucursal: {sucursales.sucursal_activa()['nombre']}\n"
            "Elige qué quieres hacer con MHSync:")


async def enviar(texto, config=None, botones=False):
    config = config or cargar_config()
    if not config:
        return False
    prefijo = f"PC: {socket.gethostname()}\n" if not botones else ""
    enviado = False
    error = None
    for chat_id in destinatarios(config):
        payload = {"chat_id": chat_id, "text": (prefijo + texto)[:4096]}
        if botones:
            payload["reply_markup"] = (botones if isinstance(botones, dict)
                                       else teclado_configuracion() if botones == "configuracion"
                                       else teclado_sucursales() if botones == "sucursales"
                                       else teclado())
        for intento in range(3):
            try:
                await api(config["token"], "sendMessage", payload)
                enviado = True
                break
            except RuntimeError as exc:
                error = exc
                if intento < 2:
                    await asyncio.sleep(2)
    if not enviado and error:
        raise error
    return enviado


def _descargar_archivo_telegram(token, ruta_remota, destino):
    """Descarga con límite de tamaño, sin exponer el token en errores o rutas."""
    url = f"https://api.telegram.org/file/bot{token}/{urllib.parse.quote(ruta_remota, safe='/')}"
    try:
        with urllib.request.urlopen(url, timeout=90) as respuesta, destino.open("wb") as salida:
            total = 0
            while bloque := respuesta.read(1024 * 1024):
                total += len(bloque)
                if total > MAX_EXCEL_BYTES:
                    raise ValueError("El Excel supera los 20 MB permitidos por Telegram")
                salida.write(bloque)
    except ValueError:
        destino.unlink(missing_ok=True)
        raise
    except Exception:
        destino.unlink(missing_ok=True)
        raise RuntimeError("No se pudo descargar el Excel desde Telegram") from None


async def recibir_excel(config, documento):
    """Recibe solo un .xlsx del chat vinculado y devuelve ruta local validada."""
    nombre = Path(documento.get("file_name") or "").name
    if not nombre.lower().endswith(".xlsx"):
        raise ValueError("Envía el archivo de precios como documento .xlsx")
    if int(documento.get("file_size") or 0) > MAX_EXCEL_BYTES:
        raise ValueError("El Excel supera los 20 MB permitidos por Telegram")
    info = await api(config["token"], "getFile", {"file_id": documento["file_id"]})
    ruta_remota = (info or {}).get("file_path")
    if not ruta_remota:
        raise ValueError("Telegram no entregó el archivo; vuelve a enviarlo")
    ARCHIVOS_DIR.mkdir(parents=True, exist_ok=True)
    destino = ARCHIVOS_DIR / f"precios_{uuid.uuid4().hex}.xlsx"
    try:
        await asyncio.to_thread(_descargar_archivo_telegram, config["token"], ruta_remota, destino)
        with zipfile.ZipFile(destino) as contenido:
            if sum(item.file_size for item in contenido.infolist()) > 100 * 1024 * 1024:
                raise ValueError("El Excel descomprimido es demasiado grande")
        import upload_precios
        total, filas = upload_precios.resumen_excel_precios(destino)
        if total < 1:
            raise ValueError("El Excel no contiene precios")
        errores = upload_precios.validar_encabezado_precios(filas)
        errores += upload_precios.validar_estructura_precios(filas)
        if errores:
            raise ValueError(f"Excel inválido: {errores[0]}")
        return destino, total, filas
    except Exception:
        destino.unlink(missing_ok=True)
        raise


async def configurar(leer_token):
    print("\n  CONFIGURAR TELEGRAM")
    print("  Crea tu bot con @BotFather y copia su token.")
    try:
        token = leer_token("  Token del bot (no se mostrará): ").strip()
    except KeyboardInterrupt:
        print("\n  Vinculación cancelada.")
        return False
    if not token:
        print("  Cancelado.")
        return False
    try:
        info = await api(token, "getMe", timeout=10)
    except asyncio.CancelledError:
        tarea = asyncio.current_task()
        if tarea:
            tarea.uncancel()
        print("\n  Vinculación cancelada.")
        return False
    except RuntimeError as exc:
        print(f"  {exc}")
        return False
    codigo = secrets.token_hex(3).upper()
    print(f"  Bot comprobado: @{info.get('username', 'sin_nombre')}")
    print(f"  En Telegram, abre ese bot y envíale: /vincular {codigo}")
    if copiar_comando_vinculacion(codigo):
        print("  El comando ya está copiado. Pégalo en Telegram con Ctrl+V.")
    mostrar_qr_invitacion(info.get("username", ""), codigo)
    print("  Esperando el mensaje durante dos minutos...")
    limite = asyncio.get_running_loop().time() + 120
    offset = None
    while asyncio.get_running_loop().time() < limite:
        try:
            params = {"timeout": 3, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            updates = await api(token, "getUpdates", params, timeout=6)
        except asyncio.CancelledError:
            tarea = asyncio.current_task()
            if tarea:
                tarea.uncancel()
            print("\n  Vinculación cancelada.")
            return False
        except RuntimeError as exc:
            print(f"  {exc}")
            return False
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            if message.get("text", "").strip().upper() not in (
                    f"/VINCULAR {codigo}", f"/START VINCULAR_{codigo}"):
                continue
            if chat.get("type") != "private" or not isinstance(sender.get("id"), int):
                continue
            nombre = sender.get("first_name") or "usuario"
            print(f"  Solicitud recibida de {nombre} (ID {sender['id']}).")
            try:
                confirmado = input("  ¿Vincular este chat? (s/n): ").strip().lower() == "s"
            except KeyboardInterrupt:
                confirmado = False
            if not confirmado:
                print("  Cancelado.")
                return False
            config = {"token": token, "chat_id": chat["id"], "user_id": sender["id"],
                      "nombre_usuario": nombre,
                      "offset": offset, "bot_username": info.get("username", "")}
            _guardar_json(CONFIG_FILE, config)
            await enviar("MHSync vinculado.\n" + texto_menu(), config, botones=True)
            print("  Bot vinculado. El token queda guardado solo en este equipo.")
            return True
    print("  Se agotó el tiempo. Vuelve a configurar cuando quieras.")
    return False


async def configurar_solo_avisos(leer_token, leer_id):
    """Vincula un PC adicional para enviar avisos sin consumir órdenes del bot."""
    print("\n  ESTE PC SOLO ENVIARÁ AVISOS")
    print("  En el PC que controla el bot, consulta el ID del chat en Telegram.")
    token = leer_token("  Token del mismo bot (no se mostrará): ").strip()
    if not token:
        print("  Cancelado.")
        return False
    chat_texto = leer_id("  ID de cada chat autorizado (separados por coma): ").strip()
    partes = [parte.strip() for parte in chat_texto.split(",")]
    if not partes or not all(parte.isdecimal() for parte in partes):
        print("  Cada ID del chat debe ser un número.")
        return False
    chat_ids = list(dict.fromkeys(int(parte) for parte in partes))
    try:
        await api(token, "getMe")
        for chat_id in chat_ids:
            chat = await api(token, "getChat", {"chat_id": chat_id})
            if chat.get("type") != "private":
                print("  Cada ID debe corresponder a un chat privado con el bot.")
                return False
        config = {"token": token, "chat_id": chat_ids[0],
                  "chat_ids": chat_ids, "modo": "avisos"}
        for chat_id in chat_ids:
            await enviar("✅ MHSync conectado para enviar avisos desde este PC.",
                         {"token": token, "chat_id": chat_id})
    except RuntimeError as exc:
        print(f"  {exc}")
        return False
    _guardar_json(CONFIG_FILE, config)
    print("  Listo. Este PC avisará al programar y al terminar; no recibirá órdenes del bot.")
    return True


class Bot:
    def __init__(self, config, ejecutar, debe_detener=None,
                 actualizar_credenciales=None, cambiar_sucursal=None, ejecutar_excel=None,
                 programar_excel=None):
        self.config = config
        self.ejecutar = ejecutar
        self.offset = config.get("offset")
        self.trabajo = None
        self.detener = asyncio.Event()
        self.debe_detener = debe_detener or (lambda: False)
        self.actualizar_credenciales = actualizar_credenciales
        self.cambiar_sucursal = cambiar_sucursal
        self.ejecutar_excel = ejecutar_excel
        self.programar_excel = programar_excel
        self.edicion = None
        self.excel_pendiente = None

    def parar(self):
        self.detener.set()

    async def _respuesta(self, texto, botones=False):
        await enviar(texto, self.config, botones)

    async def _iniciar(self, clave):
        if self.trabajo and not self.trabajo.done():
            await self._respuesta("Ya hay una tarea en curso. Usa Estado para consultarla.")
            return
        nombre, comando = COMMANDS[clave]
        await self._respuesta(f"Orden recibida: {nombre}. Te avisaré al terminar.")
        self.trabajo = asyncio.create_task(self.ejecutar(comando, nombre))

        def terminado(tarea):
            try:
                tarea.result()
            except asyncio.CancelledError:
                guardar_estado("Cancelado", tarea=nombre, detalle="La ejecución remota se interrumpió")
            except Exception as exc:
                guardar_estado("Falló", tarea=nombre, detalle=type(exc).__name__)
                asyncio.create_task(self._respuesta(f"❌ {nombre}: falló. Revisa el PC o el registro."))

        self.trabajo.add_done_callback(terminado)

    def _cancelar_excel(self):
        if self.excel_pendiente and self.excel_pendiente.get("ruta"):
            Path(self.excel_pendiente["ruta"]).unlink(missing_ok=True)
        self.excel_pendiente = None

    async def _recibir_excel(self, documento):
        if not self.excel_pendiente or self.excel_pendiente["etapa"] != "documento":
            await self._respuesta("Primero elige Subir precios, Completo manual o Programar precios en /menu.")
            return
        try:
            ruta, total, filas = await recibir_excel(self.config, documento)
        except Exception as error:
            mensaje = str(error) if isinstance(error, (ValueError, RuntimeError)) else "No se pudo leer el Excel. Vuelve a enviarlo."
            await self._respuesta(f"❌ {mensaje}")
            return
        import upload_precios
        codigos = {str(fila[0]).strip().upper() for fila in filas[1:] if fila and fila[0]}
        detalles = upload_precios.detalles_productos_para_vista_previa(codigos)
        import config as sucursales
        sucursal = sucursales.sucursal_activa()
        modo = self.excel_pendiente["modo"]
        self.excel_pendiente.update({"ruta": ruta, "total": total,
                                    "etapa": "hora" if modo == "programar" else "confirmar",
                                    "id": secrets.token_hex(4), "sucursal": sucursal["key"]})
        accion = ("subir precios a Tivendo" if modo == "upload" else
                  "programar el ciclo completo 1→2→4→3" if modo == "programar" else
                  "ejecutar el ciclo completo 1→2→4→3")
        lineas = [f"Archivo: {Path(documento.get('file_name') or '').name}",
                  f"Sucursal: {sucursal['nombre']}",
                  f"Precios: {total}", f"Acción: {accion}", "", "Vista previa:"]
        for indice, fila in enumerate(filas[1:11], 1):
            codigo = str(fila[0]).strip()
            nombre = detalles.get(codigo.upper(), {}).get("nombre") or "Nombre no disponible"
            precio = upload_precios.formatear_precio_clp(fila[3])
            lineas.append(f"{indice:02d}. {codigo} | {nombre[:70]} | {precio}")
        if total > 10:
            lineas.append(f"... y {total - 10} precio(s) más en el Excel.")
        if modo == "programar":
            lineas.append("¿A qué hora de hoy? Escribe HH:MM (por ejemplo 21:40) o /cancelar.")
            await self._respuesta("\n".join(lineas))
        else:
            lineas.append("Confirma para empezar ahora o cancela.")
            await self._respuesta("\n".join(lineas),
                                 botones=teclado_confirmar_excel(self.excel_pendiente["id"]))

    async def _procesar_programacion(self, texto):
        pendiente = self.excel_pendiente
        if pendiente["etapa"] == "hora":
            import re
            if not re.fullmatch(r"\d{1,2}:\d{2}", texto):
                await self._respuesta("Escribe una hora válida HH:MM para hoy, por ejemplo 21:40.")
                return
            horas, minutos = map(int, texto.split(":"))
            if horas > 23 or minutos > 59:
                await self._respuesta("Esa hora no existe. Escribe HH:MM.")
                return
            ahora = datetime.now()
            elegido = ahora.replace(hour=horas, minute=minutos, second=0, microsecond=0)
            if elegido <= ahora:
                await self._respuesta("Esa hora ya pasó hoy. Elige una hora futura.")
                return
            pendiente["hora"] = f"{horas:02d}:{minutos:02d}"
            pendiente["etapa"] = "apagado"
            await self._respuesta("¿Apagar el PC al terminar si todo sale bien? Responde sí o no.")
            return
        respuesta = texto.casefold()
        if respuesta not in ("si", "sí", "s", "no", "n"):
            await self._respuesta("Responde sí o no: ¿apagar el PC al terminar sin errores?")
            return
        pendiente["apagar"] = respuesta in ("si", "sí", "s")
        pendiente["etapa"] = "confirmar"
        import config as sucursales
        await self._respuesta(
            f"Confirma el cambio de {pendiente['total']} precios para hoy a las {pendiente['hora']} "
            f"en {sucursales.sucursal_activa()['nombre']}. "
            f"Al terminar: {'apagar el PC si no hay errores' if pendiente['apagar'] else 'dejar el PC encendido'}. "
            "La programación anterior de este PC será reemplazada.",
            botones=teclado_confirmar_excel(pendiente["id"]),
        )

    async def _confirmar_excel(self, identificador):
        pendiente = self.excel_pendiente
        if not pendiente or pendiente.get("id") != identificador or pendiente["etapa"] != "confirmar":
            await self._respuesta("Esta confirmación ya no está activa. Elige de nuevo en /menu.")
            return
        if self.trabajo and not self.trabajo.done():
            await self._respuesta("Ya hay una tarea en curso. Espera a que termine.")
            return
        if pendiente["modo"] == "programar" and not self.programar_excel:
            await self._respuesta("Programar desde Telegram no está disponible en este PC.")
            return
        if pendiente["modo"] != "programar" and not self.ejecutar_excel:
            await self._respuesta("La subida de Excel no está disponible en este bot.")
            return
        import config as sucursales
        if sucursales.sucursal_activa()["key"] != pendiente["sucursal"]:
            self._cancelar_excel()
            await self._respuesta("La sucursal cambió después de revisar el Excel. Elige de nuevo la opción y envíalo otra vez.")
            return
        modo, ruta = pendiente["modo"], pendiente["ruta"]
        if modo == "programar":
            try:
                ok, detalle = await self.programar_excel(ruta, pendiente["hora"], pendiente["apagar"])
            except Exception:
                ok, detalle = False, "No pude crear la programación. Revisa el PC y vuelve a confirmar."
            if ok:
                self.excel_pendiente = None  # el Excel queda guardado hasta la ejecución
                if detalle != "Programado correctamente.":
                    await self._respuesta(f"✅ {detalle}")
            else:
                await self._respuesta(f"❌ {detalle}")
            return
        self.excel_pendiente = None
        await self._respuesta("Orden recibida. Iniciando con el Excel enviado; te avisaré al terminar.")
        self.trabajo = asyncio.create_task(self.ejecutar_excel(modo, ruta))

        def terminado(tarea):
            try:
                resultado = tarea.result()
                if resultado:
                    Path(ruta).unlink(missing_ok=True)
            except asyncio.CancelledError:
                asyncio.create_task(self._respuesta("❌ La operación con el Excel fue cancelada."))
            except Exception:
                asyncio.create_task(self._respuesta("❌ La operación con el Excel falló. Revisa el registro del PC."))

        self.trabajo.add_done_callback(terminado)

    async def _procesar_credencial(self, message):
        texto = (message.get("text") or "").strip()
        if self.edicion["etapa"] == "correo":
            if texto.lower() == "/igual":
                import credenciales
                prefijo = "TIVENDO" if self.edicion["servicio"] == "Tivendo" else "MH"
                texto = credenciales.cargar_credenciales()[prefijo + "_EMAIL"]
            if not texto or "@" not in texto or any(c.isspace() for c in texto) or len(texto) > 254:
                await self._respuesta("Escribe un correo válido o usa /cancelar.")
                return
            self.edicion["correo"] = texto
            self.edicion["etapa"] = "clave"
            await self._respuesta("Escribe la nueva contraseña. Telegram recibirá el mensaje; intentaré borrarlo antes de comprobar el acceso. Usa /cancelar para salir.")
            return

        # Telegram recibe la clave como mensaje. Si no se puede borrar, no se guarda.
        try:
            await api(self.config["token"], "deleteMessage", {
                "chat_id": message["chat"]["id"], "message_id": message["message_id"]})
        except (KeyError, RuntimeError):
            self.edicion = None
            await self._respuesta("No pude borrar la contraseña del chat. No la guardé; elimínala manualmente y vuelve a intentarlo.")
            return
        if not texto:
            await self._respuesta("La contraseña está vacía. Escribe otra o usa /cancelar.")
            return
        servicio, correo = self.edicion["servicio"], self.edicion["correo"]
        self.edicion = None
        await self._respuesta(f"Comprobando acceso a {servicio}...")
        try:
            ok, detalle = await self.actualizar_credenciales(servicio, correo, texto)
        except Exception:
            ok, detalle = False, "No se pudo comprobar el acceso. Las credenciales anteriores siguen guardadas."
        await self._respuesta(("✅ " if ok else "❌ ") + detalle, botones="configuracion")

    async def procesar(self, update):
        actual = cargar_config()
        if actual and actual["token"] == self.config["token"]:
            self.config.clear()
            self.config.update(actual)
        message = update.get("message") or {}
        callback = update.get("callback_query") or {}
        origen = callback.get("message") or message
        chat = origen.get("chat") or {}
        sender = callback.get("from") or message.get("from") or {}
        autorizados = usuarios_autorizados(self.config)
        autorizado = any(chat.get("id") == usuario["chat_id"]
                         and sender.get("id") == usuario["user_id"]
                         for usuario in autorizados)
        if not autorizado:
            if not callback and chat.get("type") == "private":
                await self._vincular_invitado(message, chat, sender)
            return  # Ninguna otra cuenta puede ejecutar Sync.
        if callback:
            await api(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback["id"]})
        texto = (callback.get("data") or message.get("text") or "").strip()
        clave = texto.lower().split("@")[0]
        if clave.startswith("/"):
            clave = clave[1:]
        if message.get("document"):
            if self.edicion:
                await self._respuesta("Termina o cancela la edición de credenciales antes de enviar un Excel.")
            else:
                await self._recibir_excel(message["document"])
            return
        if (self.excel_pendiente and self.excel_pendiente.get("modo") == "programar"
                and self.excel_pendiente.get("etapa") in ("hora", "apagado")
                and not callback and clave not in ("cancelar", "menu")):
            await self._procesar_programacion(texto)
            return
        if self.excel_pendiente and not callback and clave not in ("cancelar", "menu"):
            await self._respuesta("Envía el Excel como documento .xlsx o usa /cancelar.")
            return
        if self.edicion and not callback and clave not in ("cancelar", "menu"):
            await self._procesar_credencial(message)
            return
        if clave in ("cancelar", "menu"):
            self.edicion = None
            self._cancelar_excel()
        elif callback and clave in COMMANDS:
            self.edicion = None
            self._cancelar_excel()
        if clave in ("start", "menu", "ayuda"):
            await self._respuesta(texto_menu(), botones=True)
        elif clave == "cancelar":
            await self._respuesta("Cancelado.", botones=True)
        elif clave in ("excel_upload", "excel_completo", "excel_programar"):
            if clave == "excel_programar" and not self.programar_excel:
                await self._respuesta("Programar desde Telegram no está disponible en este PC.")
            elif clave != "excel_programar" and not self.ejecutar_excel:
                await self._respuesta("La subida de Excel no está disponible en este bot.")
            elif self.trabajo and not self.trabajo.done():
                await self._respuesta("Hay una tarea en curso. Espera a que termine.")
            else:
                self.edicion = None
                self._cancelar_excel()
                modo = ("upload" if clave == "excel_upload" else
                        "programar" if clave == "excel_programar" else "completo")
                self.excel_pendiente = {"modo": modo, "etapa": "documento"}
                await self._respuesta("Envía ahora el Excel de precios como documento .xlsx (máximo 20 MB). Lo revisaré antes de continuar.")
        elif clave.startswith("excel_confirmar:"):
            await self._confirmar_excel(clave.partition(":")[2])
        elif clave.startswith("excel_cancelar:"):
            if self.excel_pendiente and self.excel_pendiente.get("id") == clave.partition(":")[2]:
                self._cancelar_excel()
                await self._respuesta("Excel descartado. No se inició ninguna sincronización.", botones=True)
            else:
                await self._respuesta("Esta confirmación ya no está activa.")
        elif clave == "configuracion":
            self.edicion = None
            await self._respuesta("Configuración de MHSync:", botones="configuracion")
        elif clave == "sucursales":
            import config as sucursales
            await self._respuesta(f"Sucursal actual: {sucursales.sucursal_activa()['nombre']}. Elige una:",
                                 botones="sucursales")
        elif clave.startswith("sucursal:"):
            if not self.cambiar_sucursal:
                await self._respuesta("El cambio de sucursal no está disponible en este bot.")
            elif self.trabajo and not self.trabajo.done():
                await self._respuesta("Espera a que termine la sincronización antes de cambiar de sucursal.")
            else:
                try:
                    ok, detalle = await self.cambiar_sucursal(clave.partition(":")[2].upper())
                except Exception:
                    ok, detalle = False, "No se pudo cambiar la sucursal en este PC."
                await self._respuesta(("✅ " if ok else "❌ ") + detalle, botones="sucursales")
        elif clave in ("cred_tivendo", "cred_mercadohouse"):
            if not self.actualizar_credenciales:
                await self._respuesta("La edición de credenciales no está disponible en este bot.")
            elif self.trabajo and not self.trabajo.done():
                await self._respuesta("Espera a que termine la sincronización antes de editar accesos.")
            else:
                servicio = "Tivendo" if clave == "cred_tivendo" else "Mercadohouse"
                self.edicion = {"servicio": servicio, "etapa": "correo"}
                await self._respuesta(f"Nuevo correo de {servicio}, o /igual para conservar el actual. Después te pediré la contraseña. Usa /cancelar para salir.")
        elif clave == "estado":
            estado = cargar_estado()
            lineas = [f"Estado: {estado.get('estado')}", f"Tarea: {estado.get('tarea', '-')}"]
            if estado.get("estado") == "En curso" and estado.get("etapa"):
                lineas.append(f"Paso actual: {estado.get('fuente', '-')} — {estado['etapa']}")
                lineas.append(f"Desde: {estado.get('etapa_desde', '-')}")
            else:
                lineas.append(f"Hora: {estado.get('fecha', '-')}")
                if estado.get("detalle"):
                    lineas.append(estado["detalle"])
            await self._respuesta("\n".join(lineas), botones=True)
        elif clave in COMMANDS:
            await self._iniciar(clave)

    async def _vincular_invitado(self, message, chat, sender):
        texto = message.get("text", "").strip().upper()
        if texto.startswith("/START VINCULAR_"):
            texto = "/VINCULAR " + texto[len("/START VINCULAR_"):]
        if not texto.startswith("/VINCULAR "):
            return
        try:
            invitacion = json.loads(VINCULACION_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if (texto != f"/VINCULAR {invitacion.get('codigo')}"
                or datetime.now().timestamp() > invitacion.get("vence", 0)
                or not isinstance(sender.get("id"), int)):
            return
        def cambio(config):
            usuarios = list(usuarios_autorizados(config))
            if any(u["user_id"] == sender["id"] for u in usuarios):
                return False
            usuarios.append({"chat_id": chat["id"], "user_id": sender["id"],
                             "nombre": sender.get("first_name") or "Cuenta sin nombre"})
            config["usuarios"] = usuarios
            config["chat_ids"] = [usuario["chat_id"] for usuario in usuarios]
            return True

        actual = _modificar_config(cambio, self.config)
        if not actual:
            return
        if actual is not self.config:
            self.config.clear()
            self.config.update(actual)
        VINCULACION_FILE.unlink(missing_ok=True)
        await enviar("✅ Cuenta vinculada. Ya puede usar las opciones de MHSync.",
                     self.config, botones=True)

    async def servir(self):
        print("  Bot de Telegram activo.")
        try:
            await configurar_acceso_menu(self.config["token"])
        except RuntimeError:
            print("  No se pudo configurar el botón de menú; se reintentará en el próximo inicio.")
        try:
            await self._respuesta("MHSync está conectado.\n" + texto_menu(), botones=True)
        except RuntimeError:
            print("  Telegram no está disponible todavía; se reintentará la conexión.")
        try:
            while not self.detener.is_set() and not self.debe_detener():
                try:
                    params = {"timeout": 3, "allowed_updates": ["message", "callback_query"]}
                    if self.offset is not None:
                        params["offset"] = self.offset
                    updates = await api(self.config["token"], "getUpdates", params, timeout=8)
                    for update in updates:
                        if self.detener.is_set() or self.debe_detener():
                            break
                        self.offset = update["update_id"] + 1
                        def guardar_offset(config):
                            config["offset"] = self.offset
                            return True

                        actual = _modificar_config(guardar_offset, self.config)
                        if actual and actual is not self.config:
                            self.config.clear()
                            self.config.update(actual)
                        await self.procesar(update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"  Telegram: {type(exc).__name__}; reintentando conexión...")
                    try:
                        await asyncio.wait_for(self.detener.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
        finally:
            if self.trabajo and not self.trabajo.done():
                print("  Esperando que termine la sincronización antes de cerrar el bot...")
                await asyncio.gather(self.trabajo, return_exceptions=True)
            print("  Bot de Telegram detenido.")
