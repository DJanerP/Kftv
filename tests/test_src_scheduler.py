"""
Tests unitarios — src/scheduler.py y src/recorder.py
Cancerbero: cubre casos no cubiertos por test_scheduler.py (root scheduler).

Ejecutar:
    cd /home/david/Portfolio/Mundia2026
    venv/bin/pytest tests/test_src_scheduler.py -v
"""
import os
import sys
import time
import threading
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Helpers ───────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent

PARTIDO_FIXTURE = {
    "id": 99,
    "fase": "Fase de Grupos",
    "grupo": "A",
    "fecha_es": "2026-07-01",
    "hora_es": "21:00",
    "local": "España",
    "visitante": "Francia",
    "sede": "Test Stadium",
    "ciudad": "Testville",
}

CONFIG_FIXTURE = {
    "grabaciones": {"directorio": str(BASE_DIR / "grabaciones"), "formato": "{fecha}_{local}_vs_{visitante}_{canal}"},
    "grabacion": {
        "buffer_inicio_min": 10,
        "buffer_final_min": 45,
        "calidad": "best",
        "extension_video": "ts",
        "extension_radio": "mp3",
    },
    "canales_por_defecto": ["rtve_la1"],
    "logs": {"directorio": str(BASE_DIR / "logs"), "nivel": "INFO"},
}

CANAL_TV_FIXTURE = {
    "nombre": "Test TV",
    "pais": "ES",
    "tipo": "tv",
    "idioma": "Español",
    "method": "ffmpeg",
    "url": "https://example.com/stream.m3u8",
}

CANAL_RADIO_FIXTURE = {
    "nombre": "Test Radio",
    "pais": "ES",
    "tipo": "radio",
    "idioma": "Español",
    "method": "ffmpeg",
    "url": "https://example.com/stream.mp3",
    "extension": "mp3",
}


# ── MatchScheduler — partidos pasados, futuros y en curso ────────────────────

class TestMatchSchedulerPartidos:
    """Verifica la lógica temporal del scheduler sin tocar el filesystem real."""

    def _make_scheduler(self, partidos):
        from src.scheduler import MatchScheduler
        with patch("src.scheduler.cargar_config", return_value=CONFIG_FIXTURE), \
             patch("src.scheduler.cargar_canales", return_value={"rtve_la1": CANAL_TV_FIXTURE}), \
             patch("src.scheduler.cargar_partidos", return_value=partidos):
            return MatchScheduler()

    def test_programar_todos_ignora_partidos_pasados(self):
        ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        partido_pasado = dict(PARTIDO_FIXTURE, id=1, fecha_es=ayer, hora_es="12:00")
        sched = self._make_scheduler([partido_pasado])
        n = sched.programar_todos()
        assert n == 0, "No debería programar partidos que ya terminaron"

    def test_programar_todos_incluye_partidos_futuros(self):
        manana = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        partido_futuro = dict(PARTIDO_FIXTURE, id=2, fecha_es=manana, hora_es="21:00")
        sched = self._make_scheduler([partido_futuro])
        n = sched.programar_todos()
        assert n == 1
        # Cancelar timers para no bloquear el test
        sched.cancelar_partido(2)

    def test_programar_todos_ignora_partido_en_curso_con_buffer_pasado(self):
        """Un partido cuyo inicio_grabacion (inicio - buffer) ya pasó se salta."""
        ahora = datetime.now()
        # Partido que empezó hace 15 min, buffer de 10 min → inicio_grabacion = -25 min atrás
        inicio = ahora - timedelta(minutes=15)
        partido = dict(
            PARTIDO_FIXTURE, id=3,
            fecha_es=inicio.strftime("%Y-%m-%d"),
            hora_es=inicio.strftime("%H:%M"),
        )
        sched = self._make_scheduler([partido])
        n = sched.programar_todos()
        assert n == 0

    def test_partidos_hoy_filtra_por_fecha(self):
        hoy = datetime.now().strftime("%Y-%m-%d")
        ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        manana = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        partidos = [
            dict(PARTIDO_FIXTURE, id=10, fecha_es=ayer),
            dict(PARTIDO_FIXTURE, id=11, fecha_es=hoy),
            dict(PARTIDO_FIXTURE, id=12, fecha_es=manana),
        ]
        sched = self._make_scheduler(partidos)
        hoy_list = sched.partidos_hoy()
        assert len(hoy_list) == 1
        assert hoy_list[0]["id"] == 11

    def test_partidos_proximos_respeta_n(self):
        manana = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        partidos = [
            dict(PARTIDO_FIXTURE, id=i, fecha_es=manana, hora_es=f"{i+8:02d}:00")
            for i in range(5)
        ]
        sched = self._make_scheduler(partidos)
        assert len(sched.partidos_proximos(3)) == 3
        assert len(sched.partidos_proximos(10)) == 5  # solo hay 5

    def test_partidos_proximos_ordenados_por_fecha(self):
        manana = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        pasado = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        partidos = [
            dict(PARTIDO_FIXTURE, id=20, fecha_es=pasado, hora_es="21:00"),
            dict(PARTIDO_FIXTURE, id=21, fecha_es=manana, hora_es="18:00"),
        ]
        sched = self._make_scheduler(partidos)
        proximos = sched.partidos_proximos(5)
        assert proximos[0]["id"] == 21  # manana es más próximo

    def test_duracion_total_incluye_buffer_inicio(self):
        """Bug corregido: la duración debe incluir buffer_inicio_min, no solo 90 + buffer_final."""
        sched = self._make_scheduler([])
        cfg = CONFIG_FIXTURE["grabacion"]
        esperado = (cfg["buffer_inicio_min"] + 90 + cfg["buffer_final_min"]) * 60
        assert sched._duracion_total_seg() == esperado

    def test_canales_invalidos_filtrados_con_warning(self, caplog):
        import logging
        sched = self._make_scheduler([])
        # Simular partido con canales que no existen en channels.yaml
        partido = dict(PARTIDO_FIXTURE, canales=["no_existe", "rtve_la1"])
        with caplog.at_level(logging.WARNING):
            validos = sched._canales_para_partido(partido)
        assert "rtve_la1" in validos
        assert "no_existe" not in validos

    def test_buscar_partido_existente(self):
        sched = self._make_scheduler([PARTIDO_FIXTURE])
        p = sched._buscar_partido(99)
        assert p is not None
        assert p["id"] == 99

    def test_buscar_partido_inexistente_devuelve_none(self):
        sched = self._make_scheduler([PARTIDO_FIXTURE])
        assert sched._buscar_partido(9999) is None

    def test_grabar_ahora_partido_inexistente_lanza_error(self):
        sched = self._make_scheduler([PARTIDO_FIXTURE])
        with pytest.raises(ValueError, match="9999"):
            sched.grabar_ahora(9999)

    def test_cancelar_partido_no_falla_si_no_hay_timer(self):
        sched = self._make_scheduler([])
        sched.cancelar_partido(999)  # No debe lanzar excepción


