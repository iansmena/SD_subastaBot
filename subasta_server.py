"""Servidor de subastas con Pyro4 y login por codigo de Telegram (OTP).

Reglas de la subasta:
    - Un solo objeto, con precio base.
    - Cada puja debe ser EXACTAMENTE el precio actual + $10.
    - Cronometro de 10 s que se reinicia con cada puja. Si se cumple sin
      pujas nuevas, el objeto se adjudica al ultimo que pujo.

Login:
    - El cliente escribe su usuario.
    - El servidor genera un codigo de 6 digitos y lo envia por Telegram.
    - El cliente escribe ese codigo y recibe un token de sesion.
    - Todas las pujas se hacen con el token, no con el nombre.

Ejecucion (con el Name Server ya iniciado):
    export TELEGRAM_BOT_TOKEN="123456:ABC..."     # token de @BotFather
    python3 subasta_server.py --producto "Laptop" --precio-base 100
    python3 subasta_server.py --sin-telegram      # prueba sin Telegram
"""

import argparse
import hmac
import os
import queue
import secrets
import threading
import time
import urllib.error

import Pyro4
import Pyro4.errors

from telegram_bot import BotConsola, BotTelegram

NOMBRE_SERVICIO = "subastas.telegram"
INCREMENTO = 10.0             # la puja debe ser precio actual + $10
SEGUNDOS_CRONOMETRO = 10      # tiempo sin pujas para adjudicar
VIGENCIA_CODIGO = 60          # segundos que dura un codigo
INTENTOS_CODIGO = 3           # intentos antes de invalidar el codigo
ESPERA_REENVIO = 30           # segundos minimos entre dos codigos
TIEMPO_ESPERA_CALLBACK = 3    # timeout al avisar a un cliente


class AutenticadorOTP(object):
    """Genera, envia y verifica codigos de un solo uso (OTP).

    No es un objeto Pyro: vive dentro del servidor y ServicioSubasta lo
    usa. Asi el cliente nunca puede ver los codigos ni las sesiones.
    """

    def __init__(self, bot):
        """Recibe el bot (Telegram o consola) que entrega los codigos."""
        self._bot = bot
        self._lock = threading.Lock()
        # usuario -> {"codigo", "vence", "intentos", "enviado"}
        self._pendientes = {}
        # token -> usuario
        self._sesiones = {}

    def solicitar_codigo(self, usuario):
        """Crea un codigo para el usuario y se lo envia por Telegram."""
        chat_id = self._bot.chat_de(usuario)
        if chat_id is None:
            raise PermissionError(
                f"'{usuario}' no esta vinculado. Abre el bot en Telegram "
                f"y escribe: /start {usuario}")

        ahora = time.time()
        with self._lock:
            previo = self._pendientes.get(usuario)
            # Evita que alguien pida codigos sin parar (spam al usuario).
            if previo and ahora - previo["enviado"] < ESPERA_REENVIO:
                falta = int(ESPERA_REENVIO - (ahora - previo["enviado"]))
                raise PermissionError(
                    f"Ya se envio un codigo. Espera {falta} s para otro.")
            # secrets (no random) genera numeros seguros para claves.
            # :06d rellena con ceros a la izquierda: 42 -> "000042".
            codigo = f"{secrets.randbelow(10**6):06d}"
            self._pendientes[usuario] = {
                "codigo": codigo,
                "vence": ahora + VIGENCIA_CODIGO,
                "intentos": INTENTOS_CODIGO,
                "enviado": ahora,
            }

        # El envio por red se hace FUERA del Lock: si Telegram tarda,
        # los demas usuarios no quedan bloqueados esperando.
        texto = (f"Tu codigo para la subasta es: {codigo}\n"
                 f"Vence en {VIGENCIA_CODIGO} segundos. No lo compartas.")
        try:
            self._bot.enviar_mensaje(chat_id, texto)
        except (urllib.error.URLError, OSError, RuntimeError) as error:
            with self._lock:
                self._pendientes.pop(usuario, None)
            print(f"[auth] Fallo el envio a {usuario}: {error}")
            raise PermissionError(
                "No se pudo enviar el codigo por Telegram.")
        print(f"[auth] Codigo enviado a {usuario}.")
        return VIGENCIA_CODIGO

    def verificar(self, usuario, codigo):
        """Comprueba el codigo y, si es correcto, devuelve un token nuevo."""
        with self._lock:
            pendiente = self._pendientes.get(usuario)
            if pendiente is None:
                raise PermissionError(
                    "No hay un codigo activo. Pide uno nuevo.")
            if time.time() > pendiente["vence"]:
                del self._pendientes[usuario]
                raise PermissionError("El codigo vencio. Pide uno nuevo.")
            # compare_digest compara en tiempo constante (evita ataques
            # que miden cuanto tarda la comparacion).
            if not hmac.compare_digest(pendiente["codigo"], str(codigo)):
                pendiente["intentos"] -= 1
                if pendiente["intentos"] <= 0:
                    del self._pendientes[usuario]
                    raise PermissionError(
                        "Demasiados intentos. Pide un codigo nuevo.")
                raise PermissionError(
                    f"Codigo incorrecto. Te quedan {pendiente['intentos']} "
                    "intentos.")
            # Un solo uso: el codigo se borra apenas se acepta.
            del self._pendientes[usuario]
            token = secrets.token_urlsafe(16)
            self._sesiones[token] = usuario
        print(f"[auth] {usuario} inicio sesion.")
        return token

    def usuario_de(self, token):
        """Devuelve el usuario dueno del token o lanza PermissionError."""
        with self._lock:
            usuario = self._sesiones.get(token)
        if usuario is None:
            raise PermissionError("Sesion invalida. Inicia sesion.")
        return usuario

    def cerrar(self, token):
        """Elimina una sesion."""
        with self._lock:
            return self._sesiones.pop(token, None)


