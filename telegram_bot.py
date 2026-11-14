"""Bot de Telegram que vincula usuarios y les envia el codigo de acceso.

Usa la Bot API de Telegram por HTTPS con urllib (biblioteca estandar),
asi que no hace falta instalar nada mas que Pyro4.

Como se vincula un usuario con su Telegram:
    1. El usuario abre el bot en Telegram y escribe:  /start ian
    2. Telegram guarda ese mensaje; este modulo lo lee con getUpdates.
    3. Se guarda la pareja "ian" -> chat_id en vinculos.json.
    4. Desde ese momento el servidor puede enviarle codigos a "ian".

El bot NO necesita un servidor web: hace "long polling", es decir,
pregunta a Telegram "hay mensajes nuevos?" y espera hasta 30 s por la
respuesta, en un bucle que corre en un hilo aparte.
"""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

# Archivo donde se guardan los vinculos usuario -> chat_id.
ARCHIVO_VINCULOS = "vinculos.json"
URL_API = "https://api.telegram.org/bot{token}/{metodo}"


class BotTelegram(object):
    """Envia mensajes y atiende el comando /start <usuario>."""

    def __init__(self, token, archivo=ARCHIVO_VINCULOS):
        """Guarda el token y carga los vinculos que ya existian."""
        self._token = token
        self._archivo = archivo
        # Lock propio: el hilo del bot escribe vinculos y los hilos de
        # Pyro4 los leen al mismo tiempo.
        self._lock = threading.Lock()
        self._vinculos = self._cargar_vinculos()
        # offset le dice a Telegram que mensajes ya procesamos.
        self._offset = 0

    # ---------------------------------------------------------------
    # Acceso a la API de Telegram
    # ---------------------------------------------------------------
    def _llamar_api(self, metodo, parametros, espera=10):
        """Llama a un metodo de la Bot API y devuelve el campo "result"."""
        url = URL_API.format(token=self._token, metodo=metodo)
        # urlencode convierte {"a": 1} en "a=1"; encode() lo pasa a bytes.
        datos = urllib.parse.urlencode(parametros).encode()
        # "with" cierra la conexion aunque ocurra un error.
        with urllib.request.urlopen(url, data=datos, timeout=espera) as resp:
            respuesta = json.loads(resp.read().decode())
        if not respuesta.get("ok"):
            raise RuntimeError(respuesta.get("description", "error"))
        return respuesta["result"]

    def enviar_mensaje(self, chat_id, texto):
        """Envia un mensaje de texto a un chat."""
        self._llamar_api("sendMessage", {"chat_id": chat_id, "text": texto})

    # ---------------------------------------------------------------
    # Vinculos usuario -> chat_id
    # ---------------------------------------------------------------
    def _cargar_vinculos(self):
        """Lee vinculos.json; si no existe, empieza vacio."""
        if not os.path.exists(self._archivo):
            return {}
        with open(self._archivo, encoding="utf-8") as archivo:
            return json.load(archivo)

    def _guardar_vinculos(self):
        """Escribe los vinculos en disco (se llama con el Lock tomado)."""
        with open(self._archivo, "w", encoding="utf-8") as archivo:
            json.dump(self._vinculos, archivo, indent=2)

    def chat_de(self, usuario):
        """Devuelve el chat_id del usuario o None si no esta vinculado."""
        with self._lock:
            return self._vinculos.get(usuario)

    def _vincular(self, usuario, chat_id):
        """Asocia un usuario a un chat y devuelve el texto de respuesta."""
        with self._lock:
            actual = self._vinculos.get(usuario)
            # Un nombre ya tomado por OTRO chat no se puede robar.
            if actual is not None and actual != chat_id:
                return f"El usuario '{usuario}' ya pertenece a otra cuenta."
            self._vinculos[usuario] = chat_id
            self._guardar_vinculos()
        return (f"Listo. '{usuario}' quedo vinculado a este chat. "
                "Aqui recibiras tus codigos de acceso.")

    # ---------------------------------------------------------------
    # Hilo que escucha los mensajes que la gente le escribe al bot
    # ---------------------------------------------------------------
    def iniciar(self):
        """Arranca el hilo de long polling en segundo plano."""
        # daemon=True: el hilo muere cuando termina el programa principal.
        threading.Thread(target=self._escuchar, daemon=True).start()

    def _escuchar(self):
        """Bucle infinito: pide mensajes nuevos y los procesa."""
        while True:
            try:
                # timeout=30: Telegram espera hasta 30 s si no hay mensajes.
                mensajes = self._llamar_api(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 30},
                    espera=40)
            except (urllib.error.URLError, OSError, RuntimeError) as error:
                print(f"[bot] Error consultando Telegram: {error}")
                threading.Event().wait(5)  # pausa de 5 s antes de reintentar
                continue
            for mensaje in mensajes:
                # Siguiente offset = id del ultimo + 1 (marca como leido).
                self._offset = mensaje["update_id"] + 1
                self._procesar(mensaje)

    def _procesar(self, actualizacion):
        """Atiende /start <usuario>; cualquier otra cosa recibe ayuda."""
        # .get(..., {}) evita KeyError si el mensaje no trae ese campo.
        mensaje = actualizacion.get("message", {})
        texto = mensaje.get("text", "").strip()
        chat_id = mensaje.get("chat", {}).get("id")
        if chat_id is None:
            return
        # split() separa por espacios: "/start ian" -> ["/start", "ian"].
        partes = texto.split()
        if len(partes) == 2 and partes[0] == "/start":
            respuesta = self._vincular(partes[1].lower(), chat_id)
        else:
            respuesta = ("Para vincularte escribe: /start <tu_usuario>\n"
                         "Ejemplo: /start ian")
        try:
            self.enviar_mensaje(chat_id, respuesta)
        except (urllib.error.URLError, OSError, RuntimeError) as error:
            print(f"[bot] No se pudo responder: {error}")


class BotConsola(object):
    """Reemplazo del bot para probar sin Internet ni Telegram.

    Todos los usuarios se consideran vinculados y el codigo se imprime
    en la terminal del servidor. Tiene los mismos metodos publicos que
    BotTelegram (chat_de, enviar_mensaje, iniciar), asi el servidor no
    necesita saber cual de los dos esta usando.
    """

    def iniciar(self):
        """No hay nada que escuchar en modo consola."""
        print("[bot] Modo consola: los codigos se muestran aqui.")

    def chat_de(self, usuario):
        """En modo consola el 'chat' es el propio nombre de usuario."""
        return usuario

    def enviar_mensaje(self, chat_id, texto):
        """Imprime el mensaje en la terminal del servidor."""
        print(f"[bot -> {chat_id}] {texto}")