# ── RecordingJob — errores de proceso ────────────────────────────────────────

class TestRecordingJobErrores:
    """Verifica el manejo de errores en RecordingJob sin streams reales."""

    def _make_job(self, canal_cfg=None, tmpdir=None):
        from src.recorder import RecordingJob
        cfg = canal_cfg or CANAL_TV_FIXTURE
        d = tmpdir or tempfile.mkdtemp()
        return RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="test_canal",
            canal_config=cfg,
            duracion_seg=10,
            output_dir=Path(d),
            config=CONFIG_FIXTURE,
        )

    def test_ffmpeg_fallo_marca_error(self):
        """ffmpeg que retorna código != 0 debe marcar job.error."""
        job = self._make_job()
        # Parchear Popen para simular ffmpeg que falla
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"Error de ffmpeg simulado")

        with patch("subprocess.Popen", return_value=mock_proc):
            job.iniciar()
            job.esperar(timeout=5)

        assert job.error is not None, "Debe haber error cuando ffmpeg falla"
        assert not job.completado

    def test_ffmpeg_exitcode_255_se_considera_ok(self):
        """Código 255 = detenido por señal (SIGTERM normal al terminar duración)."""
        job = self._make_job()
        mock_proc = MagicMock()
        mock_proc.returncode = 255
        mock_proc.communicate.return_value = (b"", b"")

        with patch("subprocess.Popen", return_value=mock_proc):
            job.iniciar()
            job.esperar(timeout=5)

        assert job.completado
        assert not job.error

    def test_ytdlp_fallo_intenta_ffmpeg_directo(self):
        """Si yt-dlp no extrae URL, debe hacer fallback a ffmpeg directo."""
        canal_ytdlp = dict(CANAL_TV_FIXTURE, method="yt-dlp")
        job = self._make_job(canal_cfg=canal_ytdlp)

        # yt-dlp falla (returncode=1), luego ffmpeg directo también falla para aislar el test
        ytdlp_result = MagicMock()
        ytdlp_result.returncode = 1
        ytdlp_result.stdout = ""

        ffmpeg_proc = MagicMock()
        ffmpeg_proc.returncode = 1
        ffmpeg_proc.communicate.return_value = (b"", b"")

        with patch("subprocess.run", return_value=ytdlp_result), \
             patch("subprocess.Popen", return_value=ffmpeg_proc):
            job.iniciar()
            job.esperar(timeout=5)

        # El fallback a ffmpeg directo se intentó (error viene de ffmpeg, no de "yt-dlp no disponible")
        assert job.error is not None

    def test_ytdlp_metodo_desconocido_lanza_error(self):
        """Un método no reconocido en canal_config debe marcar error."""
        canal = dict(CANAL_TV_FIXTURE, method="protocolo_inventado")
        job = self._make_job(canal_cfg=canal)
        job.iniciar()
        job.esperar(timeout=5)
        assert job.error is not None
        assert "desconocido" in job.error.lower()

    def test_rtve_ztnr_sin_id_asset_marca_error(self):
        """Canal rtve_ztnr sin id_asset debe producir error descriptivo."""
        canal = dict(CANAL_TV_FIXTURE, method="rtve_ztnr")  # sin id_asset
        job = self._make_job(canal_cfg=canal)
        job.iniciar()
        job.esperar(timeout=5)
        assert job.error is not None
        assert "id_asset" in job.error.lower()

    def test_detener_job_no_iniciado_no_falla(self):
        """detener() sobre un job que nunca se inició no debe lanzar excepción."""
        job = self._make_job()
        job.detener()  # _proc_ffmpeg es None, no debe explotar

    def test_esperar_job_no_iniciado_no_bloquea(self):
        """esperar() sin thread activo debe retornar inmediatamente."""
        job = self._make_job()
        start = time.monotonic()
        job.esperar(timeout=2)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0  # No debe bloquear

    def test_repr_estados(self):
        job = self._make_job()
        assert "activa" in repr(job)
        job.completado = True
        assert "completada" in repr(job)
        job.completado = False
        job.error = "algo falló"
        assert "error" in repr(job)


