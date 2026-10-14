"""
Tests unitarios — relay_lan.py
Cancerbero: ningún cambio al relay sale sin pasar estos tests.

No tocan internet: levantan un origen falso que exige User-Agent de navegador,
igual que hace fuente-h, y comprueban que el relay lo satisface por su cuenta.

Ejecutar:
    cd /home/david/Portfolio/Mundia2026
    venv/bin/pytest tests/test_relay_lan.py -v
"""
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from relay_lan import (CACHE_MAX, COLCHON, VENTANA, NYALER, REFERER_NYALER, Prefetcher,
                       hacer_handler, ip_lan, resolver_origen)

PLAYLIST = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.5,\nindex_1.ts\n"
SEGMENTO = b"\x47" * 1880  # paquetes TS de mentira


class OrigenExigente(BaseHTTPRequestHandler):
    """Origen que devuelve 403 salvo con User-Agent de navegador — como fuente-h."""

    protocol_version = "HTTP/1.1"
    recibidas = []  # cabeceras que le han llegado, para inspeccionarlas desde el test

    def do_GET(self):
        OrigenExigente.recibidas.append(dict(self.headers))
        if "Mozilla" not in self.headers.get("User-Agent", ""):
            self.send_error(403, "Forbidden")
            return
        cuerpo = PLAYLIST if self.path.endswith(".m3u8") else SEGMENTO
        tipo = "application/vnd.apple.mpegurl" if self.path.endswith(".m3u8") else "video/mp2t"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, fmt, *args):
        pass


def _arrancar(handler):
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor, f"http://127.0.0.1:{servidor.server_port}/"


@pytest.fixture
def relay():
    """Origen exigente + relay delante. Devuelve la URL base del relay."""
    origen_srv, origen_url = _arrancar(OrigenExigente)
    relay_srv, relay_url = _arrancar(hacer_handler(origen_url, "https://ejemplo.test/"))
    yield relay_url
    relay_srv.shutdown()
    origen_srv.shutdown()


