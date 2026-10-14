#!/usr/bin/env python3
"""VER EL PARTIDO A MI MANERA — la receta de las nueve sesiones, en un sitio (11-oct-2026).

    venv/bin/python ver_partido.py fuentes [--solo a07z,a028]     # mide en paralelo (30 s cada una) y ordena por errores
    venv/bin/python ver_partido.py ver a07z [--retraso 45] [--sin-xrandr]
    venv/bin/python ver_partido.py estado                          # JSON: qué está pasando
    venv/bin/python ver_partido.py parar                           # mpv por su socket, el relay por su PID real, 165 Hz

La receta (kakashis del 1 y 10-oct): calendario (web, aún a mano) → fuentes → relay con retraso SIEMPRE → sondear :8100
(200 + ≥3 EXTINF, o /estado listo) → guardián con freno y --mute=no → xrandr según fps → parar por el PID real.
Las trampas que esto cierra: el PID de `nohup $!` era el del shell; `pgrep -f` se mataba a sí mismo; mpv lanzado antes
del «listo» moría; relays zombi sin nadie escuchando; la ventana que no se podía cerrar (ahora `q` crea logs/parar).
Todo lo que toca procesos es inyectable: los tests no abren ni mpv ni relay ni internet.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PUERTO = 8100
SOCKET_MPV = "/tmp/mpv_futbol"
SALIDA = "DP-0"
MODO = "3440x1440"
HZ_REPOSO = 165
COLCHON_SUELTA = 40        # segundos de caché en mpv antes de soltar la pausa
CAIDAS_MAX, CAIDAS_VENTANA = 3, 120
UA = "Mozilla/5.0"

# Las fuentes de ronda.sh (ESPN LatAm) + las de sources.yaml laliga. El nombre del canal NUNCA es fijo (sources.yaml).
FUENTES: dict[str, str] = {
    "ultra": "http://servidor-g.ejemplo:8080/Match_TV_HD/",
    "a0dz": "http://servidor-c.ejemplo:9998/play/a0dz/",
    **{k: f"http://servidor-a.ejemplo:8000/play/{k}/" for k in ("a07z", "a080", "a081", "a07n", "a074", "a09l", "a08c", "a06a")},
    **{k: f"http://servidor-b.ejemplo:8000/play/{k}/" for k in ("a027", "a028", "a034")},
    "goltv": "http://servidor-d.ejemplo:8888/GOLTV/",
    **{f"nyaler{n}": f"https://servidor-h.ejemplo/stream/{n}/" for n in (3, 5, 16, 18, 24, 47)},
}
ETIQUETAS = {"a07z": "ESPN", "a080": "ESPN2", "a081": "ESPN3", "a07n": "ESPN4", "a074": "ESPN5", "a09l": "ESPN Premium",
             "a08c": "ESPN6", "a06a": "ESPN7", "a034": "ESPN", "a027": "ESPN7", "a028": "ESPN6", "a0dz": "ESPN Deportes US",
             "ultra": "Match! Ultra", "goltv": "GolTV LatAm"}


class FuenteDesconocida(ValueError):
    pass


# ── puros ──────────────────────────────────────────────────────────────────────────────────────────────────────

_MEDIR = re.compile(r"^(\S+)\s+errores30s=(\d+)\s*(?:(\d+),(\d+),(\d+)/(\d+))?")


def parsear_medir(linea: str) -> dict | None:
    m = _MEDIR.match((linea or "").strip())
    if not m:
        return None
    fps = round(int(m.group(5)) / int(m.group(6)), 2) if m.group(5) and int(m.group(6)) else None
    return {"id": m.group(1), "errores": int(m.group(2)), "w": int(m.group(3)) if m.group(3) else None,
            "h": int(m.group(4)) if m.group(4) else None, "fps": fps}


def hz_para(fps: float | None) -> float | None:
    """La regla de las sesiones: 120 Hz para 60 fps, 100 para 25/50, 59,97 para 29,97/30. Lo demás, no se toca."""
    if fps is None:
        return None
    if abs(fps - 60) < 1 or abs(fps - 59.94) < 0.5:
        return 120
    if abs(fps - 50) < 1 or abs(fps - 25) < 1:
        return 100
    if abs(fps - 30) < 1 or abs(fps - 29.97) < 0.5:
        return 59.97
    return None


def resolver_url(fuente: str) -> str:
    """Un id de FUENTES o una URL; siempre la base acabada en «/» (lo que `relay_lan.py --origen` pide)."""
    if fuente in FUENTES:
        return FUENTES[fuente]
    if fuente.startswith(("http://", "https://")):
        return re.sub(r"index\.m3u8$", "", fuente).rstrip("/") + "/"
    raise FuenteDesconocida(f"fuente desconocida: {fuente}. Ids: {', '.join(sorted(FUENTES))}, o una URL")


def playlist_lista(status: int | None, cuerpo: str | None) -> bool:
    return status == 200 and (cuerpo or "").count("#EXTINF") >= 3


def escribir_input_conf(raiz: Path) -> Path:
    """La `q` de mpv crea logs/parar antes de salir: el guardián no vuelve a abrir la ventana."""
    logs = Path(raiz) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / "input.conf"
    p.write_text(f'q run "/usr/bin/touch" "{logs / "parar"}"; quit\n'
                 f'ESC run "/usr/bin/touch" "{logs / "parar"}"; quit\n', encoding="utf-8")
    return p


# ── procesos (inyectables) ──────────────────────────────────────────────────────────────────────────────────────

def _http(url: str, timeout: float = 1.0) -> tuple[int | None, str | None]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:                                                    # noqa: BLE001
        return None, None


def _vivo(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError):
        return False


def _socket_cmd(ruta: str, cmd: list) -> dict | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(ruta)
            s.sendall((json.dumps({"command": cmd}) + "\n").encode())
            return json.loads(s.recv(4096).decode("utf-8", "replace").splitlines()[0])
    except Exception:                                                    # noqa: BLE001
        return None


def _socket_vivo(ruta: str) -> bool:
    return _socket_cmd(ruta, ["get_property", "pause"]) is not None


def _run(cmd: list, **k):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=k.get("timeout", 20))


def _estado_path(raiz: Path) -> Path:
    return Path(raiz) / "logs" / "ver_partido.json"


def _leer_estado(raiz: Path) -> dict:
    try:
        return json.loads(_estado_path(raiz).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"estado": "parado"}


def _escribir_estado(raiz: Path, d: dict) -> None:
    p = _estado_path(raiz)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _xrandr(hz: float | None, run=_run) -> None:
    if hz is None:
        return
    try:
        run(["xrandr", "--output", SALIDA, "--mode", MODO, "--rate", str(hz)], timeout=10)
    except Exception:                                                    # noqa: BLE001
        pass


# ── fuentes ────────────────────────────────────────────────────────────────────────────────────────────────────

def medir(fuente: str, url: str, raiz: Path = RAIZ, run=_run) -> dict:
    r = run([str(Path(raiz) / "medir.sh"), fuente, url + "index.m3u8"], timeout=70)
    d = parsear_medir((r.stdout or "").strip().splitlines()[-1] if r.stdout else "") or {"id": fuente, "errores": None, "w": None, "h": None, "fps": None}
    if d["fps"] is None and d["w"] is None:
        d["errores"] = None            # «0 errores» sin ni un frame no es limpia: es que no hay señal (403, 404, muda)
    d["url"] = url
    d["etiqueta"] = ETIQUETAS.get(fuente, "")
    return d


def fuentes(solo: list[str] | None = None, raiz: Path = RAIZ, run=_run) -> list[dict]:
    ids = [f for f in (solo or list(FUENTES)) if f in FUENTES]
    with ThreadPoolExecutor(max_workers=min(12, len(ids) or 1)) as ex:
        res = list(ex.map(lambda f: medir(f, FUENTES[f], raiz, run), ids))
    res.sort(key=lambda d: (d["errores"] is None, d["errores"] if d["errores"] is not None else 10**6))
    logs = Path(raiz) / "logs"; logs.mkdir(exist_ok=True)
    (logs / f"fuentes_{datetime.now():%Y-%m-%d_%H%M}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    (logs / "fuentes_ultimo.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


# ── el guardián ────────────────────────────────────────────────────────────────────────────────────────────────

def _cmd_mpv(raiz: Path, url: str, pausa: bool, titulo: str) -> list[str]:
    conf = escribir_input_conf(raiz)
    return ["mpv", f"--title={titulo}", *(["--pause"] if pausa else []), "--mute=no", f"--input-ipc-server={SOCKET_MPV}",
            f"--input-conf={conf}", "--cache=yes", "--cache-secs=300", "--demuxer-max-bytes=400MiB",
            "--demuxer-readahead-secs=300", "--demuxer-lavf-o=live_start_index=0", "--video-sync=display-resample",
            "--hwdec=auto", "--volume=100", url]


def _soltar_cuando_haya_colchon(ruta: str, socket_cmd=_socket_cmd, sleep=time.sleep, tope: int = 120) -> None:
    """mpv arranca en pausa; se suelta cuando la caché pasa de COLCHON_SUELTA s (o al tope, para no quedarse mudo)."""
    for _ in range(tope):
        r = socket_cmd(ruta, ["get_property", "demuxer-cache-duration"])
        if r and isinstance(r.get("data"), (int, float)) and r["data"] >= COLCHON_SUELTA:
            break
        sleep(1)
    socket_cmd(ruta, ["set_property", "pause", False])
    socket_cmd(ruta, ["set_property", "mute", False])


def guardian(raiz: Path, url: str, *, titulo: str = "Fútbol", popen=subprocess.Popen, sleep=time.sleep, soltar=None,
             ahora=time.time) -> int:
    """Relanza mpv mientras no exista logs/parar. Devuelve cuántas veces lo lanzó. Se rinde si cae CAIDAS_MAX veces en
    CAIDAS_VENTANA s (algo está roto: nada de zombis)."""
    parar = Path(raiz) / "logs" / "parar"
    soltar = soltar or (lambda ruta: _soltar_cuando_haya_colchon(ruta, sleep=sleep))
    lanzados, caidas = 0, []
    pausa = True
    while not parar.exists():
        p = popen(_cmd_mpv(raiz, url, pausa, titulo), cwd=str(raiz))
        lanzados += 1
        est = _leer_estado(raiz); est.update({"estado": "viendo", "mpv_pid": getattr(p, "pid", None)}); _escribir_estado(raiz, est)
        if pausa:
            soltar(SOCKET_MPV)
        rc = p.wait()
        pausa = False
        t = ahora()
        caidas = [c for c in caidas if t - c <= CAIDAS_VENTANA] + [t]
        if parar.exists():
            break
        if len(caidas) >= CAIDAS_MAX:
            break
        sleep(2)
    return lanzados


# ── ver / estado / parar ───────────────────────────────────────────────────────────────────────────────────────

def _medir_fps(url: str, run=_run) -> float | None:
    try:
        r = run(["ffprobe", "-v", "error", "-user_agent", UA, "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                 "-of", "csv=p=0", url + "index.m3u8"], timeout=20)
        a, b = (r.stdout or "").strip().splitlines()[0].split("/")
        return round(int(a) / int(b), 2) if int(b) else None
    except Exception:                                                    # noqa: BLE001
        return None


def ver(fuente: str, *, raiz: Path = RAIZ, retraso: int = 45, xrandr: bool = True, popen=subprocess.Popen, http=_http,
        sleep=time.sleep, guardian=guardian, medir_fps=None, run=_run, parar_fn=None) -> dict:
    url = resolver_url(fuente)
    medir_fps = medir_fps or (lambda u: _medir_fps(u, run))
    parar_fn = parar_fn or (lambda **k: parar(raiz=raiz, run=run))
    logs = Path(raiz) / "logs"; logs.mkdir(exist_ok=True)
    (logs / "parar").unlink(missing_ok=True)
    log = logs / f"relay_{fuente if fuente in FUENTES else 'url'}.log"
    relay = popen([sys.executable, str(Path(raiz) / "relay_lan.py"), "--origen", url, "--puerto", str(PUERTO), "--retraso", str(retraso)],
                  cwd=str(raiz), start_new_session=True, stdout=open(log, "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    est = {"estado": "llenando", "canal": fuente, "url": url, "relay_pid": relay.pid, "mpv_pid": None, "fps": None, "hz": None,
           "desde": datetime.now().isoformat(timespec="seconds"), "log": str(log), "retraso": retraso}
    _escribir_estado(raiz, est)
    fps = medir_fps(url)
    hz = hz_para(fps) if xrandr else None
    est.update({"fps": fps, "hz": hz}); _escribir_estado(raiz, est)
    # el sondeo: mpv NUNCA antes del «listo» (un 503 al arrancar lo mata)
    tope = 60 + 2 * retraso
    listo = False
    for _ in range(tope):
        if relay.poll() is not None:
            break
        st, cuerpo = http(f"http://127.0.0.1:{PUERTO}/estado", timeout=1.0)
        if st == 200:
            try:
                listo = bool(json.loads(cuerpo or "{}").get("listo"))
            except ValueError:
                listo = False
        if not listo:
            listo = playlist_lista(*http(f"http://127.0.0.1:{PUERTO}/index.m3u8", timeout=1.0))
        if listo:
            break
        sleep(1)
    if not listo:
        est["estado"] = "relay_no_lleno"; _escribir_estado(raiz, est)
        parar_fn(raiz=raiz)
        return est
    _xrandr(hz, run)
    titulo = f"{ETIQUETAS.get(fuente, fuente)} · relay −{retraso}s"
    try:
        guardian(raiz=raiz, url=f"http://127.0.0.1:{PUERTO}/index.m3u8", titulo=titulo, popen=popen, sleep=sleep)
    finally:
        parar_fn(raiz=raiz)
    return est


def estado(*, raiz: Path = RAIZ, http=_http, vivo=_vivo, socket_vivo=_socket_vivo) -> dict:
    est = _leer_estado(raiz)
    st, cuerpo = http(f"http://127.0.0.1:{PUERTO}/estado", timeout=1.0)
    relay = None
    if st == 200:
        try:
            relay = json.loads(cuerpo or "{}")
        except ValueError:
            relay = None
    est["relay"] = relay
    est["relay_vivo"] = vivo(est.get("relay_pid"))
    est["mpv_vivo"] = socket_vivo(SOCKET_MPV)
    if est.get("estado") != "parado" and not est["relay_vivo"] and not est["mpv_vivo"]:
        est["estado"] = "parado"
    return est


def parar(*, raiz: Path = RAIZ, kill=os.kill, run=_run, socket_cmd=_socket_cmd, vivo=_vivo, xrandr: bool = True) -> dict:
    """Idempotente: la bandera, mpv por su socket, el relay por el PID guardado (nunca pgrep -f), 165 Hz, limpiar."""
    logs = Path(raiz) / "logs"; logs.mkdir(exist_ok=True)
    (logs / "parar").touch()
    est = _leer_estado(raiz)
    socket_cmd(SOCKET_MPV, ["quit"])
    pid = est.get("relay_pid")
    if pid:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if vivo(pid):
                    kill(int(pid), sig)
                    time.sleep(0.2) if run is _run else None
            except (ProcessLookupError, PermissionError):
                break
    if xrandr:
        _xrandr(HZ_REPOSO, run)
    (logs / "parar").unlink(missing_ok=True)
    est.update({"estado": "parado", "mpv_pid": None, "parado": datetime.now().isoformat(timespec="seconds")})
    _escribir_estado(raiz, est)
    return est


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fuentes"); f.add_argument("--solo", default=None, help="ids separados por coma")
    v = sub.add_parser("ver"); v.add_argument("fuente"); v.add_argument("--retraso", type=int, default=45); v.add_argument("--sin-xrandr", action="store_true")
    sub.add_parser("estado"); sub.add_parser("parar")
    a = ap.parse_args(argv)
    if a.cmd == "fuentes":
        for d in fuentes([x.strip() for x in a.solo.split(",")] if a.solo else None):
            print(f"{d['id']:9} {str(d['errores']):>5} err/30s  {d['w'] or '—'}x{d['h'] or '—'} @{d['fps'] or '—'}  {d['etiqueta']}")
        return 0
    if a.cmd == "ver":
        try:
            est = ver(a.fuente, retraso=a.retraso, xrandr=not a.sin_xrandr)
        except FuenteDesconocida as e:
            print(e); return 2
        print(json.dumps(est, ensure_ascii=False)); return 0
    if a.cmd == "estado":
        print(json.dumps(estado(), ensure_ascii=False)); return 0
    if a.cmd == "parar":
        print(json.dumps(parar(), ensure_ascii=False)); return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
