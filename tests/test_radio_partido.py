"""
Tests — radio_partido.py
Cancerbero: la puntuación, la carga de radios y las órdenes a mpv, sin tocar internet
ni whisper (se inyectan grabar/transcribir falsos).

Ejecutar:
    venv/bin/pytest tests/test_radio_partido.py -v
"""
import json
import os
import socket
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import radio_partido
from radio_partido import (cargar_radios, comando_grabar_radio, comando_mpv_archivo,
                           comando_mpv_radio, escuchar, esperar_archivo, lanzar, normalizar,
                           orden_retraso, puntuar, retrasar)

RELATO = ("¡Gooool de Barcelona! Minuto 41, empata el Barcelona en el Sánchez-Pizjuán. "
          "Córner para el Sevilla, el árbitro señala falta del delantero. Tarjeta amarilla.")
NOTICIAS = ("El presidente anunció hoy nuevas medidas económicas. La inflación subió un dos "
            "por ciento en agosto y el dólar cerró estable en la jornada.")
MUSICA = "la la la baby te quiero mucho mi amor"


def test_normalizar_quita_tildes_y_mayusculas():
    assert normalizar("Árbitro  CÓRNER") == "arbitro corner"


def test_normalizar_acorta_el_grito_de_gol_pero_respeta_dobles():
    assert normalizar("¡Goooool! Sevilla") == "gol! sevilla"  # el «¡» no es ASCII y cae


def test_relato_puntua_alto_y_nombra_equipos():
    puntos, detalle = puntuar(RELATO, ["Sevilla", "Barcelona"])
    assert puntos >= 10
    assert any(d.startswith("Barcelona×2") for d in detalle)
    assert any(d.startswith("Sevilla×1") for d in detalle)
    assert "gol×1" in detalle and "corner×1" in detalle


def test_noticias_y_musica_puntuan_cero():
    assert puntuar(NOTICIAS, ["Sevilla", "Barcelona"])[0] == 0
    assert puntuar(MUSICA, ["Sevilla", "Barcelona"])[0] == 0
    assert puntuar("", ["Sevilla"]) == (0, [])


def test_equipo_pesa_mas_que_una_palabra_suelta():
    solo_equipo, _ = puntuar("juega el Sevilla", ["Sevilla"])
    solo_palabra, _ = puntuar("hubo un gol", ["Sevilla"])
    assert solo_equipo == 3 and solo_palabra == 1


def test_no_confunde_palabras_dentro_de_otras():
    # "golpe" no es "gol"; "areas" no es "area" — se exige palabra completa.
    assert puntuar("un golpe en las areas", ["X"])[0] == 0


def test_cargar_radios_desde_yaml_ordena_por_votos_y_descarta_incompletas(tmp_path):
    ruta = tmp_path / "sources.yaml"
    ruta.write_text(
        "audio:\n  ser: http://x\n  sudamerica:\n"
        "    - {nombre: Poca, pais: Chile, votos: 10, url: 'http://poca'}\n"
        "    - {nombre: Mucha, pais: Colombia, votos: 999, url: 'http://mucha'}\n"
        "    - {nombre: SinUrl, pais: Peru, votos: 5}\n", encoding="utf-8")
    radios = cargar_radios(str(ruta))
    assert [r["nombre"] for r in radios] == ["Mucha", "Poca"]
    assert radios[0]["votos"] == 999 and radios[0]["url"] == "http://mucha"


def test_cargar_radios_sin_bloque_devuelve_vacio(tmp_path):
    ruta = tmp_path / "s.yaml"
    ruta.write_text("audio:\n  ser: http://x\n", encoding="utf-8")
    assert cargar_radios(str(ruta)) == []


@pytest.mark.skipif(not os.path.exists(radio_partido.SOURCES), reason="sources.yaml no se publica")
def test_sources_reales_tienen_radios_con_url():
    radios = cargar_radios()
    assert len(radios) >= 5
    assert all(r["url"].startswith("http") for r in radios)


def test_escuchar_ordena_las_que_relatan_primero_y_marca_las_mudas():
    radios = [
        {"nombre": "Muda", "pais": "X", "votos": 99999, "url": "u1"},
        {"nombre": "Noticias", "pais": "X", "votos": 5000, "url": "u2"},
        {"nombre": "Relato", "pais": "X", "votos": 10, "url": "u3"},
    ]
    textos = {"u2": NOTICIAS, "u3": RELATO}

    def grabar_falso(url, segundos, destino):
        if url == "u1":
            return False
        with open(destino, "w") as f:
            f.write(url)
        return True

    def transcribir_falso(wav):
        with open(wav) as f:
            return textos[f.read()]

    res = escuchar(["Sevilla", "Barcelona"], 5, radios, grabar_falso, transcribir_falso)
    assert [r["nombre"] for r in res] == ["Relato", "Noticias", "Muda"]
    assert res[0]["puntos"] > 0 and res[1]["puntos"] == 0 and res[2]["puntos"] == -1
    assert res[2]["detalle"] == ["sin audio"]


