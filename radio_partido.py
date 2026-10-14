#!/usr/bin/env python3
"""
radio_partido.py — relato de radio sudamericana sincronizado con el vídeo.

El vídeo siempre llega con retraso (relay + caché de mpv); la radio va casi al segundo.
Así que la radio se RETRASA hasta cuadrar: se reproduce en un mpv aparte con caché
grande y se hace seek hacia atrás dentro de esa caché.

Subcomandos:
    escuchar  --equipos Sevilla Barcelona [--segundos 30]
        Graba N segundos de cada radio de `audio.sudamerica` (sources.yaml) en paralelo,
        transcribe con whisper y puntúa cada transcripción por vocabulario de partido.
        Imprime la tabla ordenada: las que puntúan son las que están relatando.
    lanzar    URL [--socket /tmp/mpv_radio] [--grabar]
        Abre la radio en mpv con socket IPC. Sin --grabar, el margen de retraso es la caché
        que deje el servidor (medido 20-sep: streamtheworld 29-44 s, zeno/uy 8-10 s).
        Con --grabar, ffmpeg graba a logs/radio_<hora>.mp3 y mpv reproduce ese fichero
        mientras crece: se puede retrasar tanto como lleve grabado. Es la opción buena.
    retrasar  SEGUNDOS [--socket /tmp/mpv_radio]
        Retrasa la radio N segundos (seek relativo hacia atrás). Negativo = adelanta.
        Ajuste fino con las teclas de mpv en la ventana de la radio: ←/→ 5 s, ↓/↑ 60 s.

El mpv del vídeo se lanza con --mute=yes para oír solo la radio.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import yaml

AQUI = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(AQUI, "sources.yaml")
SOCKET_DEFECTO = "/tmp/mpv_radio"

# Vocabulario que solo aparece en un relato de fútbol en marcha. Sin tildes, en minúsculas.
VOCABULARIO = (
    "gol", "golazo", "corner", "tiro de esquina", "penal", "penalti", "arbitro", "arquero",
    "portero", "minuto", "primer tiempo", "segundo tiempo", "falta", "fuera de juego",
    "offside", "tarjeta", "amarilla", "roja", "saque", "lateral", "remate", "cabezazo",
    "travesano", "palo", "area", "delantero", "defensa", "mediocampo", "contragolpe",
    "pelota", "balon", "empate", "ganando", "perdiendo", "marcador", "estadio", "hinchada",
)
PESO_EQUIPO = 3  # cada mención de un equipo pesa como tres palabras del vocabulario


def normalizar(texto):
    """Minúsculas y sin tildes, para comparar sin que 'árbitro' y 'arbitro' sean distintos."""
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    # «Goooool» → «gol»: una letra repetida 3+ veces se reduce a una. Las dobles (Sevilla) se respetan.
    sin_alargar = re.sub(r"(.)\1{2,}", r"\1", sin_tildes.lower())
    return re.sub(r"\s+", " ", sin_alargar).strip()


def puntuar(transcripcion, equipos):
    """Puntuación de "esto es un relato del partido": menciones de equipos × 3 + vocabulario.

    Devuelve (puntos, detalle) — detalle es la lista de coincidencias para que David vea
    por qué puntúa. 0 puntos = esa radio no está en el partido.
    """
    texto = normalizar(transcripcion)
    if not texto:
        return 0, []
    detalle = []
    puntos = 0
    for equipo in equipos:
        n = len(re.findall(r"\b" + re.escape(normalizar(equipo)) + r"\b", texto))
        if n:
            puntos += n * PESO_EQUIPO
            detalle.append(f"{equipo}×{n}")
    for palabra in VOCABULARIO:
        n = len(re.findall(r"\b" + re.escape(palabra) + r"\b", texto))
        if n:
            puntos += n
            detalle.append(f"{palabra}×{n}")
    return puntos, detalle


def cargar_radios(ruta=SOURCES):
    """Lee `audio.sudamerica` de sources.yaml. Cada entrada: nombre, pais, votos, url."""
    with open(ruta, encoding="utf-8") as f:
        datos = yaml.safe_load(f)
    radios = (datos.get("audio") or {}).get("sudamerica") or []
    validas = []
    for r in radios:
        if not r.get("url") or not r.get("nombre"):
            continue
        validas.append({
            "nombre": r["nombre"],
            "pais": r.get("pais", "?"),
            "votos": int(r.get("votos", 0)),
            "url": r["url"],
        })
    return sorted(validas, key=lambda r: -r["votos"])


def grabar(url, segundos, destino):
    """Graba `segundos` de audio de la radio a un wav de 16 kHz mono (lo que whisper quiere)."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-user_agent", "VLC/3.0.20",
           "-i", url, "-t", str(segundos), "-ac", "1", "-ar", "16000", destino]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=segundos + 30)
    return r.returncode == 0 and os.path.exists(destino) and os.path.getsize(destino) > 1000


