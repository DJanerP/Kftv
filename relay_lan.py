#!/usr/bin/env python3
"""Relay HLS para la LAN — reenvía un stream que exige User-Agent de navegador.

fuente-h (y otras fuentes de sources.yaml) devuelven 403 sin UA de navegador.
VLC de Android manda el suyo y no deja cambiarlo, así que no puede consumirlas
directamente. Este relay se pone en medio: sirve la playlist y los segmentos en
la LAN sin cabeceras especiales, y hacia el origen añade el UA (y el Referer
sólo si se pide con --referer). No transcodifica — sólo reenvía bytes.

Lleva un prefetcher: un hilo va por delante descargando los segmentos a memoria,
así el cliente los recibe al instante en vez de esperar a que el relay se los
baje. Sin eso, la tablet iba siempre al límite y se cortaba. La playlist que se
sirve sólo anuncia lo que ya está en caché, para que VLC nunca pida un segmento
que aún no ha llegado.

    python3 relay_lan.py --slot 3
    python3 relay_lan.py --origen http://servidor-d.ejemplo:8888/GOLTV/

Luego, en el VLC de la tablet: http://<IP_LAN>:8099/index.m3u8
(subir la caché de red de VLC a ~5000 ms ayuda con fuentes justas de ancho de banda)
"""
import argparse
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"
NYALER = "https://servidor-h.ejemplo/stream/{slot}/"
# 29-ago: fuente-h invirtio la regla. Antes exigia Referer; ahora el nginx da 403
# CON Referer y 200 SIN el. Por eso --slot ya no manda ninguno. Si otra fuente
# vuelve a pedirlo, se pasa a mano con --referer.
REFERER_NYALER = None

CACHE_MAX = 120       # suelo de segmentos en memoria (~6 min a 2,9 s por segmento)
VENTANA = 6           # segmentos anunciados por defecto (~17 s); --retraso lo sube
COLCHON = 3           # reserva por delante: nunca se anuncia el borde del directo
HILOS = 4             # descargas simultáneas hacia el origen
DUR_SUPUESTA = 2.9    # segundos por segmento mientras no se haya visto ningún EXTINF
MAX_PENDIENTES = 32   # descargas en vuelo: tope para que un origen atascado no crezca sin fin
SIN_INGERIR = 30      # segundos sin un segmento nuevo antes de avisar en el log
ESTATICOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# El retraso vive en la VENTANA, no en el colchón. Un reproductor no puede cachear más
# vídeo del que la playlist le anuncia: con VENTANA=6 el techo eran ~17 s por mucho que
# el relay se guardara 60 s de reserva por delante (medido el 29-ago: mpv se quedaba en
# 7-10 s). Ahora --retraso N agranda lo que se sirve, mpv entra por el principio de esa
# ventana y va N segundos por detrás del directo con N segundos en la mano.

# Por qué en paralelo: ESPN Deportes (y compañía) limitan POR CONEXIÓN, no por ancho de
# banda. Medido el 29-ago: en secuencial, 3,2 s para bajar 2,9 s de vídeo — por debajo de
# tiempo real, el reproductor se queda seco. Los mismos 4 segmentos en paralelo: 3,9 s para
# 11,5 s de vídeo, casi 3x tiempo real. Con ese margen el colchón crece y se puede ver con
# retraso sin un solo corte.