def test_comando_mpv_radio_sin_video_con_cache_y_socket():
    cmd = comando_mpv_radio("http://radio", "/tmp/s")
    assert cmd[0] == "mpv" and cmd[-1] == "http://radio"
    assert "--no-video" in cmd
    assert "--cache-secs=600" in cmd and "--demuxer-readahead-secs=600" in cmd
    assert "--input-ipc-server=/tmp/s" in cmd


def test_orden_retraso_es_seek_relativo_negativo():
    orden = json.loads(orden_retraso(45))
    assert orden == {"command": ["seek", -45.0, "relative"]}
    assert orden_retraso(45).endswith("\n")


def test_retrasar_envia_la_orden_por_el_socket_y_devuelve_la_respuesta():
    carpeta = tempfile.mkdtemp()
    ruta = os.path.join(carpeta, "mpv.sock")
    recibido = {}
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(ruta)
    srv.listen(1)

    def mpv_falso():
        conn, _ = srv.accept()
        recibido["linea"] = conn.recv(4096).decode()
        conn.sendall(b'{"error":"success"}\n')
        conn.close()

    t = threading.Thread(target=mpv_falso, daemon=True)
    t.start()
    respuesta = retrasar(30, ruta)
    t.join(3)
    srv.close()
    assert json.loads(recibido["linea"]) == {"command": ["seek", -30.0, "relative"]}
    assert "success" in respuesta


def test_retrasar_sin_mpv_falla_claro():
    with pytest.raises(OSError):
        retrasar(10, "/tmp/no_existe_mpv_radio_sock")


# --- modo --grabar: ffmpeg a mp3 que crece + mpv sobre el fichero (retraso sin límite) ---

def test_comando_grabar_radio_es_ffmpeg_a_mp3_con_flush():
    cmd = comando_grabar_radio("http://radio", "/tmp/r.mp3")
    assert cmd[0] == "ffmpeg" and cmd[-1] == "/tmp/r.mp3"
    assert cmd[cmd.index("-i") + 1] == "http://radio"
    assert "libmp3lame" in cmd
    assert cmd[cmd.index("-flush_packets") + 1] == "1"


def test_comando_mpv_archivo_sin_video_keep_open_y_socket():
    cmd = comando_mpv_archivo("/tmp/r.mp3", "/tmp/s")
    assert cmd[0] == "mpv" and cmd[-1] == "/tmp/r.mp3"
    assert "--no-video" in cmd and "--keep-open=yes" in cmd
    assert "--input-ipc-server=/tmp/s" in cmd
    assert not any(a.startswith("--cache") for a in cmd)


def test_esperar_archivo_vuelve_cuando_hay_bytes_y_falla_si_no_llegan():
    tamanos = iter([0, 0, 70000])
    assert esperar_archivo("x", minimo=65536, timeout=5, dormir=lambda s: None,
                           tamano=lambda a: next(tamanos)) is True
    assert esperar_archivo("x", minimo=65536, timeout=1, dormir=lambda s: None,
                           tamano=lambda a: 10) is False


def test_lanzar_con_grabar_arranca_ffmpeg_y_luego_mpv_sobre_el_mp3(tmp_path):
    lanzados = []

    def popen_falso(cmd, **kw):
        lanzados.append(cmd)
        if cmd[0] == "ffmpeg":
            open(cmd[-1], "wb").write(b"\0" * 70000)
        assert kw["start_new_session"] is True

    archivo = lanzar("http://radio", "/tmp/s", grabar=True, popen=popen_falso, carpeta=str(tmp_path))
    assert archivo.startswith(str(tmp_path)) and archivo.endswith(".mp3")
    assert [c[0] for c in lanzados] == ["ffmpeg", "mpv"]
    assert lanzados[0][-1] == archivo and lanzados[1][-1] == archivo
    assert "--input-ipc-server=/tmp/s" in lanzados[1]


def test_lanzar_con_grabar_no_abre_mpv_si_ffmpeg_no_escribe(tmp_path):
    lanzados = []
    archivo = lanzar("http://radio", "/tmp/s", grabar=True, popen=lambda c, **k: lanzados.append(c),
                     carpeta=str(tmp_path), esperar=lambda a: False)
    assert archivo is None
    assert [c[0] for c in lanzados] == ["ffmpeg"]


def test_lanzar_sin_grabar_abre_mpv_directo_contra_la_url(tmp_path):
    lanzados = []
    archivo = lanzar("http://radio", "/tmp/s", popen=lambda c, **k: lanzados.append(c), carpeta=str(tmp_path))
    assert archivo is None
    assert [c[0] for c in lanzados] == ["mpv"] and lanzados[0][-1] == "http://radio"
