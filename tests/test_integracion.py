"""
Test de integración — grabación real 10 segundos.
Verifica que los streams principales siguen vivos y producen ficheros válidos.

Ejecutar:
    cd /home/david/Portfolio/Mundia2026
    venv/bin/pytest tests/test_integracion.py -v -s

Nota: requiere conexión a internet y ~30s por canal.
"""
import os
import sys
import tempfile
import subprocess
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scheduler import _duracion_fichero

DURACION_TEST = 10  # segundos

STREAMS = {
    'rtve_la1':  'https://rtvelivestream.rtve.es/rtvesec/la1/la1_main_dvr.m3u8',
    'cadena_ser': 'https://playerservices.streamtheworld.com/api/livestream-redirect/CADENASER.mp3',
    'w_radio':   'https://playerservices.streamtheworld.com/api/livestream-redirect/WRADIO.mp3',
    'ard':       'https://daserste-live.ard-mcdn.de/daserste/live/hls/int/master.m3u8',
}


def _grabar_10s(url, sufijo):
    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as f:
        ruta = f.name
    result = subprocess.run(
        ['ffmpeg', '-y', '-i', url, '-t', str(DURACION_TEST), '-c', 'copy', ruta],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=60,
    )
    return ruta, result.returncode


@pytest.mark.parametrize("nombre,url", [
    ('rtve_la1',   STREAMS['rtve_la1']),
    ('cadena_ser', STREAMS['cadena_ser']),
    ('w_radio',    STREAMS['w_radio']),
])
def test_stream_produce_fichero_valido(nombre, url):
    sufijo = '.mkv' if 'la1' in nombre or 'ard' in nombre else '.mp3'
    ruta, rc = _grabar_10s(url, sufijo)
    try:
        assert rc == 0, f"{nombre}: ffmpeg salió con código {rc}"
        dur = _duracion_fichero(ruta)
        assert dur >= DURACION_TEST * 0.8, (
            f"{nombre}: duración {dur:.1f}s, esperaba ≥{DURACION_TEST * 0.8}s"
        )
    finally:
        if os.path.exists(ruta):
            os.unlink(ruta)
