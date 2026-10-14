"""
Tests unitarios — src/ (RecordingJob, MatchScheduler, config loaders)
Cancerbero: sin mocks de red ni procesos reales. ffmpeg y yt-dlp son mockeados.

Ejecutar:
    cd /home/david/Portfolio/Mundia2026
    venv/bin/pytest tests/test_src.py -v
"""
import json
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

# Asegurar que el directorio raíz del proyecto está en el path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures comunes ──────────────────────────────────────────────────────────

PARTIDO_FIXTURE = {
    "id": 99,
    "fase": "Fase de Grupos",
    "grupo": "A",
    "fecha_es": "2026-06-20",
    "hora_es": "21:00",
    "local": "España",
    "visitante": "Marruecos",
    "sede": "MetLife Stadium",
    "ciudad": "East Rutherford, NJ",
}

CANAL_FFMPEG = {
    "nombre": "RTVE La 1",
    "pais": "ES",
    "tipo": "tv",
    "method": "ffmpeg",
    "url": "https://rtvelivestream.rtve.es/rtvesec/la1/la1_main.m3u8",
}

CANAL_YTDLP = {
    "nombre": "Canal Once",
    "pais": "MX",
    "tipo": "tv",
    "method": "yt-dlp",
    "url": "https://www.once.tv/en-vivo/",
}

CANAL_RADIO = {
    "nombre": "Cadena SER",
    "pais": "ES",
    "tipo": "radio",
    "method": "ffmpeg",
    "url": "https://playerservices.streamtheworld.com/api/livestream-redirect/CADENASER.mp3",
    "extension": "mp3",
}

CONFIG_FIXTURE = {
    "grabaciones": {
        "directorio": "/tmp/mundia_test_grabaciones",
        "formato": "{fecha}_{local}_vs_{visitante}_{canal}",
    },
    "grabacion": {
        "buffer_inicio_min": 10,
        "buffer_final_min": 45,
        "calidad": "best",
        "extension_video": "ts",
        "extension_radio": "mp3",
    },
    "canales_por_defecto": ["rtve_la1"],
    "logs": {
        "directorio": "/tmp/mundia_test_logs",
        "nivel": "INFO",
    },
}


@pytest.fixture
def tmp_output(tmp_path):
    return tmp_path / "grabaciones"


# ── RecordingJob — construcción y nombre de archivo ───────────────────────────

class TestRecordingJobNombre:
    def test_nombre_video_extension_ts(self, tmp_output):
        from src.recorder import RecordingJob
        job = RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="rtve_la1",
            canal_config=CANAL_FFMPEG,
            duracion_seg=8100,
            output_dir=tmp_output,
            config=CONFIG_FIXTURE,
        )
        assert job.output_path.suffix == ".ts"
        assert "rtve_la1" in job.output_path.name

    def test_nombre_radio_extension_mp3(self, tmp_output):
        from src.recorder import RecordingJob
        job = RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="cadena_ser",
            canal_config=CANAL_RADIO,
            duracion_seg=8100,
            output_dir=tmp_output,
            config=CONFIG_FIXTURE,
        )
        assert job.output_path.suffix == ".mp3"

    def test_repr_estado_inicial(self, tmp_output):
        from src.recorder import RecordingJob
        job = RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="rtve_la1",
            canal_config=CANAL_FFMPEG,
            duracion_seg=60,
            output_dir=tmp_output,
            config=CONFIG_FIXTURE,
        )
        assert "activa" in repr(job) or "error" in repr(job) or "completada" in repr(job)
        assert not job.iniciado
        assert not job.completado
        assert job.error is None


# ── RecordingJob — método ffmpeg directo (mockeado) ───────────────────────────

