#!/usr/bin/env python3
"""
Mundia2026 — scheduler de grabaciones
Lee partidos.json + sources.yaml y lanza ffmpeg a la hora exacta de cada partido.

Uso:
    python scheduler.py           # arranca el daemon, graba todo lo que queda
    python scheduler.py --lista   # muestra qué partidos tienen fuente y cuáles no
"""
import json
import yaml
import subprocess
import unicodedata
import os
import re
import sys
import threading
import time
import logging
from datetime import datetime
import pytz

SPAIN_TZ = pytz.timezone('Europe/Madrid')
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))


def _setup_log():
    log_path = os.path.join(BASE_DIR, 'mundia.log')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-8s  %(message)s',
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)


PIDFILE = os.path.join(BASE_DIR, 'mundia.pid')
log = logging.getLogger(__name__)


# ── Carga ─────────────────────────────────────────────────────────────────────

def cargar():
    with open(os.path.join(BASE_DIR, 'sources.yaml')) as f:
        cfg = yaml.safe_load(f)
    with open(os.path.join(BASE_DIR, 'partidos.json')) as f:
        partidos = json.load(f)
    return cfg, partidos


# ── Resolución de fuente ───────────────────────────────────────────────────────

def resolver_fuente(partido, canales, comodin=None, comodin_nombre='comodin', cazetv=None):
    # Override por partido tiene máxima prioridad (RTVE, ARD específico, etc.)
    if partido.get('canal_url'):
        return partido['canal_url'], partido.get('canal_nombre', 'override')
    for equipo in [partido.get('local', ''), partido.get('visitante', '')]:
        if equipo in canales:
            return canales[equipo], equipo
    if comodin:
        return comodin, comodin_nombre
    if cazetv:
        return cazetv, 'CazéTV'
    return None, None


def extraer_url_youtube(yt_url, nombre):
    """Extrae la URL HLS real de un directo de YouTube via yt-dlp con geo-bypass Brasil."""
    try:
        r = subprocess.run(
            ['yt-dlp', '--geo-bypass-country', 'BR',
             '-f', 'best[protocol=m3u8_native]/best',
             '--get-url', '--no-warnings', yt_url],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode == 0:
            urls = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
            if urls:
                log.info(f"CazéTV URL OK para {nombre}")
                return urls[0]
        motivo = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'sin output'
        log.warning(f"CazéTV sin stream para {nombre}: {motivo}")
    except subprocess.TimeoutExpired:
        log.warning(f"CazéTV timeout (90s) para {nombre}")
    except FileNotFoundError:
        log.error("yt-dlp no encontrado — pip install yt-dlp")
    except Exception as e:
        log.error(f"CazéTV error inesperado: {e}")
    return None


# ── Nomenclatura ───────────────────────────────────────────────────────────────

def slug(s):
    """Convierte cualquier texto Unicode a ASCII seguro para nombre de fichero."""
    # Descomponer caracteres acentuados y quedarse con la base ASCII
    s = unicodedata.normalize('NFKD', s)
    s = s.encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^\w]', '_', s)[:20].strip('_')


def nombre_carpeta(partido):
    fecha = partido['fecha_es']
    hora  = partido['hora_es'].replace(':', 'h')
    local = slug(partido['local'])
    vis   = slug(partido['visitante'])
    grupo = partido.get('grupo') or partido.get('fase', 'KO')[:4]
    return f"{fecha}_{hora}_Grp{grupo}_{local}_vs_{vis}"


# ── Comprobación de grabación existente ───────────────────────────────────────

def _duracion_fichero(ruta):
    """Devuelve la duración en segundos del fichero, o 0 si no existe o falla."""
    if not os.path.exists(ruta):
        return 0
    try:
        out = subprocess.check_output(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', ruta],
            stderr=subprocess.DEVNULL,
        )
        return float(out.strip())
    except Exception:
        return 0


def _ya_grabado(ruta, duracion_min):
    """True si el fichero existe y tiene al menos el 90% de la duración esperada."""
    dur = _duracion_fichero(ruta)
    return dur >= duracion_min * 60 * 0.9


# ── Sin canal — carpeta + nota triste ─────────────────────────────────────────

