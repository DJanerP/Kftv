"""
Motor de grabación.

Estrategia por método:
  rtve_ztnr → Obtiene URL firmada del servidor ZTNR de RTVE (token ~7 días)
              luego ffmpeg graba con duración exacta.
  yt-dlp    → yt-dlp --get-url para obtener la URL del stream HLS/m3u8
              → ffmpeg -i <url> -t <duración> -c copy output.ts
  ffmpeg    → URL directa conocida, ffmpeg graba directamente con duración
"""
import subprocess
import threading
import logging
import re
import sys
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


def _obtener_url_rtve_ztnr(id_asset: str) -> str:
    """
    Obtiene la URL firmada del stream de RTVE vía el servidor ZTNR.
    Usa el módulo yt-dlp para desencriptar el payload del PNG.
    Genera un token HMAC válido ~7 días.
    """
    # Añadir el venv al path si yt_dlp no está disponible en el entorno actual
    venv_lib = Path(__file__).parent.parent / "venv" / "lib"
    for p in venv_lib.glob("python3*/site-packages"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    import yt_dlp
    import yt_dlp.extractor.rtve as rtve_mod

    ydl = yt_dlp.YoutubeDL({"quiet": True})
    ie = rtve_mod.RTVEBaseIE(ydl)

    ztnr_url = (
        f"http://www.rtve.es/ztnr/movil/thumbnail/rtveplayw/videos/{id_asset}.png"
    )
    png = ie._download_webpage(
        ztnr_url, id_asset, "Obteniendo URL firmada RTVE", query={"q": "v2"}, fatal=False
    )
    if not png:
        raise RuntimeError(f"ZTNR no devolvió datos para idAsset={id_asset}")

    urls = list(ie._decrypt_url(png))
    if not urls:
        raise RuntimeError(f"No se pudo desencriptar la URL RTVE para idAsset={id_asset}")

    stream_url = urls[0][1]
    logger.debug(f"URL RTVE obtenida: {stream_url[:80]}...")
    return stream_url


def _sanitizar(texto: str) -> str:
    """Convierte un texto a nombre de archivo seguro."""
    texto = texto.strip()
    texto = re.sub(r"[^\w\s\-áéíóúüñÁÉÍÓÚÜÑ]", "", texto)
    texto = re.sub(r"\s+", "_", texto)
    return texto[:40]


def construir_nombre(partido: dict, canal_id: str, config: dict) -> str:
    """Genera el nombre del archivo de salida."""
    formato = config["grabaciones"]["formato"]
    ext_video = config["grabacion"]["extension_video"]
    ext_radio = config["grabacion"]["extension_radio"]

    nombre = formato.format(
        fecha=partido.get("fecha_es", "").replace("-", ""),
        hora=partido.get("hora_es", "").replace(":", ""),
        local=_sanitizar(partido.get("local", "local")),
        visitante=_sanitizar(partido.get("visitante", "visitante")),
        canal=canal_id,
        fase=_sanitizar(partido.get("fase", "")),
        grupo=partido.get("grupo") or "",
    )
    return nombre


class RecordingJob:
    """
    Representa la grabación de un partido en un canal concreto.
    Corre en su propio thread. No bloquea el scheduler.
    """

    def __init__(
        self,
        partido: dict,
        canal_id: str,
        canal_config: dict,
        duracion_seg: int,
        output_dir: Path,
        config: dict,
    ):
        self.partido = partido
        self.canal_id = canal_id
        self.canal_config = canal_config
        self.duracion_seg = duracion_seg
        self.output_dir = Path(output_dir)
        self.config = config

        self._proc_principal = None
        self._proc_ffmpeg = None
        self._thread = None
        self.iniciado = False
        self.completado = False
        self.error: str | None = None

        nombre = construir_nombre(partido, canal_id, config)
        tipo = canal_config.get("tipo", "tv")
        ext = (
            canal_config.get("extension", config["grabacion"]["extension_radio"])
            if tipo == "radio"
            else config["grabacion"]["extension_video"]
        )
        self.output_path = self.output_dir / f"{nombre}.{ext}"

    # ──────────────────────────────────────────────────────────────
    # API pública
    # ──────────────────────────────────────────────────────────────

    def iniciar(self):
        """Lanza la grabación en background. No bloquea."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"rec-{self.canal_id}")
        self._thread.start()
        self.iniciado = True
        logger.info(
            f"[{self.canal_id}] Grabación iniciada → {self.output_path.name} "
            f"({self.duracion_seg // 60} min)"
        )

    def detener(self):
        """Detiene la grabación forzosamente. SIGTERM primero, luego SIGKILL si no responde."""
        import signal
        import time as _time

        for proc in (self._proc_principal, self._proc_ffmpeg):
            if proc is None:
                continue
            try:
                proc.terminate()
            except OSError:
                pass

        # Dar 5 segundos para salida limpia; luego SIGKILL
        deadline = _time.monotonic() + 5
        for proc in (self._proc_principal, self._proc_ffmpeg):
            if proc is None:
                continue
            remaining = deadline - _time.monotonic()
            try:
                proc.wait(timeout=max(0, remaining))
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass

        logger.info(f"[{self.canal_id}] Grabación detenida manualmente.")

    def esperar(self, timeout: int | None = None):
        """Bloquea hasta que termine la grabación (útil para tests)."""
        if self._thread:
            self._thread.join(timeout=timeout)

    def __repr__(self):
        estado = "completada" if self.completado else ("error" if self.error else "activa")
        return f"<RecordingJob {self.canal_id} partido={self.partido['id']} {estado}>"

    # ──────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────

    def _run(self):
        try:
            method = self.canal_config.get("method", "yt-dlp")
            if method == "yt-dlp":
                self._grabar_ytdlp()
            elif method == "ffmpeg":
                self._grabar_ffmpeg_directo()
            elif method == "rtve_ztnr":
                self._grabar_rtve_ztnr()
            else:
                raise ValueError(f"Método desconocido: {method}")
            self.completado = True
            logger.info(f"[{self.canal_id}] ✓ Grabación completada: {self.output_path.name}")
        except Exception as e:
            self.error = str(e)
            logger.error(f"[{self.canal_id}] ✗ Error: {e}")

    def _grabar_ytdlp(self):
        """
        Paso 1: yt-dlp --get-url para obtener la URL del stream HLS.
        Paso 2: ffmpeg graba con duración exacta.

        Si yt-dlp no puede extraer la URL (canal no soportado),
        intenta pasar la URL original directamente a ffmpeg.
        """
        url = self.canal_config["url"]
        calidad = self.config["grabacion"]["calidad"]

        logger.debug(f"[{self.canal_id}] Extrayendo URL del stream con yt-dlp...")

        # Mapeo de calidad a formato de yt-dlp
        formato_ytdlp = {
            "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        }.get(calidad, "best")

        resultado = subprocess.run(
            [
                "yt-dlp",
                "--get-url",
                "--format", formato_ytdlp,
                "--no-playlist",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if resultado.returncode != 0 or not resultado.stdout.strip():
            logger.warning(
                f"[{self.canal_id}] yt-dlp no pudo extraer URL "
                f"(código {resultado.returncode}). Intentando ffmpeg directo..."
            )
            # Fallback: intentar grabar la URL original con ffmpeg
            self._grabar_ffmpeg_url(url)
            return

        stream_urls = resultado.stdout.strip().split("\n")
        stream_url = stream_urls[0]

        # Si hay dos URLs (video+audio separados), ffmpeg las combina
        if len(stream_urls) >= 2:
            self._grabar_ffmpeg_dual(stream_urls[0], stream_urls[1])
        else:
            self._grabar_ffmpeg_url(stream_url)

    def _log_path(self) -> Path:
        """Ruta del fichero de log de ffmpeg para este job."""
        return self.output_path.with_suffix(".ffmpeg.log")

    def _grabar_ffmpeg_url(self, url: str):
        """Graba una URL directa con ffmpeg con duración limitada."""
        cmd = [
            "ffmpeg", "-y",
            "-user_agent", "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "-i", url,
            "-t", str(self.duracion_seg),
            "-c", "copy",
            str(self.output_path),
        ]
        logger.debug(f"[{self.canal_id}] ffmpeg: {' '.join(cmd[:6])}...")
        # Escribir stderr de ffmpeg a fichero en lugar de PIPE para evitar
        # acumulación en RAM durante grabaciones largas (2h+)
        with open(self._log_path(), "w") as lf:
            self._proc_ffmpeg = subprocess.Popen(cmd, stdout=lf, stderr=lf)
            self._proc_ffmpeg.communicate()
        if self._proc_ffmpeg.returncode not in (0, 255):  # 255 = detenido por señal (normal)
            raise RuntimeError(
                f"ffmpeg terminó con código {self._proc_ffmpeg.returncode} "
                f"— ver {self._log_path().name}"
            )

    def _grabar_ffmpeg_dual(self, video_url: str, audio_url: str):
        """Graba video y audio de URLs separadas (formato DASH) con ffmpeg."""
        cmd = [
            "ffmpeg", "-y",
            "-user_agent", "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "-i", video_url,
            "-i", audio_url,
            "-t", str(self.duracion_seg),
            "-c", "copy",
            str(self.output_path),
        ]
        logger.debug(f"[{self.canal_id}] ffmpeg dual stream...")
        with open(self._log_path(), "w") as lf:
            self._proc_ffmpeg = subprocess.Popen(cmd, stdout=lf, stderr=lf)
            self._proc_ffmpeg.communicate()
        if self._proc_ffmpeg.returncode not in (0, 255):
            raise RuntimeError(
                f"ffmpeg dual terminó con código {self._proc_ffmpeg.returncode} "
                f"— ver {self._log_path().name}"
            )

    def _grabar_ffmpeg_directo(self):
        """Para canales de radio o streams con URL directa conocida."""
        url = self.canal_config.get("url")
        if not url:
            raise ValueError(f"[{self.canal_id}] Canal con method=ffmpeg sin campo 'url' en config.")
        cmd = [
            "ffmpeg", "-y",
            "-user_agent", "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "-i", url,
            "-t", str(self.duracion_seg),
            "-c", "copy",
            str(self.output_path),
        ]
        logger.debug(f"[{self.canal_id}] ffmpeg directo: {url[:60]}...")
        with open(self._log_path(), "w") as lf:
            self._proc_ffmpeg = subprocess.Popen(cmd, stdout=lf, stderr=lf)
            self._proc_ffmpeg.communicate()
        if self._proc_ffmpeg.returncode not in (0, 255):
            raise RuntimeError(
                f"ffmpeg directo terminó con código {self._proc_ffmpeg.returncode} "
                f"— ver {self._log_path().name}"
            )

    def _grabar_rtve_ztnr(self):
        """Para canales RTVE: obtiene URL firmada via ZTNR, luego graba con ffmpeg."""
        id_asset = self.canal_config.get("id_asset")
        if not id_asset:
            raise ValueError(f"[{self.canal_id}] Canal rtve_ztnr sin id_asset en config.")
        logger.debug(f"[{self.canal_id}] Obteniendo URL firmada RTVE (idAsset={id_asset})...")
        stream_url = _obtener_url_rtve_ztnr(id_asset)
        self._grabar_ffmpeg_url(stream_url)


def grabar_ahora(
    partido: dict,
    canal_id: str,
    canal_config: dict,
    duracion_seg: int,
    output_dir: Path,
    config: dict,
) -> RecordingJob:
    """Helper: crea y lanza un RecordingJob inmediatamente."""
    job = RecordingJob(partido, canal_id, canal_config, duracion_seg, output_dir, config)
    job.iniciar()
    return job