class TestRecordingJobFfmpegDirecto:
    def _make_job(self, tmp_output, canal_config=None):
        from src.recorder import RecordingJob
        return RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="rtve_la1",
            canal_config=canal_config or CANAL_FFMPEG,
            duracion_seg=10,
            output_dir=tmp_output,
            config=CONFIG_FIXTURE,
        )

    def test_grabacion_exitosa_marca_completado(self, tmp_output):
        """ffmpeg retorna 0 → job.completado == True."""
        from src.recorder import RecordingJob
        job = self._make_job(tmp_output)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")

        with patch("src.recorder.subprocess.Popen", return_value=mock_proc):
            job.iniciar()
            job.esperar(timeout=5)

        assert job.completado
        assert job.error is None

    def test_grabacion_fallida_marca_error(self, tmp_output):
        """ffmpeg retorna código distinto de 0/255 → job.error no es None."""
        job = self._make_job(tmp_output)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"error de stream")

        with patch("src.recorder.subprocess.Popen", return_value=mock_proc):
            job.iniciar()
            job.esperar(timeout=5)

        assert not job.completado
        assert job.error is not None
        assert "1" in job.error  # el código aparece en el mensaje

    def test_codigo_255_se_trata_como_exito(self, tmp_output):
        """Código 255 = detenido por señal = comportamiento normal, no error."""
        job = self._make_job(tmp_output)

        mock_proc = MagicMock()
        mock_proc.returncode = 255
        mock_proc.communicate.return_value = (b"", b"")

        with patch("src.recorder.subprocess.Popen", return_value=mock_proc):
            job.iniciar()
            job.esperar(timeout=5)

        assert job.completado
        assert job.error is None

    def test_canal_sin_url_metodo_ffmpeg_marca_error(self, tmp_output):
        """Canal method=ffmpeg sin campo url → error claro, no AttributeError."""
        canal_sin_url = {"nombre": "Sin URL", "pais": "ES", "tipo": "tv", "method": "ffmpeg"}
        job = self._make_job(tmp_output, canal_config=canal_sin_url)
        job.iniciar()
        job.esperar(timeout=5)

        assert not job.completado
        assert job.error is not None
        assert "url" in job.error.lower()

    def test_metodo_desconocido_marca_error(self, tmp_output):
        """Método inexistente → error claro."""
        canal_raro = {**CANAL_FFMPEG, "method": "magia"}
        job = self._make_job(tmp_output, canal_config=canal_raro)
        job.iniciar()
        job.esperar(timeout=5)

        assert not job.completado
        assert job.error is not None


# ── RecordingJob — método yt-dlp (mockeado) ───────────────────────────────────