def sin_canal_partido(partido, cfg):
    antelacion_seg = cfg['antelacion_min'] * 60
    destino_base   = cfg['destino']

    fecha    = partido['fecha_es']
    hora     = partido['hora_es']
    dt_naive = datetime.strptime(f"{fecha} {hora}", '%Y-%m-%d %H:%M')
    dt_es    = SPAIN_TZ.localize(dt_naive)
    ts_inicio = dt_es.timestamp() - antelacion_seg

    carpeta     = nombre_carpeta(partido)
    dir_partido = os.path.join(destino_base, carpeta)
    ruta_nota   = os.path.join(dir_partido, 'no_grabado.txt')

    ahora  = time.time()
    espera = ts_inicio - ahora

    if espera < 0:
        return  # ya pasó

    log.info(f"SIN CANAL — en {espera/3600:.1f}h dejaré nota: {carpeta}")
    time.sleep(espera)

    # Recargar para respetar omitir:true añadido en caliente
    _, partidos_fresh = cargar()
    p_fresh = next((p for p in partidos_fresh if p['id'] == partido['id']), partido)
    if p_fresh.get('omitir'):
        log.info(f"OMITIDO (omitir:true): {carpeta}")
        return

    os.makedirs(dir_partido, exist_ok=True)
    with open(ruta_nota, 'w') as f:
        f.write(
            f":(  \n\n"
            f"Lamentablemente este partido no se ha podido grabar.\n"
            f"No encontramos un canal de televisión accesible para este encuentro.\n\n"
            f"{partido['local']} vs {partido['visitante']}\n"
            f"{fecha}  {hora}\n"
        )
    log.info(f":(  nota escrita: {carpeta}")


# ── Grabación con ffmpeg ───────────────────────────────────────────────────────

def _ffmpeg_grabar(url, ruta, duracion_seg, log_ruta, extra_flags=None):
    cmd = ['ffmpeg', '-y', '-i', url, '-t', str(duracion_seg), '-c', 'copy']
    if extra_flags:
        cmd += extra_flags
    cmd.append(ruta)
    with open(log_ruta, 'w') as lf:
        return subprocess.run(cmd, stdout=lf, stderr=lf)


def grabar_partido(partido, url, cfg):
    duracion_min   = cfg['duracion_min']
    duracion_seg   = duracion_min * 60
    antelacion_seg = cfg['antelacion_min'] * 60
    destino_base   = cfg['destino']
    canales_audio  = cfg.get('audio', {})
    comodin        = cfg.get('comodin')

    fecha    = partido['fecha_es']
    hora     = partido['hora_es']
    dt_naive = datetime.strptime(f"{fecha} {hora}", '%Y-%m-%d %H:%M')
    dt_es    = SPAIN_TZ.localize(dt_naive)
    ts_inicio = dt_es.timestamp() - antelacion_seg

    carpeta     = nombre_carpeta(partido)
    dir_partido = os.path.join(destino_base, carpeta)
    ruta_video  = os.path.join(dir_partido, 'video.mkv')

    ahora  = time.time()
    espera = ts_inicio - ahora

    # Partido ya terminado hace más de duracion_min → skip
    if espera < -duracion_seg:
        log.debug(f"SKIP (finalizado): {carpeta}")
        return

    # Vídeo ya grabado con duración suficiente → skip
    if _ya_grabado(ruta_video, duracion_min):
        log.info(f"YA EXISTE — omitiendo: {carpeta}")
        return

    if espera > 0:
        log.info(f"PROGRAMADO en {espera/3600:.1f}h → {carpeta}")
        time.sleep(espera)

    # Recargar para respetar omitir:true añadido en caliente
    _, partidos_fresh = cargar()
    p_fresh = next((p for p in partidos_fresh if p['id'] == partido['id']), partido)
    if p_fresh.get('omitir'):
        log.info(f"OMITIDO (omitir:true): {carpeta}")
        return

    os.makedirs(dir_partido, exist_ok=True)

    # Si la fuente es YouTube, extraer URL real ahora (el stream solo existe en vivo)
    if 'youtube.com' in url:
        url = extraer_url_youtube(url, carpeta)
        if not url:
            ruta_nota = os.path.join(dir_partido, 'no_grabado.txt')
            with open(ruta_nota, 'w') as f:
                f.write(
                    f":(  CazéTV no estaba en vivo al inicio del partido.\n\n"
                    f"{partido['local']} vs {partido['visitante']}\n"
                    f"{fecha}  {hora}\n"
                )
            log.warning(f"CazéTV sin stream en el momento del partido: {carpeta}")
            return

    log.info(f"▶ INICIO: {carpeta}")

    hilos = []

    # Vídeo principal (canal de país)
    hilos.append(threading.Thread(
        target=_ffmpeg_grabar,
        args=(url, ruta_video, duracion_seg,
              os.path.join(dir_partido, 'video.log'), None),
        daemon=True,
        name=f"{carpeta}-video",
    ))

    # Audios en paralelo
    for nombre_radio, url_audio in canales_audio.items():
        ruta_audio = os.path.join(dir_partido, f"{nombre_radio}.mp3")
        if _ya_grabado(ruta_audio, duracion_min):
            log.info(f"  Audio ya existe — omitiendo: {nombre_radio}")
            continue
        hilos.append(threading.Thread(
            target=_ffmpeg_grabar,
            args=(url_audio, ruta_audio, duracion_seg,
                  os.path.join(dir_partido, f"{nombre_radio}.log"), None),
            daemon=True,
            name=f"{carpeta}-{nombre_radio}",
        ))

    for t in hilos:
        t.start()
    for t in hilos:
        t.join()

    # Verificar resultados
    if _ya_grabado(ruta_video, duracion_min):
        size_mb = os.path.getsize(ruta_video) / 1_048_576
        log.info(f"✓ VIDEO OK ({size_mb:.0f} MB): {carpeta}")
    else:
        log.error(f"✗ VIDEO FALLO: {carpeta} — ver {dir_partido}/video.log")

    for nombre_radio in canales_audio:
        ruta_audio = os.path.join(dir_partido, f"{nombre_radio}.mp3")
        if _ya_grabado(ruta_audio, duracion_min):
            size_mb = os.path.getsize(ruta_audio) / 1_048_576
            log.info(f"✓ AUDIO {nombre_radio} OK ({size_mb:.0f} MB): {carpeta}")
        else:
            log.error(f"✗ AUDIO {nombre_radio} FALLO: {carpeta} — ver {dir_partido}/{nombre_radio}.log")