def transcribir(wav, modelo="base"):
    """Transcribe con el whisper de línea de comandos. Devuelve el texto (vacío si falla)."""
    carpeta = os.path.dirname(wav)
    cmd = ["whisper", wav, "--model", modelo, "--language", "es", "--output_format", "txt",
           "--output_dir", carpeta, "--fp16", "False", "--verbose", "False"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    txt = os.path.splitext(wav)[0] + ".txt"
    if r.returncode != 0 or not os.path.exists(txt):
        return ""
    with open(txt, encoding="utf-8") as f:
        return f.read()


def escuchar(equipos, segundos=30, radios=None, grabar_fn=grabar, transcribir_fn=transcribir):
    """Graba todas las radios en paralelo, transcribe, puntúa. Devuelve la lista ordenada."""
    radios = radios if radios is not None else cargar_radios()
    carpeta = tempfile.mkdtemp(prefix="radio_partido_")
    resultados = []

    def una(r):
        wav = os.path.join(carpeta, re.sub(r"[^a-z0-9]+", "_", normalizar(r["nombre"])) + ".wav")
        if not grabar_fn(r["url"], segundos, wav):
            return {**r, "puntos": -1, "detalle": ["sin audio"], "texto": ""}
        texto = transcribir_fn(wav)
        puntos, detalle = puntuar(texto, equipos)
        return {**r, "puntos": puntos, "detalle": detalle, "texto": texto}

    with ThreadPoolExecutor(max_workers=len(radios) or 1) as pool:
        resultados = list(pool.map(una, radios))
    # Primero las que relatan (más puntos), a igualdad la más votada.
    return sorted(resultados, key=lambda r: (-r["puntos"], -r["votos"]))


def imprimir_tabla(resultados):
    print(f"{'pts':>4}  {'votos':>6}  {'emisora':<22} {'pais':<10} detalle")
    for r in resultados:
        pts = "—" if r["puntos"] < 0 else str(r["puntos"])
        print(f"{pts:>4}  {r['votos']:>6}  {r['nombre'][:22]:<22} {r['pais'][:10]:<10} "
              f"{', '.join(r['detalle'][:6])}")
    relatan = [r for r in resultados if r["puntos"] > 0]
    if relatan:
        print(f"\nRelatan el partido: {', '.join(r['nombre'] for r in relatan)}")
        print(f"Para lanzar la primera:\n  venv/bin/python radio_partido.py lanzar '{relatan[0]['url']}'")
    else:
        print("\nNinguna radio está relatando este partido ahora mismo.")


def comando_mpv_radio(url, socket_path=SOCKET_DEFECTO):
    """El mpv de la radio: sin vídeo, caché de 10 min para poder retrasarla, IPC para el seek."""
    return ["mpv", "--no-video", "--force-window=yes", "--title=RADIO", "--cache=yes",
            "--cache-secs=600", "--demuxer-readahead-secs=600", "--demuxer-max-bytes=200MiB",
            f"--input-ipc-server={socket_path}", url]


def comando_grabar_radio(url, archivo):
    """ffmpeg que graba la radio a un mp3 que crece. -flush_packets para que mpv lo vea casi al segundo."""
    return ["ffmpeg", "-y", "-loglevel", "error", "-i", url, "-c:a", "libmp3lame", "-b:a", "128k",
            "-flush_packets", "1", archivo]


def comando_mpv_archivo(archivo, socket_path=SOCKET_DEFECTO):
    """mpv sobre el mp3 que crece: seek hacia atrás ilimitado. keep-open por si alcanza el final."""
    return ["mpv", "--no-video", "--force-window=yes", "--title=RADIO", "--keep-open=yes",
            f"--input-ipc-server={socket_path}", archivo]


def esperar_archivo(archivo, minimo=65536, timeout=20, dormir=time.sleep, tamano=None):
    """Bloquea hasta que el mp3 tenga `minimo` bytes (ffmpeg ya escribe). False si no llega."""
    tamano = tamano or (lambda a: os.path.getsize(a) if os.path.exists(a) else 0)
    for _ in range(int(timeout * 2)):
        if tamano(archivo) >= minimo:
            return True
        dormir(0.5)
    return False


def lanzar(url, socket_path=SOCKET_DEFECTO, grabar=False, popen=subprocess.Popen, carpeta=None,
           esperar=esperar_archivo):
    """Sin --grabar: mpv directo contra el stream (la caché que deje el servidor, 8-44 s medidos).
    Con --grabar: ffmpeg graba a logs/radio_<hora>.mp3 y mpv reproduce el fichero: retraso sin límite."""
    carpeta = carpeta or os.path.join(AQUI, "logs")
    os.makedirs(carpeta, exist_ok=True)
    log = os.path.join(carpeta, "mpv_radio.log")
    comun = dict(stdin=subprocess.DEVNULL, start_new_session=True)
    if grabar:
        archivo = os.path.join(carpeta, time.strftime("radio_%Y-%m-%d_%H%M%S.mp3"))
        with open(os.path.join(carpeta, "ffmpeg_radio.log"), "ab") as f:
            popen(comando_grabar_radio(url, archivo), stdout=f, stderr=subprocess.STDOUT, **comun)
        if not esperar(archivo):
            print(f"ffmpeg no escribe en {archivo}: mira {carpeta}/ffmpeg_radio.log")
            return None
        cmd = comando_mpv_archivo(archivo, socket_path)
    else:
        archivo = None
        cmd = comando_mpv_radio(url, socket_path)
    with open(log, "ab") as f:
        popen(cmd, stdout=f, stderr=subprocess.STDOUT, **comun)
    print(f"Radio lanzada{' (grabando a ' + archivo + ')' if archivo else ''}. Socket: {socket_path}. Log: {log}")
    print("Retrasar N s:  venv/bin/python radio_partido.py retrasar N")
    print("Vídeo con --mute=yes para oír solo la radio.")
    return archivo


def orden_retraso(segundos):
    """JSON que mpv entiende por el socket: seek relativo hacia atrás."""
    return json.dumps({"command": ["seek", -float(segundos), "relative"]}) + "\n"


def retrasar(segundos, socket_path=SOCKET_DEFECTO):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(socket_path)
        s.sendall(orden_retraso(segundos).encode())
        respuesta = s.recv(4096).decode(errors="replace").strip()
    finally:
        s.close()
    print(f"seek {-float(segundos):+.1f} s → {respuesta or 'sin respuesta'}")
    return respuesta


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("escuchar")
    e.add_argument("--equipos", nargs="+", required=True)
    e.add_argument("--segundos", type=int, default=30)
    l = sub.add_parser("lanzar")
    l.add_argument("url")
    l.add_argument("--socket", default=SOCKET_DEFECTO)
    l.add_argument("--grabar", action="store_true",
                   help="grabar a mp3 con ffmpeg y reproducir el fichero: retraso sin límite")
    r = sub.add_parser("retrasar")
    r.add_argument("segundos", type=float)
    r.add_argument("--socket", default=SOCKET_DEFECTO)
    a = p.parse_args(argv)
    if a.cmd == "escuchar":
        imprimir_tabla(escuchar(a.equipos, a.segundos))
    elif a.cmd == "lanzar":
        lanzar(a.url, a.socket, grabar=a.grabar)
    elif a.cmd == "retrasar":
        retrasar(a.segundos, a.socket)


if __name__ == "__main__":
    main()