@Pyro4.behavior(instance_mode="single")
class ServicioSubasta(object):
    """Subasta de un objeto compartida por todos los clientes.

    instance_mode="single": una sola instancia para todos, porque todos
    deben ver el mismo precio. Pyro4 atiende cada llamada en un hilo
    distinto, por eso el estado se protege con un Lock.

    Solo los metodos marcados con @Pyro4.expose se pueden llamar desde
    el cliente. iniciar_subasta() no esta expuesto: solo el servidor
    decide cuando empieza.
    """

    def __init__(self, producto, precio_base, autenticador):
        """Prepara la subasta (todavia cerrada) y el hilo notificador."""
        self._lock = threading.Lock()
        self._auth = autenticador
        self._producto = producto
        self._precio_base = float(precio_base)
        self._precio_actual = float(precio_base)
        self._ganador = None          # ultimo que pujo
        self._estado = "esperando"    # esperando -> abierta -> finalizada
        self._clientes = {}           # usuario -> proxy del receptor
        self._cronometro = None       # threading.Timer activo
        self._ronda = 0               # numero del cronometro vigente
        self._vence = 0.0             # hora en que vence el cronometro
        self._eventos = queue.Queue()
        self.finalizada = threading.Event()
        threading.Thread(target=self._notificar_clientes,
                         daemon=True).start()

    # ---------------------------------------------------------------
    # Login (expuesto al cliente)
    # ---------------------------------------------------------------
    @Pyro4.expose
    def solicitar_codigo(self, usuario):
        """Envia un codigo al Telegram del usuario; devuelve su vigencia."""
        return self._auth.solicitar_codigo(self._normalizar(usuario))

    @Pyro4.expose
    def iniciar_sesion(self, usuario, codigo):
        """Verifica el codigo y devuelve el token de sesion."""
        return self._auth.verificar(self._normalizar(usuario), codigo)

    @Pyro4.expose
    def cerrar_sesion(self, token):
        """Cierra la sesion y deja de enviar avisos a ese cliente."""
        usuario = self._auth.cerrar(token)
        if usuario:
            with self._lock:
                self._clientes.pop(usuario, None)
            print(f"[subasta] {usuario} salio.")

    @staticmethod
    def _normalizar(usuario):
        """Valida el nombre y lo pasa a minusculas ("Ian" == "ian")."""
        if not isinstance(usuario, str) or not usuario.strip():
            raise PermissionError("El usuario no puede estar vacio.")
        return usuario.strip().lower()

    # ---------------------------------------------------------------
    # Subasta (expuesto al cliente)
    # ---------------------------------------------------------------
    @Pyro4.expose
    def registrar_receptor(self, token, receptor):
        """Guarda el callback del cliente y devuelve el estado actual."""
        usuario = self._auth.usuario_de(token)
        # Si el cliente no responde en 3 s, la llamada falla en vez de
        # colgar al hilo notificador.
        receptor._pyroTimeout = TIEMPO_ESPERA_CALLBACK
        with self._lock:
            self._clientes[usuario] = receptor
            print(f"[subasta] {usuario} se unio "
                  f"({len(self._clientes)} conectados).")
            return self._estado_actual()

    @Pyro4.expose
    def pujar(self, token, monto):
        """Acepta la puja si es exactamente precio actual + $10.

        El usuario sale del token, no de un parametro: asi nadie puede
        pujar a nombre de otro.
        """
        usuario = self._auth.usuario_de(token)
        if isinstance(monto, bool) or not isinstance(monto, (int, float)):
            raise ValueError("El monto debe ser un numero.")

        with self._lock:
            if self._estado == "esperando":
                raise RuntimeError("La subasta aun no inicia.")
            if self._estado == "finalizada":
                raise RuntimeError("La subasta ya termino.")
            esperado = self._precio_actual + INCREMENTO
            # round(..., 2) compara centavos y evita errores de float
            # como 0.1 + 0.2 = 0.30000000000000004.
            if round(monto, 2) != round(esperado, 2):
                raise ValueError(
                    f"Solo se acepta ${esperado:.2f} "
                    f"(precio actual ${self._precio_actual:.2f} + "
                    f"${INCREMENTO:.0f}).")
            if usuario == self._ganador:
                raise ValueError("Ya vas ganando; espera otra puja.")

            self._precio_actual = esperado
            self._ganador = usuario
            self._reiniciar_cronometro()
            print(f"[subasta] {usuario} puja ${esperado:.2f}")
            self._eventos.put(("nueva_puja",
                               (usuario, esperado, SEGUNDOS_CRONOMETRO)))
            return self._precio_actual

    @Pyro4.expose
    def obtener_estado(self):
        """Devuelve el estado de la subasta como diccionario."""
        with self._lock:
            return self._estado_actual()

    def _estado_actual(self):
        """Arma el diccionario de estado (se llama con el Lock tomado)."""
        restante = 0
        if self._estado == "abierta":
            restante = max(0, round(self._vence - time.time()))
        return {
            "producto": self._producto,
            "precio_base": self._precio_base,
            "precio_actual": self._precio_actual,
            "siguiente_puja": self._precio_actual + INCREMENTO,
            "ganador": self._ganador,
            "estado": self._estado,
            "segundos_restantes": restante,
            "conectados": len(self._clientes),
        }

    # ---------------------------------------------------------------
    # Cronometro (solo servidor)
    # ---------------------------------------------------------------
    def iniciar_subasta(self):
        """Abre la subasta y arranca el primer cronometro de 10 s."""
        with self._lock:
            if self._estado != "esperando":
                return
            self._estado = "abierta"
            self._reiniciar_cronometro()
            estado = self._estado_actual()
        print("[subasta] Subasta iniciada.")
        self._eventos.put(("subasta_iniciada", (estado,)))

    def _reiniciar_cronometro(self):
        """Cancela el cronometro anterior y crea uno nuevo de 10 s.

        Se llama con el Lock tomado. Cada cronometro lleva su numero de
        ronda; cuando vence, _adjudicar solo actua si su ronda sigue
        siendo la vigente. Eso cubre la carrera en la que el Timer ya
        empezo a ejecutarse justo cuando llego una puja: cancel() ya no
        lo puede detener, pero su ronda quedo vieja y se ignora.
        """
        if self._cronometro is not None:
            self._cronometro.cancel()
        self._ronda += 1
        self._vence = time.time() + SEGUNDOS_CRONOMETRO
        # args=(...) son los argumentos que el Timer pasara a _adjudicar.
        self._cronometro = threading.Timer(
            SEGUNDOS_CRONOMETRO, self._adjudicar, args=(self._ronda,))
        self._cronometro.daemon = True
        self._cronometro.start()

    def _adjudicar(self, ronda):
        """Vende el objeto al ultimo postor si nadie pujo en 10 s."""
        with self._lock:
            if ronda != self._ronda or self._estado != "abierta":
                return  # cronometro viejo: hubo una puja despues
            self._estado = "finalizada"
            ganador, precio = self._ganador, self._precio_actual
        if ganador:
            print(f"[subasta] VENDIDO a {ganador} por ${precio:.2f}")
        else:
            print("[subasta] Termino sin pujas (desierta).")
        self._eventos.put(("subasta_finalizada", (ganador, precio)))

    # ---------------------------------------------------------------
    # Avisos a los clientes (callbacks)
    # ---------------------------------------------------------------
    def _notificar_clientes(self):
        """Un solo hilo saca eventos de la cola y avisa a todos, en orden."""
        while True:
            metodo, argumentos = self._eventos.get()
            with self._lock:
                destinatarios = list(self._clientes.items())
            for usuario, receptor in destinatarios:
                try:
                    # getattr(obj, "nombre") obtiene el metodo por su
                    # nombre; *argumentos desempaqueta la tupla.
                    getattr(receptor, metodo)(*argumentos)
                except Pyro4.errors.CommunicationError:
                    print(f"[subasta] {usuario} no responde; se elimina.")
                    with self._lock:
                        self._clientes.pop(usuario, None)
            if metodo == "subasta_finalizada":
                self.finalizada.set()