class TestRecordingJobYtDlp:
    def _make_job(self, tmp_output):
        from src.recorder import RecordingJob
        return RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="canal_once",
            canal_config=CANAL_YTDLP,
            duracion_seg=10,
            output_dir=tmp_output,
            config=CONFIG_FIXTURE,
        )

    def test_ytdlp_ok_extrae_url_y_llama_ffmpeg(self, tmp_output):
        """yt-dlp devuelve URL → ffmpeg la recibe y graba."""
        job = self._make_job(tmp_output)

        ytdlp_result = MagicMock()
        ytdlp_result.returncode = 0
        ytdlp_result.stdout = "https://stream.ejemplo.com/live.m3u8\n"

        ffmpeg_proc = MagicMock()
        ffmpeg_proc.returncode = 0
        ffmpeg_proc.communicate.return_value = (b"", b"")

        with patch("src.recorder.subprocess.run", return_value=ytdlp_result), \
             patch("src.recorder.subprocess.Popen", return_value=ffmpeg_proc):
            job.iniciar()
            job.esperar(timeout=5)

        assert job.completado
        assert job.error is None

    def test_ytdlp_falla_hace_fallback_a_ffmpeg_directo(self, tmp_output):
        """yt-dlp returncode != 0 → fallback a URL original con ffmpeg."""
        job = self._make_job(tmp_output)

        ytdlp_result = MagicMock()
        ytdlp_result.returncode = 1
        ytdlp_result.stdout = ""

        ffmpeg_proc = MagicMock()
        ffmpeg_proc.returncode = 0
        ffmpeg_proc.communicate.return_value = (b"", b"")

        with patch("src.recorder.subprocess.run", return_value=ytdlp_result), \
             patch("src.recorder.subprocess.Popen", return_value=ffmpeg_proc) as mock_popen:
            job.iniciar()
            job.esperar(timeout=5)

        # El fallback llama ffmpeg con la URL original del canal
        assert job.completado
        popen_cmd = mock_popen.call_args[0][0]
        assert CANAL_YTDLP["url"] in popen_cmd

    def test_ytdlp_stdout_vacio_hace_fallback(self, tmp_output):
        """yt-dlp retorna 0 pero stdout vacío → fallback."""
        job = self._make_job(tmp_output)

        ytdlp_result = MagicMock()
        ytdlp_result.returncode = 0
        ytdlp_result.stdout = ""  # sin URL

        ffmpeg_proc = MagicMock()
        ffmpeg_proc.returncode = 0
        ffmpeg_proc.communicate.return_value = (b"", b"")

        with patch("src.recorder.subprocess.run", return_value=ytdlp_result), \
             patch("src.recorder.subprocess.Popen", return_value=ffmpeg_proc):
            job.iniciar()
            job.esperar(timeout=5)

        assert job.completado

    def test_ytdlp_dos_urls_usa_ffmpeg_dual(self, tmp_output):
        """yt-dlp devuelve dos URLs (DASH video+audio) → ffmpeg dual."""
        job = self._make_job(tmp_output)

        ytdlp_result = MagicMock()
        ytdlp_result.returncode = 0
        ytdlp_result.stdout = (
            "https://video.ejemplo.com/video.m3u8\n"
            "https://audio.ejemplo.com/audio.m3u8\n"
        )

        ffmpeg_proc = MagicMock()
        ffmpeg_proc.returncode = 0
        ffmpeg_proc.communicate.return_value = (b"", b"")

        with patch("src.recorder.subprocess.run", return_value=ytdlp_result), \
             patch("src.recorder.subprocess.Popen", return_value=ffmpeg_proc) as mock_popen:
            job.iniciar()
            job.esperar(timeout=5)

        assert job.completado
        popen_cmd = mock_popen.call_args[0][0]
        # Dual stream: dos -i en el comando
        assert popen_cmd.count("-i") == 2

    def test_ytdlp_timeout_marca_error(self, tmp_output):
        """yt-dlp lanza TimeoutExpired → error claro."""
        import subprocess
        job = self._make_job(tmp_output)

        with patch("src.recorder.subprocess.run", side_effect=subprocess.TimeoutExpired("yt-dlp", 30)):
            job.iniciar()
            job.esperar(timeout=5)

        assert not job.completado
        assert job.error is not None


# ── RecordingJob — detener() ──────────────────────────────────────────────────

class TestRecordingJobDetener:
    def test_detener_termina_proceso(self, tmp_output):
        """detener() llama terminate() en el proceso ffmpeg."""
        from src.recorder import RecordingJob

        job = RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="rtve_la1",
            canal_config=CANAL_FFMPEG,
            duracion_seg=3600,
            output_dir=tmp_output,
            config=CONFIG_FIXTURE,
        )

        mock_proc = MagicMock()
        mock_proc.returncode = 255
        # communicate() bloquea "eternamente" hasta que detener() lo interrumpa
        # Simulamos con un sleep corto para que el thread arranque
        mock_proc.communicate.side_effect = lambda: time.sleep(0.5)
        mock_proc.wait.return_value = None

        with patch("src.recorder.subprocess.Popen", return_value=mock_proc):
            job.iniciar()
            time.sleep(0.1)  # dar tiempo al thread a arrancar
            job.detener()

        mock_proc.terminate.assert_called()

    def test_detener_sin_proceso_no_explota(self, tmp_output):
        """detener() cuando no hay proceso activo no lanza excepción."""
        from src.recorder import RecordingJob
        job = RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="rtve_la1",
            canal_config=CANAL_FFMPEG,
            duracion_seg=60,
            output_dir=tmp_output,
            config=CONFIG_FIXTURE,
        )
        # No se ha llamado iniciar() → _proc_ffmpeg es None
        job.detener()  # no debe explotar