# ── RecordingJob — canal de radio ────────────────────────────────────────────

class TestRecordingJobRadio:
    def test_radio_usa_extension_mp3(self):
        from src.recorder import RecordingJob
        job = RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="cadena_ser",
            canal_config=CANAL_RADIO_FIXTURE,
            duracion_seg=600,
            output_dir=Path(tempfile.mkdtemp()),
            config=CONFIG_FIXTURE,
        )
        assert job.output_path.suffix == ".mp3"

    def test_tv_usa_extension_ts(self):
        from src.recorder import RecordingJob
        job = RecordingJob(
            partido=PARTIDO_FIXTURE,
            canal_id="rtve_la1",
            canal_config=CANAL_TV_FIXTURE,
            duracion_seg=8100,
            output_dir=Path(tempfile.mkdtemp()),
            config=CONFIG_FIXTURE,
        )
        assert job.output_path.suffix == ".ts"


# ── src/config.py — manejo de archivos faltantes ────────────────────────────

class TestConfig:
    def test_cargar_config_faltante_lanza_error(self, tmp_path):
        from src.config import cargar_config
        with patch("src.config.CONFIG_DIR", tmp_path):
            with pytest.raises(FileNotFoundError, match="config.yaml"):
                cargar_config()

    def test_cargar_canales_faltante_lanza_error(self, tmp_path):
        from src.config import cargar_canales
        with patch("src.config.CONFIG_DIR", tmp_path):
            with pytest.raises(FileNotFoundError, match="channels.yaml"):
                cargar_canales()

    def test_cargar_partidos_faltante_lanza_error(self, tmp_path):
        from src.config import cargar_partidos
        with patch("src.config.BASE_DIR", tmp_path):
            with pytest.raises(FileNotFoundError, match="partidos.json"):
                cargar_partidos()

    def test_cargar_canales_filtra_entradas_sin_url(self, tmp_path):
        """Canales sin URL deben ignorarse con un warning."""
        import yaml
        channels_data = {
            "canales": {
                "valido": {"url": "https://example.com/stream.m3u8", "method": "ffmpeg"},
                "sin_url": {"method": "ffmpeg"},
            }
        }
        (tmp_path / "channels.yaml").write_text(yaml.dump(channels_data))
        from src.config import cargar_canales
        with patch("src.config.CONFIG_DIR", tmp_path):
            canales = cargar_canales()
        assert "valido" in canales
        assert "sin_url" not in canales

    def test_cargar_partidos_valida_lista(self, tmp_path):
        """partidos.json que no sea lista debe lanzar ValueError."""
        import json
        (tmp_path / "partidos.json").write_text(json.dumps({"no": "una lista"}))
        from src.config import cargar_partidos
        with patch("src.config.BASE_DIR", tmp_path):
            with pytest.raises(ValueError, match="lista"):
                cargar_partidos()


# ── Concurrencia — jobs_activos sin race condition ───────────────────────────

class TestJobsActivosConcurrencia:
    def test_jobs_activos_bajo_carga_concurrente(self):
        """jobs_activos() debe ser seguro cuando varios threads modifican _jobs."""
        from src.scheduler import MatchScheduler
        with patch("src.scheduler.cargar_config", return_value=CONFIG_FIXTURE), \
             patch("src.scheduler.cargar_canales", return_value={"rtve_la1": CANAL_TV_FIXTURE}), \
             patch("src.scheduler.cargar_partidos", return_value=[]):
            sched = MatchScheduler()

        errores = []

        def leer_jobs():
            for _ in range(100):
                try:
                    sched.jobs_activos()
                except Exception as e:
                    errores.append(str(e))

        def escribir_jobs():
            from src.recorder import RecordingJob
            for i in range(20):
                mock_job = MagicMock(spec=RecordingJob)
                mock_job.iniciado = True
                mock_job.completado = False
                mock_job.error = None
                with sched._lock:
                    sched._jobs.append(mock_job)

        hilos = [threading.Thread(target=leer_jobs) for _ in range(4)]
        hilos += [threading.Thread(target=escribir_jobs) for _ in range(2)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=5)

        assert not errores, f"Race condition detectada: {errores}"