def leer_argumentos():
    """Lee los parametros desde la linea de comandos."""
    parser = argparse.ArgumentParser(description="Servidor de subastas")
    parser.add_argument("--producto", default="Laptop Lenovo ThinkPad")
    parser.add_argument("--precio-base", type=float, default=100.0)
    parser.add_argument("--host", default="localhost",
                        help="IP del servidor en la red")
    parser.add_argument("--sin-telegram", action="store_true",
                        help="muestra los codigos en esta terminal")
    return parser.parse_args()


def crear_bot(sin_telegram):
    """Devuelve el bot real o el de consola segun los argumentos."""
    if sin_telegram:
        return BotConsola()
    # El token NUNCA va en el codigo: se lee de una variable de entorno.
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN. Usa: export "
                         "TELEGRAM_BOT_TOKEN=\"...\" o --sin-telegram")
    return BotTelegram(token)


def main():
    """Publica la subasta, espera Enter para iniciarla y atiende pujas."""
    args = leer_argumentos()

    bot = crear_bot(args.sin_telegram)
    bot.iniciar()
    subasta = ServicioSubasta(args.producto, args.precio_base,
                              AutenticadorOTP(bot))

    daemon = Pyro4.Daemon(host=args.host)
    uri = daemon.register(subasta)
    with Pyro4.locateNS() as ns:
        ns.register(NOMBRE_SERVICIO, uri)

    # requestLoop bloquea, asi que va en otro hilo; el hilo principal
    # queda libre para leer el Enter del administrador.
    threading.Thread(target=daemon.requestLoop, daemon=True).start()
    print(f"Servicio registrado como {NOMBRE_SERVICIO}")
    print(f"Producto: {args.producto} | Precio base: "
          f"${args.precio_base:.2f}")

    try:
        input("Cuando los usuarios esten conectados, presiona Enter "
              "para INICIAR la subasta...\n")
        subasta.iniciar_subasta()
        subasta.finalizada.wait()
        input("Subasta terminada. Presiona Enter para apagar el "
              "servidor.\n")
    except (KeyboardInterrupt, EOFError):
        print("\nServidor detenido.")
    finally:
        try:
            with Pyro4.locateNS() as ns:
                ns.remove(NOMBRE_SERVICIO)
        except Pyro4.errors.NamingError:
            pass
        daemon.shutdown()


if __name__ == "__main__":
    main()