# ── Modo lista ─────────────────────────────────────────────────────────────────

def modo_lista(partidos, canales, comodin=None, comodin_nombre='comodin', cazetv=None):
    print(f"\n{'FECHA':<12} {'HORA':<6} {'LOCAL':<22} {'VISITANTE':<22} {'CANAL'}")
    print("─" * 90)
    con_fuente = sin_fuente = cazetv_count = 0
    for p in partidos:
        url, canal = resolver_fuente(p, canales, comodin, comodin_nombre, cazetv)
        local = p['local'][:20]
        vis   = p['visitante'][:20]
        if url and 'youtube.com' in url:
            print(f"{p['fecha_es']:<12} {p['hora_es']:<6} {local:<22} {vis:<22} CazéTV (yt-dlp)")
            cazetv_count += 1
        elif url:
            print(f"{p['fecha_es']:<12} {p['hora_es']:<6} {local:<22} {vis:<22} {canal}")
            con_fuente += 1
        else:
            print(f"{p['fecha_es']:<12} {p['hora_es']:<6} {local:<22} {vis:<22} — sin fuente")
            sin_fuente += 1
    print(f"\nCon fuente: {con_fuente}  |  CazéTV (yt-dlp): {cazetv_count}  |  Sin fuente: {sin_fuente}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    cfg, partidos = cargar()
    canales = cfg['canales']
    comodin = cfg.get('comodin')

    global log
    log = _setup_log()

    cazetv = cfg.get('cazetv')

    if '--lista' in sys.argv:
        modo_lista(partidos, canales, comodin, cfg.get('comodin_nombre', 'comodin'), cazetv)
        return

    # Instancia única — abortar si ya hay un scheduler corriendo
    if os.path.exists(PIDFILE):
        try:
            pid = int(open(PIDFILE).read().strip())
            os.kill(pid, 0)  # lanza OSError si el proceso no existe
            print(f"ERROR: ya hay un scheduler corriendo (PID {pid}). Abortando.")
            sys.exit(1)
        except (OSError, ValueError):
            pass  # proceso muerto — el pidfile es huérfano, continuar

    with open(PIDFILE, 'w') as f:
        f.write(str(os.getpid()))

    import atexit
    import signal as _signal

    def _limpiar_pidfile():
        try:
            if os.path.exists(PIDFILE):
                os.remove(PIDFILE)
        except OSError:
            pass

    atexit.register(_limpiar_pidfile)

    def _salir_limpio(sig, frame):
        _limpiar_pidfile()
        sys.exit(0)

    _signal.signal(_signal.SIGTERM, _salir_limpio)

    os.makedirs(cfg['destino'], exist_ok=True)

    hilos = []
    programados = perdidos = 0

    for p in partidos:
        url, canal = resolver_fuente(p, canales, comodin, cfg.get('comodin_nombre', 'comodin'), cazetv)
        if url is None:
            perdidos += 1
            t = threading.Thread(
                target=sin_canal_partido,
                args=(p, cfg),
                daemon=False,
                name=f"sincal-{p['id']}",
            )
        else:
            programados += 1
            t = threading.Thread(
                target=grabar_partido,
                args=(p, url, cfg),
                daemon=False,
                name=f"partido-{p['id']}",
            )
        t.start()
        hilos.append(t)

    log.info(f"Daemon arrancado — programados: {programados} | sin canal: {perdidos}")

    for t in hilos:
        t.join()

    log.info("Todos los partidos procesados. Daemon terminado.")


if __name__ == '__main__':
    main()