# ── MatchScheduler — lógica de programación ───────────────────────────────────

class TestMatchScheduler:
    """Tests del scheduler. Mockean cargar_config/cargar_canales/cargar_partidos
    para no depender de los ficheros reales."""

    PARTIDOS = [
        {**PARTIDO_FIXTURE, "id": 1, "fecha_es": "2099-01-01", "hora_es": "20:00"},
        {**PARTIDO_FIXTURE, "id": 2, "fecha_es": "2099-01-02", "hora_es": "20:00"},
        {**PARTIDO_FIXTURE, "id": 3, "fecha_es": "2020-01-01", "hora_es": "20:00"},  # pasado
    ]

    CANALES = {"rtve_la1": CANAL_FFMPEG}

    def _make_scheduler(self):
        from src.scheduler import MatchScheduler
        with patch("src.scheduler.cargar_config", return_value=CONFIG_FIXTURE), \
             patch("src.scheduler.cargar_canales", return_value=self.CANALES), \
             patch("src.scheduler.cargar_partidos", return_value=self.PARTIDOS):
            return MatchScheduler()

    def test_programar_todos_solo_futuros(self):
        """programar_todos() no programa partidos del pasado."""
        s = self._make_scheduler()
        with patch.object(s, "_programar_partido") as mock_prog:
            n = s.programar_todos()
        # Solo 2 partidos futuros (id=1, id=2); el pasado (id=3) se omite
        assert n == 2
        assert mock_prog.call_count == 2

    def test_partidos_pendientes_orden_cronologico(self):
        """partidos_pendientes() retorna lista ordenada por fecha."""
        s = self._make_scheduler()
        pendientes = s.partidos_pendientes()
        assert all(p["fecha_es"] > "2020-01-01" for p in pendientes)
        fechas = [p["fecha_es"] for p in pendientes]
        assert fechas == sorted(fechas)

    def test_partidos_hoy_filtra_por_fecha(self):
        """partidos_hoy() retorna solo los del día de hoy."""
        from datetime import datetime
        hoy = datetime.now().strftime("%Y-%m-%d")
        partidos_con_hoy = [
            {**PARTIDO_FIXTURE, "id": 10, "fecha_es": hoy, "hora_es": "23:59"},
            {**PARTIDO_FIXTURE, "id": 11, "fecha_es": "2099-01-01", "hora_es": "20:00"},
        ]
        from src.scheduler import MatchScheduler
        with patch("src.scheduler.cargar_config", return_value=CONFIG_FIXTURE), \
             patch("src.scheduler.cargar_canales", return_value=self.CANALES), \
             patch("src.scheduler.cargar_partidos", return_value=partidos_con_hoy):
            s = MatchScheduler()
        hoy_partidos = s.partidos_hoy()
        assert all(p["fecha_es"] == hoy for p in hoy_partidos)
        assert len(hoy_partidos) == 1

    def test_partidos_proximos_limite_n(self):
        """partidos_proximos(n) retorna como máximo n partidos."""
        s = self._make_scheduler()
        assert len(s.partidos_proximos(1)) == 1
        assert len(s.partidos_proximos(10)) == 2  # solo hay 2 futuros

    def test_grabar_ahora_partido_inexistente(self):
        """grabar_ahora() con ID inválido lanza ValueError."""
        s = self._make_scheduler()
        with pytest.raises(ValueError, match="no encontrado"):
            s.grabar_ahora(9999)

    def test_cancelar_partido_cancela_timers(self):
        """cancelar_partido() llama cancel() en todos los timers del partido."""
        s = self._make_scheduler()
        mock_timer = MagicMock()
        s._timers[1] = [mock_timer, mock_timer]
        s.cancelar_partido(1)
        assert mock_timer.cancel.call_count == 2
        assert 1 not in s._timers

    def test_cancelar_partido_inexistente_no_explota(self):
        """cancelar_partido() con ID sin timers no lanza excepción."""
        s = self._make_scheduler()
        s.cancelar_partido(9999)  # no debe explotar

    def test_canales_invalidos_se_filtran(self):
        """_canales_para_partido() descarta canal_ids que no existen en channels.yaml."""
        s = self._make_scheduler()
        partido = {**PARTIDO_FIXTURE, "canales": ["rtve_la1", "canal_no_existe"]}
        validos = s._canales_para_partido(partido)
        assert "rtve_la1" in validos
        assert "canal_no_existe" not in validos

    def test_duracion_total_seg_formula(self):
        """La duración total incluye buffer_inicio + 90min + buffer_final."""
        s = self._make_scheduler()
        cfg = CONFIG_FIXTURE["grabacion"]
        esperado = (cfg["buffer_inicio_min"] + 90 + cfg["buffer_final_min"]) * 60
        assert s._duracion_total_seg() == esperado

    def test_jobs_activos_poda_completados(self):
        """jobs_activos() elimina de _jobs los que ya completaron o fallaron."""
        s = self._make_scheduler()

        j_activo = MagicMock(iniciado=True, completado=False, error=None)
        j_completado = MagicMock(iniciado=True, completado=True, error=None)
        j_error = MagicMock(iniciado=True, completado=False, error="algo falló")

        s._jobs = [j_activo, j_completado, j_error]
        activos = s.jobs_activos()

        assert j_activo in activos
        assert j_completado not in activos
        assert j_error not in activos
        # La lista interna debe haberse podado
        assert j_completado not in s._jobs
        assert j_error not in s._jobs


