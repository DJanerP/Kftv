"""
Tests unitarios — scheduler.py
Cancerbero: ningún cambio al scheduler sale sin pasar estos tests.

Ejecutar:
    cd /home/david/Portfolio/Mundia2026
    venv/bin/pytest tests/ -v
"""
import os
import sys
import tempfile
import subprocess
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scheduler import resolver_fuente, slug, nombre_carpeta, _ya_grabado, _duracion_fichero


# ── resolver_fuente ────────────────────────────────────────────────────────────

CANALES = {
    'España':     'https://rtve.es/la1',
    'Alemania':   'https://ard.de/live',
    'Japón':      'https://nhk.jp/live',
}
COMODIN = 'https://tudn.com/live'


def test_resolver_local_tiene_canal():
    partido = {'local': 'España', 'visitante': 'Marruecos'}
    url, canal = resolver_fuente(partido, CANALES, COMODIN)
    assert url == 'https://rtve.es/la1'
    assert canal == 'España'


def test_resolver_visitante_tiene_canal():
    partido = {'local': 'Marruecos', 'visitante': 'España'}
    url, canal = resolver_fuente(partido, CANALES, COMODIN)
    assert url == 'https://rtve.es/la1'
    assert canal == 'España'


def test_resolver_local_prioritario_sobre_visitante():
    partido = {'local': 'Alemania', 'visitante': 'Japón'}
    url, canal = resolver_fuente(partido, CANALES, COMODIN)
    assert url == 'https://ard.de/live'
    assert canal == 'Alemania'


def test_resolver_sin_canal_usa_comodin():
    partido = {'local': 'Haití', 'visitante': 'Escocia'}
    url, canal = resolver_fuente(partido, CANALES, COMODIN)
    assert url == COMODIN
    assert canal == 'TUDN'


def test_resolver_sin_canal_sin_comodin_devuelve_none():
    partido = {'local': 'Haití', 'visitante': 'Escocia'}
    url, canal = resolver_fuente(partido, CANALES, comodin=None)
    assert url is None
    assert canal is None


def test_resolver_override_por_partido_tiene_prioridad():
    """canal_url en el partido tiene prioridad sobre equipo y comodín."""
    partido = {
        'local': 'España', 'visitante': 'Japón',
        'canal_url': 'https://rtve.override/la1',
        'canal_nombre': 'RTVE',
    }
    url, canal = resolver_fuente(partido, CANALES, COMODIN)
    assert url == 'https://rtve.override/la1'
    assert canal == 'RTVE'


def test_resolver_override_gana_sobre_equipo_mapeado():
    """Si un partido tiene canal_url, ignora el canal del equipo."""
    partido = {
        'local': 'Alemania', 'visitante': 'Ecuador',
        'canal_url': 'https://rtve.es/la1',
        'canal_nombre': 'RTVE',
    }
    url, canal = resolver_fuente(partido, CANALES, COMODIN)
    # Alemania→ARD en CANALES, pero el override RTVE debe ganar
    assert url == 'https://rtve.es/la1'
    assert canal == 'RTVE'


def test_resolver_sin_override_usa_equipo_normal():
    """Sin canal_url, el comportamiento equipo→canal sigue igual."""
    partido = {'local': 'España', 'visitante': 'Marruecos'}
    url, canal = resolver_fuente(partido, CANALES, COMODIN)
    assert url == 'https://rtve.es/la1'
    assert canal == 'España'


# ── slug ──────────────────────────────────────────────────────────────────────

def test_slug_acentos():
    assert slug('España') == 'Espana'


def test_slug_espacios_a_guion():
    assert '_' in slug('Arabia Saudi')


def test_slug_trunca_a_20():
    assert len(slug('X' * 30)) <= 20


def test_slug_unicode_no_ascii():
    # Caracteres sin equivalente ASCII deben eliminarse, no reventar
    resultado = slug('日本')
    assert isinstance(resultado, str)


def test_slug_coreano():
    resultado = slug('대한민국')
    assert isinstance(resultado, str)


# ── nombre_carpeta ─────────────────────────────────────────────────────────────

def test_nombre_carpeta_grupo():
    partido = {
        'fecha_es': '2026-06-15',
        'hora_es': '19:00',
        'local': 'España',
        'visitante': 'Cabo Verde',
        'grupo': 'H',
    }
    nombre = nombre_carpeta(partido)
    assert nombre.startswith('2026-06-15')
    assert '19h00' in nombre
    assert 'Espana' in nombre
    assert 'Cabo_Verde' in nombre
    assert 'GrpH' in nombre


def test_nombre_carpeta_fase_ko():
    partido = {
        'fecha_es': '2026-07-04',
        'hora_es': '23:00',
        'local': 'Ganador R32 M1',
        'visitante': 'Ganador R32 M2',
        'grupo': None,
        'fase': 'Octavos',
    }
    nombre = nombre_carpeta(partido)
    assert 'GrpOcta' in nombre


# ── _ya_grabado / _duracion_fichero ────────────────────────────────────────────

def _crear_audio_test(duracion_seg, ruta):
    """Crea un MP3 de prueba con silencio usando ffmpeg."""
    subprocess.run(
        ['ffmpeg', '-y', '-f', 'lavfi', '-i', f'anullsrc=r=44100:cl=mono',
         '-t', str(duracion_seg), '-q:a', '9', ruta],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True,
    )


def test_duracion_fichero_no_existe():
    assert _duracion_fichero('/tmp/no_existe_mundia.mp3') == 0


def test_duracion_fichero_real():
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        ruta = f.name
    try:
        _crear_audio_test(10, ruta)
        dur = _duracion_fichero(ruta)
        assert 9 <= dur <= 11
    finally:
        os.unlink(ruta)


def test_ya_grabado_true_cuando_suficiente():
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        ruta = f.name
    try:
        _crear_audio_test(110, ruta)  # 110s ≥ 90% de 120s (108s)
        assert _ya_grabado(ruta, duracion_min=2) is True
    finally:
        os.unlink(ruta)


def test_ya_grabado_false_cuando_corto():
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        ruta = f.name
    try:
        _crear_audio_test(5, ruta)  # 5s << 90% de 120 min
        assert _ya_grabado(ruta, duracion_min=120) is False
    finally:
        os.unlink(ruta)


def test_ya_grabado_false_cuando_no_existe():
    assert _ya_grabado('/tmp/no_existe_mundia.mp3', duracion_min=120) is False
