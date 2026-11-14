"""Cliente de la subasta: login con codigo de Telegram y pujas de +$10.

Pasos:
    1. Escribe tu usuario (el mismo que vinculaste con /start en el bot).
    2. Te llega un codigo de 6 digitos a Telegram; escribelo aqui.
    3. Ya dentro, el menu permite pujar y ver el estado. Los avisos
       (nueva puja, inicio y fin) llegan solos por callback.

Ejecucion (con el Name Server y el servidor ya iniciados):
    python3 subasta_client.py
    python3 subasta_client.py --host 192.168.1.20   # en red: IP de ESTA PC
"""

import argparse
import threading

import Pyro4
import Pyro4.errors


NOMBRE_SERVICIO = "PYRONAME:subastas.telegram"


class ReceptorSubasta(object):
    """Objeto del cliente que el SERVIDOR llama cuando hay novedades.

    Aqui los papeles se invierten: el cliente publica este objeto en su
    propio daemon y el servidor actua como cliente de el (callback).
    """

    def __init__(self, usuario):
        """Guarda el usuario y crea la bandera de fin de subasta."""
        self._usuario = usuario
        # Event es una bandera segura entre hilos: set() la activa e
        # is_set() pregunta si esta activa.
        self.finalizada = threading.Event()

    # @Pyro4.expose: el servidor puede llamar este metodo por la red.
    # @Pyro4.callback: si este metodo falla, el error se muestra aqui
    # en el cliente en lugar de perderse.
    @Pyro4.expose
    @Pyro4.callback
    def subasta_iniciada(self, estado):
        """Aviso de que la subasta empezo."""
        print(f"\n  [AVISO] La subasta EMPEZO. Precio base "
              f"${estado['precio_base']:.2f}. Primera puja: "
              f"${estado['siguiente_puja']:.2f}")

    @Pyro4.expose
    @Pyro4.callback
    def nueva_puja(self, usuario, monto, segundos):
        """Aviso de una puja aceptada; el cronometro volvio a 10 s."""
        # Expresion condicional: valor_si_true if condicion else valor_si_false
        quien = "Tu" if usuario == self._usuario else usuario
        print(f"\n  [AVISO] {quien} pujo ${monto:.2f}. "
              f"Quedan {segundos} s si nadie supera.")

    @Pyro4.expose
    @Pyro4.callback
    def subasta_finalizada(self, ganador, monto):
        """Aviso del resultado final."""
        if ganador is None:
            print("\n  [AVISO] La subasta termino sin pujas.")
        elif ganador == self._usuario:
            print(f"\n  [AVISO] GANASTE el objeto por ${monto:.2f}")
        else:
            print(f"\n  [AVISO] Vendido a {ganador} por ${monto:.2f}")
        print("  Presiona Enter para salir.")
        self.finalizada.set()


def iniciar_sesion(subasta):
    """Pide usuario y codigo hasta lograr entrar; devuelve (usuario, token)."""
    while True:
        usuario = input("Usuario: ").strip().lower()
        if not usuario:
            continue
        try:
            vigencia = subasta.solicitar_codigo(usuario)
        except PermissionError as error:
            print(f"  {error}")
            continue
        print(f"  Te enviamos un codigo por Telegram (vence en "
              f"{vigencia} s).")

        # Bucle interno: reintentar el codigo sin volver a pedir usuario.
        while True:
            codigo = input("Codigo (Enter vacio para pedir otro): ").strip()
            if not codigo:
                break  # vuelve al bucle externo y pide un codigo nuevo
            try:
                token = subasta.iniciar_sesion(usuario, codigo)
                print(f"  Bienvenido, {usuario}.")
                return usuario, token
            except PermissionError as error:
                print(f"  {error}")
                # Si el codigo ya no sirve, hay que pedir otro.
                if "nuevo" in str(error):
                    break


def mostrar_estado(estado):
    """Imprime el estado que devuelve el servidor."""
    lider = estado["ganador"] or "nadie"
    print(f"\nObjeto: {estado['producto']}")
    print(f"Precio base: ${estado['precio_base']:.2f} | Actual: "
          f"${estado['precio_actual']:.2f} | Lider: {lider}")
    print(f"Estado: {estado['estado']} | Tiempo: "
          f"{estado['segundos_restantes']} s | Conectados: "
          f"{estado['conectados']}")


def pujar(subasta, token):
    """Pide al servidor el precio vigente y puja exactamente +$10."""
    estado = subasta.obtener_estado()
    monto = estado["siguiente_puja"]
    try:
        precio = subasta.pujar(token, monto)
        print(f"Puja aceptada: ${precio:.2f}")
    except (ValueError, RuntimeError) as error:
        # Si otro pujo entre obtener_estado() y pujar(), el servidor
        # rechaza el monto viejo: es el comportamiento correcto.
        print(f"Puja rechazada: {error}")


def iniciar_receptor(usuario, host):
    """Publica el receptor en un daemon propio atendido en segundo plano."""
    receptor = ReceptorSubasta(usuario)
    daemon = Pyro4.Daemon(host=host)
    daemon.register(receptor)
    threading.Thread(target=daemon.requestLoop, daemon=True).start()
    return receptor, daemon


def ejecutar_menu(subasta, token, receptor):
    """Menu principal hasta que el usuario salga o termine la subasta."""
    while not receptor.finalizada.is_set():
        print("\n1. Pujar (+$10)   2. Ver estado   3. Salir")
        opcion = input("Opcion: ").strip()
        if receptor.finalizada.is_set():
            break
        if opcion == "1":
            pujar(subasta, token)
        elif opcion == "2":
            mostrar_estado(subasta.obtener_estado())
        elif opcion == "3":
            break
        else:
            print("Opcion no valida.")


def main():
    """Login, registro del callback y menu."""
    parser = argparse.ArgumentParser(description="Cliente de subastas")
    parser.add_argument("--host", default="localhost",
                        help="IP de este equipo para recibir avisos")
    args = parser.parse_args()

    daemon = None
    try:
        with Pyro4.Proxy(NOMBRE_SERVICIO) as subasta:
            usuario, token = iniciar_sesion(subasta)
            receptor, daemon = iniciar_receptor(usuario, args.host)
            mostrar_estado(subasta.registrar_receptor(token, receptor))
            try:
                ejecutar_menu(subasta, token, receptor)
            finally:
                subasta.cerrar_sesion(token)
        print("Desconectado.")
    except Pyro4.errors.NamingError:
        print("No se encontro el Name Server o el servicio.")
    except Pyro4.errors.PyroError as error:
        print(f"Error de comunicacion: {error}")
    except (KeyboardInterrupt, EOFError):
        print("\nSaliendo.")
    finally:
        if daemon is not None:
            daemon.shutdown()


if __name__ == "__main__":
    main()