# ── config.py — carga y validación ────────────────────────────────────────────

class TestCargarConfig:
    def test_config_valido(self, tmp_path):
        cfg = {"grabaciones": {"directorio": "/tmp"}, "grabacion": {}}
        f = tmp_path / "config.yaml"
        f.write_text(yaml.dump(cfg), encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.CONFIG_DIR
        try:
            cfg_mod.CONFIG_DIR = tmp_path
            resultado = cfg_mod.cargar_config()
            assert isinstance(resultado, dict)
        finally:
            cfg_mod.CONFIG_DIR = original

    def test_config_no_existe(self, tmp_path):
        from src import config as cfg_mod
        original = cfg_mod.CONFIG_DIR
        try:
            cfg_mod.CONFIG_DIR = tmp_path  # carpeta sin config.yaml
            with pytest.raises(FileNotFoundError, match="config.yaml"):
                cfg_mod.cargar_config()
        finally:
            cfg_mod.CONFIG_DIR = original

    def test_config_yaml_malformado(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("clave: [sin cerrar", encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.CONFIG_DIR
        try:
            cfg_mod.CONFIG_DIR = tmp_path
            with pytest.raises(ValueError, match="malformado"):
                cfg_mod.cargar_config()
        finally:
            cfg_mod.CONFIG_DIR = original


class TestCargarPartidos:
    def test_partidos_validos(self, tmp_path):
        data = [{"id": 1, "local": "A", "visitante": "B"}]
        f = tmp_path / "partidos.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.BASE_DIR
        try:
            cfg_mod.BASE_DIR = tmp_path
            resultado = cfg_mod.cargar_partidos()
            assert resultado == data
        finally:
            cfg_mod.BASE_DIR = original

    def test_partidos_no_existe(self, tmp_path):
        from src import config as cfg_mod
        original = cfg_mod.BASE_DIR
        try:
            cfg_mod.BASE_DIR = tmp_path
            with pytest.raises(FileNotFoundError, match="partidos.json"):
                cfg_mod.cargar_partidos()
        finally:
            cfg_mod.BASE_DIR = original

    def test_partidos_json_malformado(self, tmp_path):
        f = tmp_path / "partidos.json"
        f.write_text("{no es lista}", encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.BASE_DIR
        try:
            cfg_mod.BASE_DIR = tmp_path
            with pytest.raises((ValueError, Exception)):
                cfg_mod.cargar_partidos()
        finally:
            cfg_mod.BASE_DIR = original

    def test_partidos_no_es_lista(self, tmp_path):
        f = tmp_path / "partidos.json"
        f.write_text(json.dumps({"clave": "valor"}), encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.BASE_DIR
        try:
            cfg_mod.BASE_DIR = tmp_path
            with pytest.raises(ValueError, match="lista"):
                cfg_mod.cargar_partidos()
        finally:
            cfg_mod.BASE_DIR = original


class TestCargarCanales:
    def test_canales_validos(self, tmp_path):
        data = {"canales": {"rtve_la1": {"nombre": "La 1", "url": "http://example.com"}}}
        f = tmp_path / "channels.yaml"
        f.write_text(yaml.dump(data), encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.CONFIG_DIR
        try:
            cfg_mod.CONFIG_DIR = tmp_path
            resultado = cfg_mod.cargar_canales()
            assert "rtve_la1" in resultado
        finally:
            cfg_mod.CONFIG_DIR = original

    def test_canales_sin_url_se_filtran(self, tmp_path):
        """Canales sin URL no deben aparecer en el resultado."""
        data = {
            "canales": {
                "con_url": {"nombre": "Con URL", "url": "http://example.com"},
                "sin_url": {"nombre": "Sin URL"},
            }
        }
        f = tmp_path / "channels.yaml"
        f.write_text(yaml.dump(data), encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.CONFIG_DIR
        try:
            cfg_mod.CONFIG_DIR = tmp_path
            resultado = cfg_mod.cargar_canales()
            assert "con_url" in resultado
            assert "sin_url" not in resultado
        finally:
            cfg_mod.CONFIG_DIR = original

    def test_canales_yaml_malformado(self, tmp_path):
        f = tmp_path / "channels.yaml"
        f.write_text("canales: [no_es_dict", encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.CONFIG_DIR
        try:
            cfg_mod.CONFIG_DIR = tmp_path
            with pytest.raises(ValueError, match="malformado"):
                cfg_mod.cargar_canales()
        finally:
            cfg_mod.CONFIG_DIR = original

    def test_canales_vacio_retorna_dict_vacio(self, tmp_path):
        """channels.yaml sin sección 'canales' retorna {} sin explotar."""
        f = tmp_path / "channels.yaml"
        f.write_text("otros_datos: true\n", encoding="utf-8")
        from src import config as cfg_mod
        original = cfg_mod.CONFIG_DIR
        try:
            cfg_mod.CONFIG_DIR = tmp_path
            resultado = cfg_mod.cargar_canales()
            assert resultado == {}
        finally:
            cfg_mod.CONFIG_DIR = original


# ── construir_nombre ──────────────────────────────────────────────────────────

class TestConstruirNombre:
    def test_nombre_contiene_equipos_y_canal(self):
        from src.recorder import construir_nombre
        nombre = construir_nombre(PARTIDO_FIXTURE, "rtve_la1", CONFIG_FIXTURE)
        assert "Espaa" in nombre or "Espa" in nombre  # sanitizar elimina ñ
        assert "rtve_la1" in nombre

    def test_nombre_sin_caracteres_prohibidos(self):
        from src.recorder import construir_nombre
        nombre = construir_nombre(PARTIDO_FIXTURE, "rtve_la1", CONFIG_FIXTURE)
        # No debe contener caracteres problemáticos para sistemas de archivos
        for c in "/:*?\"<>|":
            assert c not in nombre, f"Carácter prohibido '{c}' en nombre: {nombre}"

    def test_sanitizar_acento_y_enie(self):
        from src.recorder import _sanitizar
        resultado = _sanitizar("España")
        assert isinstance(resultado, str)
        # La ñ debe eliminarse (no es ASCII simple) o mantenerse según regex
        # Lo importante: no explota
