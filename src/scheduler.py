"""
Scheduler de partidos.

Lee partidos.json y programa una grabación por cada partido futuro usando
threading.Timer. Cada Timer dispara `buffer_inicio_min` antes del partido.

Ciclo de vida:
  1. MatchScheduler.cargar()    → lee partidos + canales + config
  2. MatchScheduler.programar() → crea Timers para todos los partidos futuros
  3. Timers disparan → lanzan RecordingJob para cada canal configurado
"""
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .config import cargar_config, cargar_canales, cargar_partidos
from .recorder import RecordingJob, grabar_ahora

logger = logging.getLogger(__name__)

FECHA_FMT = "%Y-%m-%d %H:%M"


def _parse_datetime(partido: dict) -> datetime:
    """Convierte fecha_es + hora_es a datetime."""
    return datetime.strptime(
        f"{partido['fecha_es']} {partido['hora_es']}", FECHA_FMT
    )


class MatchScheduler:
    """Gestiona todas las grabaciones programadas del Mundial."""

    def __init__(self):
        self.config = cargar_config()
        self.canales = cargar_canales()
        self.partidos = cargar_partidos()
        self._timers: dict[int, list[threading.Timer]] = {}
        self._jobs: list[RecordingJob] = []
        self._lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────
    # API pública
    # ──────────────────────────────────────────────────────────────

    def programar_todos(self) -> int:
        """
        Programa grabaciones para todos los partidos que aún no han empezado.
        Retorna el número de partidos programados.
        """
        ahora = datetime.now()
        cfg = self.config["grabacion"]
        buffer_inicio = timedelta(minutes=cfg["buffer_inicio_min"])
        programados = 0

        for partido in self.partidos:
            inicio = _parse_datetime(partido)
            inicio_grabacion = inicio - buffer_inicio

            if partido.get("omitir"):
                logger.info(f"OMITIDO (omitir:true): {partido['local']}_vs_{partido['visitante']}")
                continue

            if inicio_grabacion <= ahora:
                # Partido ya empezado o pasado — saltar
                continue

            segundos = (inicio_grabacion - ahora).total_seconds()
            self._programar_partido(partido, segundos)
            programados += 1

        logger.info(f"Partidos programados: {programados}")
        return programados

    def programar_partido(self, partido_id: int) -> bool:
        """Programa un partido concreto por ID. Retorna True si se programó."""
        partido = self._buscar_partido(partido_id)
        if not partido:
            logger.error(f"Partido {partido_id} no encontrado.")
            return False

        ahora = datetime.now()
        cfg = self.config["grabacion"]
        inicio = _parse_datetime(partido)
        inicio_grabacion = inicio - timedelta(minutes=cfg["buffer_inicio_min"])
        segundos = (inicio_grabacion - ahora).total_seconds()

        if segundos < -7200:
            logger.warning(f"Partido {partido_id} terminó hace más de 2 horas.")
            return False

        # Si el partido ya empezó pero no terminó, grabamos desde ahora
        if segundos < 0:
            logger.info(f"Partido {partido_id} en curso. Grabando desde ahora.")
            segundos = 0

        self._programar_partido(partido, segundos)
        return True

    def grabar_ahora(self, partido_id: int) -> list[RecordingJob]:
        """Lanza grabación inmediata de un partido. Útil para pruebas o recuperación."""
        partido = self._buscar_partido(partido_id)
        if not partido:
            raise ValueError(f"Partido {partido_id} no encontrado.")
        return self._lanzar_grabaciones(partido)

    def cancelar_partido(self, partido_id: int):
        """Cancela todos los timers de un partido."""
        with self._lock:
            timers = self._timers.pop(partido_id, [])
            for t in timers:
                t.cancel()
            logger.info(f"Cancelados {len(timers)} timer(s) para partido {partido_id}.")

    def partidos_pendientes(self) -> list[dict]:
        """Retorna lista de partidos aún no grabados, ordenados por fecha."""
        ahora = datetime.now()
        pendientes = []
        for partido in self.partidos:
            inicio = _parse_datetime(partido)
            if inicio > ahora:
                pendientes.append(partido)
        return sorted(pendientes, key=_parse_datetime)

    def partidos_hoy(self) -> list[dict]:
        """Retorna partidos de hoy."""
        hoy = datetime.now().strftime("%Y-%m-%d")
        return [p for p in self.partidos if p["fecha_es"] == hoy]

    def partidos_proximos(self, n: int = 5) -> list[dict]:
        """Retorna los próximos N partidos."""
        return self.partidos_pendientes()[:n]

    def jobs_activos(self) -> list[RecordingJob]:
        """Retorna grabaciones en curso (snapshot bajo lock). Poda jobs terminados."""
        with self._lock:
            # Podar jobs ya terminados para evitar que la lista crezca sin límite
            self._jobs = [j for j in self._jobs if not j.completado and not j.error]
            snapshot = list(self._jobs)
        # Leer atributos de los jobs fuera del lock — son escritos por el thread del job,
        # pero completado/error son monotónicos (False→True) así que leerlos sin lock es seguro.
        return [j for j in snapshot if j.iniciado]

    # ──────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────

    def _buscar_partido(self, partido_id: int) -> dict | None:
        for p in self.partidos:
            if p["id"] == partido_id:
                return p
        return None

    def _canales_para_partido(self, partido: dict) -> list[str]:
        """
        Retorna los IDs de canales a grabar para este partido.
        Si el partido tiene campo "canales", usa ese.
        Si no, usa canales_por_defecto del config.
        """
        ids = partido.get("canales") or self.config.get("canales_por_defecto", ["rtve_la1"])
        # Filtrar canales que existen en channels.yaml
        validos = [c for c in ids if c in self.canales]
        if len(validos) < len(ids):
            ignorados = set(ids) - set(validos)
            logger.warning(f"Canales no encontrados en channels.yaml, ignorados: {ignorados}")
        return validos

    def _duracion_total_seg(self) -> int:
        """
        Duración real de grabación en segundos.
        El timer dispara buffer_inicio_min ANTES del pitido, así que la grabación
        debe cubrir: buffer_inicio + 90min de partido + buffer_final.
        """
        cfg = self.config["grabacion"]
        return (cfg["buffer_inicio_min"] + 90 + cfg["buffer_final_min"]) * 60

    def _programar_partido(self, partido: dict, segundos: float):
        """Crea un threading.Timer para el partido."""
        pid = partido["id"]
        inicio = _parse_datetime(partido)
        logger.info(
            f"Programando partido #{pid}: {partido['local']} vs {partido['visitante']} "
            f"el {inicio.strftime('%d/%m %H:%M')} (en {segundos/3600:.1f}h)"
        )

        timer = threading.Timer(
            segundos,
            self._timer_callback,
            args=[partido],
        )
        timer.daemon = True
        timer.name = f"partido-{pid}"
        timer.start()

        with self._lock:
            self._timers.setdefault(pid, []).append(timer)

    def _timer_callback(self, partido: dict):
        """Dispara cuando llega la hora. Lanza los RecordingJobs."""
        pid = partido["id"]
        logger.info(
            f"▶ Timer disparado: partido #{pid} "
            f"{partido['local']} vs {partido['visitante']}"
        )
        jobs = self._lanzar_grabaciones(partido)
        with self._lock:
            self._jobs.extend(jobs)
            # Limpiar el timer ya consumido
            self._timers.pop(pid, None)

    def _lanzar_grabaciones(self, partido: dict) -> list[RecordingJob]:
        """Crea y lanza un RecordingJob por cada canal configurado."""
        canales_ids = self._canales_para_partido(partido)
        duracion = self._duracion_total_seg()
        output_dir = Path(self.config["grabaciones"]["directorio"])
        jobs = []

        for canal_id in canales_ids:
            canal_cfg = self.canales[canal_id]
            job = RecordingJob(
                partido=partido,
                canal_id=canal_id,
                canal_config=canal_cfg,
                duracion_seg=duracion,
                output_dir=output_dir,
                config=self.config,
            )
            job.iniciar()
            jobs.append(job)

        return jobs