def ip_lan():
    """IP con la que se sale a la red local (sin enviar nada)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def pedir(url, referer, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "application/octet-stream")


def resolver_playlist(url_index, referer, timeout=10):
    """Devuelve la URL de la playlist con segmentos de verdad.

    Algunos orígenes (ESPN Deportes) sirven un master que sólo apunta a otra playlist,
    y encima le cambian el nombre cada pocos minutos. Se sigue un salto.
    """
    cuerpo, _ = pedir(url_index, referer, timeout=timeout)
    texto = cuerpo.decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" not in texto:
        return url_index, texto
    for linea in texto.splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#"):
            destino = urljoin(url_index, linea)
            cuerpo, _ = pedir(destino, referer, timeout=timeout)
            return destino, cuerpo.decode("utf-8", "replace")
    return url_index, texto


class Prefetcher(threading.Thread):
    """Sigue la playlist del origen y va bajando segmentos a memoria por delante.

    El sondeo de la playlist y la descarga de segmentos van POR SEPARADO. Antes se
    pedia la playlist y se esperaba a bajar la tanda entera antes de volver a
    preguntar: como la ventana del origen es de solo 4 segmentos (~11,6 s), una tanda
    lenta —un timeout de 25 s basta— la dejaba rotar y esos segmentos se perdian para
    siempre. Eso es lo que picaba el audio el 30-ago (mpv: «Invalid audio PTS
    1078.22 -> 1101.39», 23 s de salto). Ahora se sondea a ritmo fijo y las descargas
    van por su cuenta en un pool persistente.
    """

    daemon = True

    def __init__(self, origen, referer, colchon=COLCHON, ventana=VENTANA, cache_max=None):
        super().__init__()
        self.colchon = colchon
        self.ventana = ventana
        # Si la poda desaloja un segmento recién anunciado, el reproductor lo pide, no está
        # en caché y cae al reenvío al vuelo — que contra ESPN, con el token caducado, es un
        # 403 y un hueco en el vídeo. La caché guarda siempre el doble de lo que se anuncia.
        self.cache_max = cache_max or max(CACHE_MAX, (ventana + colchon) * 2)
        self.origen = origen
        self.referer = referer
        self.cache = OrderedDict()      # nombre .ts -> bytes
        self.orden = []                 # nombres en caché, en orden de emisión
        self.indice = {}                # nombre -> nº de secuencia global (sólo crece)
        self.emitidos = 0               # cuántos segmentos han pasado por aquí en total
        self.cabecera = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
        self.duraciones = {}            # nombre -> EXTINF
        self.lock = threading.Lock()
        self.parar = threading.Event()
        self.fallos = 0
        self.url_playlist = None        # se resuelve sola (puede ser un master)
        self.urls = {}                  # nombre .ts -> URL absoluta
        # Descargas en vuelo, en el orden de la playlist: [nombre, extinf, future].
        self.pendientes = []
        self.pool = ThreadPoolExecutor(max_workers=HILOS)
        self.tras_hueco = set()         # segmentos precedidos de un hueco real
        self._hueco = False             # se perdio algo y aun no se ha marcado
        self.ultimo_ingreso = time.time()
        self.avisado = False

    def run(self):
        while not self.parar.is_set():
            try:
                self._ciclo()
            except Exception as e:
                # Captura amplia a proposito: el 30-ago un IncompleteRead (hereda de
                # HTTPException, NO de OSError) se colo por la lista cerrada de tipos y
                # mato este hilo. El relay siguio devolviendo 200 con la playlist
                # congelada y el reproductor drenó su colchon hasta secarse, sin un solo
                # aviso. Un prefetcher no se puede morir: si algo falla, se reintenta.
                self.fallos += 1
                print(f"prefetcher: {type(e).__name__}: {e}", flush=True)
                time.sleep(1)

    def _bajar(self, nombre):
        """Baja un segmento. Un reintento: los cortes sueltos son la norma aqui."""
        for intento in (1, 2):
            try:
                datos, _ = pedir(self.urls[nombre], self.referer, timeout=25)
                return datos
            except Exception:
                self.fallos += 1
                if intento == 2:
                    return None
                time.sleep(0.5)

    def _sondear(self):
        """Pide la playlist y manda a descargar lo nuevo. NO espera a que llegue."""
        # El master puede cambiar de nombre en caliente: se re-resuelve cada vuelta.
        self.url_playlist, texto = resolver_playlist(self.origen + "index.m3u8",
                                                     self.referer, timeout=10)
        if len(self.pendientes) >= MAX_PENDIENTES:
            return                      # origen atascado: no acumular mas trabajo
        dur = None
        for linea in texto.splitlines():
            linea = linea.strip()
            if linea.startswith("#EXTINF:"):
                dur = linea
            elif linea and not linea.startswith("#"):
                nombre = linea.split("?")[0].rsplit("/", 1)[-1]
                if nombre in self.duraciones or nombre in self.urls:
                    continue
                self.urls[nombre] = urljoin(self.url_playlist, linea)
                fut = self.pool.submit(self._bajar, nombre)
                self.pendientes.append([nombre, dur or "#EXTINF:2.5,", fut])

    def _drenar(self):
        """Pasa a la cache lo ya bajado, SIEMPRE desde la cabeza: el orden es el video."""
        while self.pendientes and self.pendientes[0][2].done():
            nombre, extinf, fut = self.pendientes.pop(0)
            datos = fut.result()
            if datos is None:
                # Se perdio de verdad. Se marca para avisar al reproductor con un
                # EXT-X-DISCONTINUITY en vez de encadenar el salto de PTS a ciegas.
                self.urls.pop(nombre, None)
                self._hueco = True
                continue
            with self.lock:
                if self._hueco:
                    self.tras_hueco.add(nombre)
                    self._hueco = False
                self.duraciones[nombre] = extinf
                self.cache[nombre] = datos
                self.orden.append(nombre)
                self.indice[nombre] = self.emitidos
                self.emitidos += 1
                self.ultimo_ingreso = time.time()
                self.avisado = False
                while len(self.cache) > self.cache_max:
                    viejo, _ = self.cache.popitem(last=False)
                    self.duraciones.pop(viejo, None)
                    self.indice.pop(viejo, None)
                    self.urls.pop(viejo, None)
                    self.tras_hueco.discard(viejo)
                    if viejo in self.orden:
                        self.orden.remove(viejo)

    def _vigilar(self):
        """Un relay que sirve una playlist congelada tiene que decirlo."""
        parado = time.time() - self.ultimo_ingreso
        if parado > SIN_INGERIR and not self.avisado:
            print(f"prefetcher: {parado:.0f} s sin ingerir un segmento nuevo", flush=True)
            self.avisado = True

    def _ciclo(self):
        self._sondear()
        self._drenar()
        self._vigilar()
        time.sleep(1)

    def duracion_media(self):
        """Segundos por segmento segun los EXTINF vistos. fuente-h sirve 2,4-2,5 s y ESPN
        2,9 s: dar uno por hecho desviaba el retraso pedido casi un 20% segun la fuente."""
        with self.lock:
            valores = []
            for extinf in self.duraciones.values():
                try:
                    valores.append(float(extinf.split(":", 1)[1].rstrip(",").strip()))
                except (IndexError, ValueError):
                    continue
        return sum(valores) / len(valores) if valores else DUR_SUPUESTA

    def estado(self):
        """Foto de lo que pasa por dentro, para /estado y para diagnosticar sin logs."""
        with self.lock:
            return {
                "emitidos": self.emitidos,
                "fallos": self.fallos,
                "en_cache": len(self.cache),
                "ventana": self.ventana,
                "colchon": self.colchon,
                "en_vuelo": len(self.pendientes),
                "huecos": len(self.tras_hueco),
                "segundos_sin_ingerir": round(time.time() - self.ultimo_ingreso, 1),
                "listo": len(self.orden) >= self.ventana + self.colchon,
            }

    def playlist(self):
        """Playlist con lo ya cacheado, dejando self.colchon segmentos de reserva por delante.

        No se sirve nada hasta que la ventana entera está bajada: si se empieza con media
        ventana, el retraso pedido no existe y el reproductor arranca pegado al directo.
        """
        with self.lock:
            c = self.colchon
            if len(self.orden) < self.ventana + c:
                # Ventana sin llenar. Devolver un solo segmento aquí dejaba la playlist
                # clavada en MEDIA-SEQUENCE 0 y el reproductor en Buffering eterno:
                # mejor decir "todavía no" y que el arranque espere.
                return None
            disponibles = self.orden[:-c]
            ventana = disponibles[-self.ventana:]
            if not ventana:
                return None
            # nº de secuencia global del primer segmento anunciado: nunca retrocede,
            # aunque se poden segmentos viejos de la caché (si retrocede, mpv se atasca).
            secuencia = self.indice[ventana[0]]
            lineas = [self.cabecera.rstrip("\n"), f"#EXT-X-MEDIA-SEQUENCE:{secuencia}"]
            for nombre in ventana:
                if nombre in self.tras_hueco:
                    # Se perdio el tramo anterior. Sin esta etiqueta el reproductor se
                    # come el salto de timestamps a ciegas: chasquidos y desincronia.
                    lineas.append("#EXT-X-DISCONTINUITY")
                lineas.append(self.duraciones.get(nombre, "#EXTINF:2.5,"))
                lineas.append(nombre)
        return ("\n".join(lineas) + "\n").encode()

    def segmento(self, nombre):
        with self.lock:
            return self.cache.get(nombre)


def hacer_handler(origen, referer, prefetcher=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _responder(self, cuerpo, tipo):
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(cuerpo)

        def _estatico(self, nombre, tipo):
            try:
                with open(os.path.join(ESTATICOS, nombre), "rb") as f:
                    self._responder(f.read(), tipo)
            except OSError:
                self.send_error(404, "no encontrado")

        def do_GET(self):
            ruta = self.path.lstrip("/").split("?")[0]

            # La tablet: pagina + reproductor, sin instalar nada. Chrome de Android no
            # reproduce HLS en un <video> a pelo, de ahi hls.js servido en local.
            if ruta in ("", "index.html"):
                self._estatico("tablet.html", "text/html; charset=utf-8")
                return
            if ruta == "hls.js":
                self._estatico("hls.js", "application/javascript")
                return
            if ruta == "estado":
                # El 30-ago el relay se congelo y por fuera se veia igual que sano.
                # Esto es el instrumento que faltaba: se mira desde la tablet o con curl.
                datos = prefetcher.estado() if prefetcher else {"listo": True}
                self._responder(json.dumps(datos).encode(), "application/json")
                return

            if prefetcher is not None:
                if ruta.endswith(".m3u8"):
                    cuerpo = prefetcher.playlist()
                    if cuerpo is None:
                        self.send_error(503, "Aun llenando el buffer")
                        return
                    self._responder(cuerpo, "application/vnd.apple.mpegurl")
                    return
                datos = prefetcher.segmento(ruta)
                if datos is not None:
                    self._responder(datos, "video/mp2t")
                    return
                # fuera de caché (cliente muy retrasado): se pide al vuelo

            try:
                cuerpo, tipo = pedir(origen + ruta, referer)
            except urllib.error.HTTPError as e:
                self.send_error(e.code, str(e.reason))
                return
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                self.send_error(502, str(e))
                return
            self._responder(cuerpo, tipo)

        def log_message(self, fmt, *args):  # una línea por petición, sin ruido
            print(f"{self.address_string()} {fmt % args}", flush=True)

    return Handler


def resolver_origen(slot=None, origen=None, referer=None):
    """Decide (origen, referer) segun lo pedido. --referer manda siempre."""
    if origen:
        return origen, referer
    if slot:
        return NYALER.format(slot=slot), referer if referer else REFERER_NYALER
    raise ValueError("hace falta slot u origen")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slot", type=int, help="slot de fuente-h (1..60)")
    p.add_argument("--origen", help="URL base del origen (acaba en /), alternativa a --slot")
    p.add_argument("--puerto", type=int, default=8099)
    p.add_argument("--referer", help="Referer hacia el origen (por defecto ninguno)")
    p.add_argument("--retraso", type=int, default=0, metavar="SEG",
                   help="ver con N segundos de retraso: el relay anuncia esa ventana de "
                        "vídeo ya bajado, así un origen con bajones no corta la reproducción")
    p.add_argument("--sin-prefetch", action="store_true", help="reenvío directo, sin buffer")
    args = p.parse_args()

    try:
        origen, referer = resolver_origen(args.slot, args.origen, args.referer)
    except ValueError as e:
        p.error(str(e))

    prefetcher = None
    if not args.sin_prefetch:
        prefetcher = Prefetcher(origen, referer)
        prefetcher.start()
    # El puerto se abre YA, sin esperar a llenar. Antes se abria al final y durante ~90 s
    # la tablet recibia "conexion rechazada", indistinguible de "esto no funciona"; ahora
    # la pagina carga desde el primer segundo y va contando el llenado por /estado.
    servidor = ThreadingHTTPServer(("0.0.0.0", args.puerto),
                                   hacer_handler(origen, referer, prefetcher))
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    print(f"origen : {origen}")
    print(f"tablet : http://{ip_lan()}:{args.puerto}/", flush=True)

    if prefetcher is not None:
        if args.retraso:
            # La duracion real se sabe tras la primera vuelta; hasta entonces, DUR_SUPUESTA.
            espera = time.time() + 15
            while not prefetcher.duraciones and time.time() < espera:
                time.sleep(0.5)
            dur = prefetcher.duracion_media()
            ventana = max(VENTANA, round(args.retraso / dur))
            with prefetcher.lock:
                prefetcher.ventana = ventana
                prefetcher.cache_max = max(CACHE_MAX, (ventana + prefetcher.colchon) * 2)
            print(f"llenando buffer… {ventana} segmentos de {dur:.1f} s "
                  f"(~{ventana * dur:.0f} s de retraso)", flush=True)


    if prefetcher is not None:
        # Hasta que la ventana este entera, la playlist devuelve 503: ese es el retraso.
        # mpv hay que lanzarlo DESPUES de la linea "listo" — un 503 al arrancar lo mata.
        limite = time.time() + 60 + args.retraso * 2
        while prefetcher.playlist() is None and time.time() < limite:
            time.sleep(0.5)
        if prefetcher.playlist() is None:
            # Origen lento: mejor menos retraso que dejar al reproductor comiendo 503.
            with prefetcher.lock:
                prefetcher.ventana = max(1, len(prefetcher.orden) - prefetcher.colchon)
            print(f"aviso: el buffer no se lleno a tiempo; se sirve con "
                  f"{prefetcher.ventana} segmentos", flush=True)
    print("listo  : sirviendo la ventana completa", flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        servidor.shutdown()
        if prefetcher:
            prefetcher.parar.set()


if __name__ == "__main__":
    main()