def test_origen_rechaza_a_vlc():
    """Sin el relay, un cliente tipo VLC se come el 403. Justifica que el relay exista."""
    _, origen_url = _arrancar(OrigenExigente)
    req = urllib.request.Request(origen_url + "index.m3u8",
                                 headers={"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403


def test_playlist_llega_a_un_cliente_sin_cabeceras(relay):
    req = urllib.request.Request(relay + "index.m3u8",
                                 headers={"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        assert r.read() == PLAYLIST


def test_segmento_ts_se_reenvia_entero(relay):
    with urllib.request.urlopen(relay + "index_1.ts", timeout=5) as r:
        cuerpo = r.read()
    assert cuerpo == SEGMENTO
    assert len(cuerpo) == int(r.headers["Content-Length"])


def test_cabeceras_que_recibe_el_origen(relay):
    """El relay pone el UA de navegador y el Referer; es lo único que le pide el origen."""
    OrigenExigente.recibidas.clear()
    urllib.request.urlopen(relay + "index.m3u8", timeout=5).read()
    assert len(OrigenExigente.recibidas) == 1
    cab = OrigenExigente.recibidas[0]
    assert "Mozilla" in cab["User-Agent"]
    assert cab["Referer"] == "https://ejemplo.test/"


def test_query_string_se_descarta(relay):
    """VLC añade parámetros propios; el origen no los espera."""
    with urllib.request.urlopen(relay + "index.m3u8?_=123", timeout=5) as r:
        assert r.read() == PLAYLIST


def test_origen_caido_da_502():
    """Si el origen no responde, el relay no revienta: devuelve 502."""
    _, relay_url = _arrancar(hacer_handler("http://127.0.0.1:1/", None))
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(relay_url + "index.m3u8", timeout=5)
    assert e.value.code == 502


class OrigenQueProhibeReferer(BaseHTTPRequestHandler):
    """Como quedó fuente-h el 29-ago: 403 SI llega Referer, 200 sin él."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.headers.get("Referer"):
            self.send_error(403, "Forbidden")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(PLAYLIST)))
        self.end_headers()
        self.wfile.write(PLAYLIST)

    def log_message(self, fmt, *args):
        pass


def test_slot_no_manda_referer():
    """fuente-h dejó de aceptarlo: --slot va sin Referer."""
    origen, referer = resolver_origen(slot=16)
    assert origen == NYALER.format(slot=16)
    assert referer is None
    assert REFERER_NYALER is None


def test_referer_explicito_se_respeta():
    """Si otra fuente lo vuelve a pedir, --referer lo pone."""
    _, referer = resolver_origen(slot=16, referer="https://otra.test/")
    assert referer == "https://otra.test/"
    _, referer = resolver_origen(origen="http://x/", referer="https://otra.test/")
    assert referer == "https://otra.test/"


def test_sin_slot_ni_origen_es_error():
    with pytest.raises(ValueError):
        resolver_origen()


def test_relay_sin_referer_pasa_el_origen_que_lo_prohibe():
    """Regresión del 29-ago: con Referer el origen daba 403 y el relay se caía."""
    origen_srv, origen_url = _arrancar(OrigenQueProhibeReferer)
    relay_srv, relay_url = _arrancar(hacer_handler(origen_url, None))
    try:
        with urllib.request.urlopen(relay_url + "index.m3u8", timeout=5) as r:
            assert r.status == 200
            assert r.read() == PLAYLIST
    finally:
        relay_srv.shutdown()
        origen_srv.shutdown()


def test_relay_con_referer_choca_con_ese_origen():
    """La otra mitad del hallazgo: mandarlo es exactamente lo que rompía."""
    origen_srv, origen_url = _arrancar(OrigenQueProhibeReferer)
    relay_srv, relay_url = _arrancar(hacer_handler(origen_url, "https://ejemplo.test/"))
    try:
        with urllib.request.urlopen(relay_url + "index.m3u8", timeout=5) as r:
            assert r.status == 502  # el relay traduce el 403 del origen, no revienta
    except urllib.error.HTTPError as e:
        assert e.code in (403, 502)
    finally:
        relay_srv.shutdown()
        origen_srv.shutdown()


def test_ip_lan_devuelve_una_ipv4():
    partes = ip_lan().split(".")
    assert len(partes) == 4 and all(p.isdigit() for p in partes)


# --- Prefetcher: el buffer que evita que la tablet se corte ---


class OrigenConPlaylist(OrigenExigente):
    """Origen con una playlist de varios segmentos, para ejercitar el prefetcher."""

    segmentos = [f"index_{n}.ts" for n in range(1, 21)]

    def do_GET(self):
        OrigenConPlaylist.recibidas.append(dict(self.headers))
        if "Mozilla" not in self.headers.get("User-Agent", ""):
            self.send_error(403, "Forbidden")
            return
        if self.path.endswith(".m3u8"):
            lineas = ["#EXTM3U", "#EXT-X-VERSION:3"]
            for nombre in self.segmentos:
                lineas += ["#EXTINF:2.4,", nombre]
            cuerpo, tipo = ("\n".join(lineas) + "\n").encode(), "application/vnd.apple.mpegurl"
        else:
            cuerpo, tipo = SEGMENTO, "video/mp2t"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


@pytest.fixture
def relay_con_buffer():
    """Origen con playlist + prefetcher + relay. Espera a que el buffer se llene."""
    origen_srv, origen_url = _arrancar(OrigenConPlaylist)
    pref = Prefetcher(origen_url, None)
    pref.start()
    limite = time.time() + 10
    while pref.playlist() is None and time.time() < limite:
        time.sleep(0.1)
    relay_srv, relay_url = _arrancar(hacer_handler(origen_url, None, pref))
    yield relay_url, pref
    pref.parar.set()
    relay_srv.shutdown()
    origen_srv.shutdown()


def test_el_prefetcher_llena_la_cache(relay_con_buffer):
    _, pref = relay_con_buffer
    assert len(pref.cache) >= COLCHON + 1


def test_la_playlist_solo_anuncia_lo_ya_descargado(relay_con_buffer):
    """Si anunciara segmentos sin bajar, VLC esperaría por ellos y se cortaría."""
    relay_url, pref = relay_con_buffer
    with urllib.request.urlopen(relay_url + "index.m3u8", timeout=5) as r:
        cuerpo = r.read().decode()
    anunciados = [l for l in cuerpo.splitlines() if l.endswith(".ts")]
    assert anunciados
    assert all(pref.segmento(n) is not None for n in anunciados)


def test_deja_colchon_por_delante(relay_con_buffer):
    """Los ultimos segmentos se guardan de reserva, no se anuncian."""
    relay_url, pref = relay_con_buffer
    with urllib.request.urlopen(relay_url + "index.m3u8", timeout=5) as r:
        anunciados = [l for l in r.read().decode().splitlines() if l.endswith(".ts")]
    assert pref.orden[-COLCHON:] and anunciados[-1] not in pref.orden[-COLCHON:]


def test_los_segmentos_salen_de_la_cache(relay_con_buffer):
    """Servidos de memoria: sin nuevas peticiones al origen y en tiempo despreciable."""
    relay_url, pref = relay_con_buffer
    with urllib.request.urlopen(relay_url + "index.m3u8", timeout=5) as r:
        nombre = [l for l in r.read().decode().splitlines() if l.endswith(".ts")][0]
    antes = len(OrigenConPlaylist.recibidas)
    t0 = time.time()
    with urllib.request.urlopen(relay_url + nombre, timeout=5) as r:
        assert r.read() == SEGMENTO
    assert time.time() - t0 < 0.5
    assert len(OrigenConPlaylist.recibidas) == antes  # no volvio a pedirlo


def test_segmento_fuera_de_cache_se_pide_al_origen(relay_con_buffer):
    """Cliente muy retrasado: no se le da un 404, se busca al vuelo."""
    relay_url, _ = relay_con_buffer
    with urllib.request.urlopen(relay_url + "index_99.ts", timeout=5) as r:
        assert r.read() == SEGMENTO


def test_la_secuencia_nunca_retrocede_al_podar_la_cache():
    """Regresión: si MEDIA-SEQUENCE baja al caducar segmentos viejos, mpv se atasca.

    Pasó en directo el 26-ago: la secuencia se calculaba con el tamaño de la caché,
    así que al empezar a podar retrocedía y el reproductor se quedaba en buffering.
    """
    pref = Prefetcher("http://origen.test/", None)
    secuencias = []
    for n in range(1, CACHE_MAX * 3):
        nombre = f"index_{n}.ts"
        with pref.lock:
            pref.duraciones[nombre] = "#EXTINF:2.4,"
            pref.cache[nombre] = SEGMENTO
            pref.orden.append(nombre)
            pref.indice[nombre] = pref.emitidos
            pref.emitidos += 1
            while len(pref.cache) > CACHE_MAX:
                viejo, _ = pref.cache.popitem(last=False)
                pref.duraciones.pop(viejo, None)
                pref.indice.pop(viejo, None)
                pref.orden.remove(viejo)
        cuerpo = pref.playlist()
        if cuerpo:
            linea = [l for l in cuerpo.decode().splitlines() if "MEDIA-SEQUENCE" in l][0]
            secuencias.append(int(linea.split(":")[1]))

    assert len(pref.cache) == CACHE_MAX          # se ha podado de verdad
    assert secuencias == sorted(secuencias)      # y aun así nunca retrocedió
    assert secuencias[-1] > secuencias[0]


def test_playlist_espera_a_que_se_llene_el_colchon():
    """Con colchón grande, mientras se llena no se sirve nada (ni un segmento suelto).

    Regresión del 29-ago: devolvía orden[:1] y la playlist se quedaba fija en
    MEDIA-SEQUENCE 0 — mpv en Buffering para siempre.
    """
    pf = Prefetcher("http://x/", None, colchon=10, ventana=1)
    for i in range(10):
        pf.orden.append(f"s{i}.ts")
        pf.indice[f"s{i}.ts"] = i
        pf.duraciones[f"s{i}.ts"] = "#EXTINF:2.9,"
    assert pf.playlist() is None
    pf.orden.append("s10.ts")
    pf.indice["s10.ts"] = 10
    pf.duraciones["s10.ts"] = "#EXTINF:2.9,"
    salida = pf.playlist()
    assert salida is not None and b"s0.ts" in salida


# ─── El retraso vive en la ventana anunciada (30-ago) ───────────────────────────
# Medido el 29-ago: con VENTANA=6 mpv se quedaba en 7-10 s de caché por mucho que se le
# pidiera --retraso 60, porque el retraso engordaba una reserva que no se anunciaba.

def _llenar(pf, n, extinf="#EXTINF:2.9,"):
    """Mete n segmentos ya 'bajados', como haría el prefetcher."""
    for i in range(n):
        nombre = f"s{i}.ts"
        with pf.lock:
            pf.cache[nombre] = SEGMENTO
            pf.duraciones[nombre] = extinf
            pf.orden.append(nombre)
            pf.indice[nombre] = pf.emitidos
            pf.emitidos += 1
            while len(pf.cache) > pf.cache_max:
                viejo, _ = pf.cache.popitem(last=False)
                pf.duraciones.pop(viejo, None)
                pf.indice.pop(viejo, None)
                pf.orden.remove(viejo)


def _llenar_desde(pf, desde, hasta, extinf="#EXTINF:2.9,"):
    for i in range(desde, hasta):
        nombre = f"s{i}.ts"
        with pf.lock:
            if pf._hueco:
                pf.tras_hueco.add(nombre)
                pf._hueco = False
            pf.cache[nombre] = SEGMENTO
            pf.duraciones[nombre] = extinf
            pf.orden.append(nombre)
            pf.indice[nombre] = pf.emitidos
            pf.emitidos += 1


def test_la_ventana_grande_se_anuncia_entera():
    """Con ventana=20 la playlist trae 20 segmentos, no los 6 de siempre."""
    pf = Prefetcher("http://x/", None, ventana=20)
    _llenar(pf, 40)
    anunciados = [l for l in pf.playlist().decode().splitlines() if l.endswith(".ts")]
    assert len(anunciados) == 20


def test_no_se_sirve_nada_hasta_que_la_ventana_esta_llena():
    """Si arrancara con media ventana, el retraso pedido no existiría."""
    pf = Prefetcher("http://x/", None, ventana=20)
    _llenar(pf, 15)
    assert pf.playlist() is None          # 15 < ventana(20) + colchon(3)
    _llenar(pf, 8)
    assert pf.playlist() is not None


def test_la_poda_nunca_se_come_lo_anunciado():
    """Regresión: un .ts anunciado pero ya desalojado cae al reenvío al vuelo, que
    contra ESPN (token caducado) es un 403 y un hueco en el vídeo."""
    pf = Prefetcher("http://x/", None, ventana=30)
    _llenar(pf, pf.cache_max * 3)         # se poda de sobra
    assert len(pf.cache) == pf.cache_max
    anunciados = [l for l in pf.playlist().decode().splitlines() if l.endswith(".ts")]
    assert len(anunciados) == 30
    assert all(pf.segmento(n) is not None for n in anunciados)


def test_la_cache_crece_con_la_ventana():
    """Con ventanas grandes, CACHE_MAX fijo dejaría de dar margen sobre lo anunciado."""
    assert Prefetcher("http://x/", None).cache_max == CACHE_MAX
    grande = Prefetcher("http://x/", None, ventana=100)
    assert grande.cache_max >= (100 + COLCHON) * 2


def test_la_duracion_se_mide_del_extinf_real():
    """fuente-h sirve 2,4 s y ESPN 2,9 s: dar 2,9 por hecho desvía el retraso un 20%."""
    pf = Prefetcher("http://x/", None)
    assert pf.duracion_media() == pytest.approx(2.9)   # sin datos, el supuesto
    _llenar(pf, 5, extinf="#EXTINF:2.4,")
    assert pf.duracion_media() == pytest.approx(2.4)
    # 90 s de retraso son 38 segmentos a 2,4 s, no los 31 que salen a 2,9.
    assert max(VENTANA, round(90 / pf.duracion_media())) == 38


# ─── El prefetcher no se puede morir, y no puede perder segmentos (30-ago) ──────

class OrigenTruncado(OrigenExigente):
    """Declara mas Content-Length del que envia: provoca http.client.IncompleteRead.

    Es la averia exacta del 30-ago. IncompleteRead hereda de HTTPException, NO de
    OSError, asi que se colaba por la lista cerrada de excepciones y mataba el hilo.
    """

    def do_GET(self):
        if self.path.endswith(".m3u8"):
            cuerpo = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.4,\nindex_1.ts\n"
            tipo = "application/vnd.apple.mpegurl"
        else:
            cuerpo, tipo = SEGMENTO, "video/mp2t"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo) + 5000))   # miente
        self.end_headers()
        self.wfile.write(cuerpo)


def test_incompleteread_no_mata_el_prefetcher():
    """Regresion del 30-ago: el hilo moria y el relay servia una playlist congelada."""
    srv, url = _arrancar(OrigenTruncado)
    pf = Prefetcher(url, None)
    pf.start()
    time.sleep(3)
    try:
        assert pf.is_alive(), "el prefetcher murio con un IncompleteRead"
        assert pf.fallos > 0, "deberia haber contabilizado el fallo"
    finally:
        pf.parar.set()
        srv.shutdown()


def test_una_excepcion_que_no_es_de_red_tampoco_lo_mata():
    """La captura tiene que ser amplia, no otra lista cerrada de tipos."""
    pf = Prefetcher("http://x/", None)
    pf._sondear = lambda: (_ for _ in ()).throw(ValueError("algo raro"))
    pf.start()
    time.sleep(1.5)
    try:
        assert pf.is_alive()
        assert pf.fallos > 0
    finally:
        pf.parar.set()


class OrigenLento(OrigenExigente):
    """Un segmento tarda mucho; los demas van normales."""

    sondeos = 0

    def do_GET(self):
        if self.path.endswith(".m3u8"):
            OrigenLento.sondeos += 1
            cuerpo = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.4,\nlento_1.ts\n"
            tipo = "application/vnd.apple.mpegurl"
        else:
            time.sleep(4)                     # la descarga que bloqueaba todo
            cuerpo, tipo = SEGMENTO, "video/mp2t"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


def test_el_sondeo_no_se_bloquea_con_las_descargas():
    """El bug que picaba el audio: la ventana del origen es de ~11,6 s, y esperar a
    bajar la tanda entera antes de re-sondear la dejaba rotar y perdia segmentos."""
    OrigenLento.sondeos = 0
    srv, url = _arrancar(OrigenLento)
    pf = Prefetcher(url, None)
    pf.start()
    time.sleep(3)                             # menos que los 4 s de la descarga lenta
    try:
        assert OrigenLento.sondeos >= 2, (
            f"solo {OrigenLento.sondeos} sondeos: el sondeo sigue atado a la descarga")
    finally:
        pf.parar.set()
        srv.shutdown()


def test_un_hueco_emite_discontinuity():
    """Sin la etiqueta, el reproductor se come el salto de PTS: chasquidos y desfase."""
    pf = Prefetcher("http://x/", None, ventana=4)
    _llenar(pf, 3)
    pf._hueco = True                          # se perdio el tramo siguiente
    _llenar_desde(pf, 3, 10)                  # ventana(4) + colchon(3) piden 7
    salida = pf.playlist().decode()
    assert "#EXT-X-DISCONTINUITY" in salida
    lineas = salida.splitlines()
    i = lineas.index("#EXT-X-DISCONTINUITY")
    assert lineas[i + 2] == "s3.ts"           # va justo delante del segmento tras el hueco


def test_sin_huecos_no_hay_discontinuity():
    pf = Prefetcher("http://x/", None, ventana=4)
    _llenar(pf, 10)
    assert "#EXT-X-DISCONTINUITY" not in pf.playlist().decode()


def test_estado_delata_un_relay_congelado():
    """El 30-ago el relay congelado se veia igual que uno sano desde fuera."""
    pf = Prefetcher("http://x/", None)
    pf.ultimo_ingreso = time.time() - 120
    e = pf.estado()
    assert e["segundos_sin_ingerir"] >= 119
    assert e["listo"] is False


def test_la_pagina_de_la_tablet_y_hls_se_sirven(relay_con_buffer):
    """Sin instalar nada: la tablet abre / y el navegador reproduce con hls.js."""
    relay_url, _ = relay_con_buffer
    with urllib.request.urlopen(relay_url, timeout=5) as r:
        pagina = r.read().decode()
    assert "<video" in pagina and "/hls.js" in pagina
    with urllib.request.urlopen(relay_url + "hls.js", timeout=10) as r:
        assert len(r.read()) > 100_000
    with urllib.request.urlopen(relay_url + "estado", timeout=5) as r:
        import json as _json
        e = _json.loads(r.read())
    assert "segundos_sin_ingerir" in e and "listo" in e
