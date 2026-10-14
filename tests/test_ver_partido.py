"""ver_partido.py — la receta de las nueve sesiones, por fin en un sitio (11-oct-2026, fase 6 del repensado 2 de Khauron).

Sin internet, sin mpv, sin relay: todo lo que lanza procesos es inyectable. Las trampas registradas se prueban:
el PID real del relay (no el de nohup), nada de pgrep -f, mpv solo después del «listo», la `q` que crea logs/parar,
parar sin zombis.

    venv/bin/pytest tests/test_ver_partido.py -v
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ver_partido as V  # noqa: E402


# ── puros ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_parsear_medir():
    assert V.parsear_medir("a07z errores30s=3  1920,1080,30000/1001") == {"id": "a07z", "errores": 3, "w": 1920, "h": 1080, "fps": 29.97}
    assert V.parsear_medir("a0dz errores30s=233  ") == {"id": "a0dz", "errores": 233, "w": None, "h": None, "fps": None}
    assert V.parsear_medir("basura") is None


@pytest.mark.parametrize("fps,hz", [(60, 120), (59.94, 120), (50, 100), (25, 100), (29.97, 59.97), (30, 59.97), (None, None), (24, None)])
def test_hz_para(fps, hz):
    assert V.hz_para(fps) == hz


def test_resolver_url_por_id_o_url_literal():
    assert V.resolver_url("a07z") == "http://servidor-a.ejemplo:8000/play/a07z/"
    assert V.resolver_url("a028") == "http://servidor-b.ejemplo:8000/play/a028/"
    assert V.resolver_url("http://x/play/zz/index.m3u8") == "http://x/play/zz/"
    assert V.resolver_url("http://x/play/zz/") == "http://x/play/zz/"
    with pytest.raises(V.FuenteDesconocida):
        V.resolver_url("nada")


def test_sondeo_listo_exige_200_y_tres_extinf():
    ok = "#EXTM3U\n#EXTINF:3.0,\ns1.ts\n#EXTINF:3.0,\ns2.ts\n#EXTINF:3.0,\ns3.ts\n"
    assert V.playlist_lista(200, ok) is True
    assert V.playlist_lista(200, "#EXTM3U\n#EXTINF:3.0,\ns1.ts\n") is False
    assert V.playlist_lista(503, "Aun llenando el buffer") is False


def test_input_conf_lleva_la_q_que_crea_parar(tmp_path):
    p = V.escribir_input_conf(tmp_path)
    t = p.read_text(encoding="utf-8")
    assert "q " in t and "logs/parar" in t and "quit" in t


def test_medir_sin_frame_no_es_limpia_es_muda():
    class R: stdout = "ultra errores30s=0  \n"
    d = V.medir("ultra", "http://x/", raiz=Path("/tmp"), run=lambda cmd, **k: R())
    assert d["errores"] is None and d["fps"] is None                        # 403/404: sin señal, al final de la lista


# ── estado y parar ────────────────────────────────────────────────────────────────────────────────────────────

def test_estado_sin_nada_es_parado(tmp_path):
    e = V.estado(raiz=tmp_path, http=lambda url, timeout=1.0: (None, None))
    assert e["estado"] == "parado" and e["relay"] is None and e["mpv_vivo"] is False


def test_estado_viendo_lee_el_json_el_relay_y_el_socket(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "ver_partido.json").write_text(json.dumps({"estado": "viendo", "canal": "a07z", "relay_pid": 4242, "fps": 29.97, "hz": 59.97}), encoding="utf-8")
    e = V.estado(raiz=tmp_path, http=lambda url, timeout=1.0: (200, json.dumps({"listo": True, "colchon": 15, "fallos": 0})),
                 vivo=lambda pid: pid == 4242, socket_vivo=lambda ruta: True)
    assert e["estado"] == "viendo" and e["relay"]["listo"] is True and e["relay_vivo"] is True and e["mpv_vivo"] is True


def test_parar_mata_el_pid_real_y_limpia(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "ver_partido.json").write_text(json.dumps({"estado": "viendo", "canal": "a07z", "relay_pid": 4242, "hz": 59.97}), encoding="utf-8")
    matados, corridos, enviados = [], [], []
    r = V.parar(raiz=tmp_path, kill=lambda pid, sig: matados.append((pid, sig)), run=lambda cmd, **k: corridos.append(cmd),
                socket_cmd=lambda ruta, cmd: enviados.append(cmd), vivo=lambda pid: pid == 4242, xrandr=True)
    assert (4242, 15) in matados                                                  # el PID guardado, nunca pgrep -f
    assert any("quit" in str(c) for c in enviados)                                # mpv se cierra por su socket
    assert any("xrandr" in c[0] and "165" in c for c in corridos)                 # el monitor vuelve a 165
    assert not (tmp_path / "logs" / "parar").exists()                             # la bandera no se queda puesta
    assert json.loads((tmp_path / "logs" / "ver_partido.json").read_text())["estado"] == "parado" and r["estado"] == "parado"


def test_parar_sin_nada_no_revienta(tmp_path):
    r = V.parar(raiz=tmp_path, kill=lambda *a: (_ for _ in ()).throw(ProcessLookupError()), run=lambda cmd, **k: None,
                socket_cmd=lambda *a: None, vivo=lambda pid: False, xrandr=False)
    assert r["estado"] == "parado"


# ── el guardián ───────────────────────────────────────────────────────────────────────────────────────────────

class _Proc:
    def __init__(self, rc=0):
        self.rc = rc; self.pid = 777
    def wait(self): return self.rc
    def poll(self): return self.rc


def test_guardian_relanza_mientras_no_hay_parar_y_se_rinde_a_la_tercera_caida(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    lanzados = []
    def popen(cmd, **k):
        lanzados.append(cmd)
        if len(lanzados) == 2:
            (tmp_path / "logs" / "parar").touch()                                 # David pulsa q en la segunda
        return _Proc(0)
    n = V.guardian(raiz=tmp_path, url="http://127.0.0.1:8100/index.m3u8", popen=popen, sleep=lambda s: None,
                   soltar=lambda ruta: None, ahora=iter([0, 1, 2, 3, 4, 5]).__next__)
    assert n == 2 and "--pause" in lanzados[0] and "--pause" not in lanzados[1] and "--mute=no" in lanzados[0]
    assert any(str(a).startswith("--input-conf=") for a in lanzados[0])
    # tres caídas seguidas en menos de dos minutos sin `parar`: se rinde (nada de zombis)
    (tmp_path / "logs" / "parar").unlink()
    lanzados.clear()
    n = V.guardian(raiz=tmp_path, url="u", popen=lambda cmd, **k: (lanzados.append(cmd), _Proc(1))[1], sleep=lambda s: None,
                   soltar=lambda ruta: None, ahora=iter([0, 10, 20, 30, 40, 50]).__next__)
    assert n == 3 and len(lanzados) == 3


def test_ver_lanza_el_relay_guarda_su_pid_y_no_lanza_mpv_antes_del_listo(tmp_path):
    (tmp_path / "logs").mkdir()
    orden = []
    class _Relay:
        pid = 9999
        def poll(self): return None
    sondeos = iter([(503, "Aun llenando"), (200, "#EXTM3U\n#EXTINF:3,\na\n#EXTINF:3,\nb\n#EXTINF:3,\nc\n")])
    r = V.ver("a07z", raiz=tmp_path, retraso=45, popen=lambda cmd, **k: (orden.append(("relay", cmd)), _Relay())[1],
              http=lambda url, timeout=1.0: next(sondeos), sleep=lambda s: None,
              guardian=lambda **k: (orden.append(("mpv", k["url"])), 1)[1], medir_fps=lambda url: 29.97,
              run=lambda cmd, **k: orden.append(("run", cmd)), parar_fn=lambda **k: orden.append(("parar", None)))
    assert orden[0][0] == "relay" and "--retraso" in orden[0][1] and "45" in orden[0][1] and "--puerto" in orden[0][1]
    assert orden[0][0] == "relay" and orden.index(("mpv", "http://127.0.0.1:8100/index.m3u8")) > 0   # mpv solo tras el «listo»
    j = json.loads((tmp_path / "logs" / "ver_partido.json").read_text())
    assert j["relay_pid"] == 9999 and j["canal"] == "a07z" and j["hz"] == 59.97 and r["canal"] == "a07z"
    assert any(c[0] == "run" and "xrandr" in c[1][0] and "59.97" in c[1] for c in orden)
    assert orden[-1][0] == "parar"                                                 # al salir el guardián, se para todo
