#!/usr/bin/env python3
"""
Mundia2026 — Grabador anti-spoilers del Mundial

Uso:
  python main.py daemon       → arranca el scheduler (graba todos los partidos futuros)
  python main.py hoy          → muestra partidos de hoy
  python main.py siguiente    → próximos 5 partidos
  python main.py siguiente 10 → próximos 10 partidos
  python main.py grabar ID    → graba un partido ahora mismo (por ID)
  python main.py test CANAL   → graba 60 segundos de prueba de un canal
  python main.py canales      → lista los canales disponibles
  python main.py archivos     → lista las grabaciones disponibles
"""
import sys
import time
import signal
import logging
from pathlib import Path
from datetime import datetime

import click
from rich.panel import Panel

# Añadir el directorio raíz al path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import cargar_config, cargar_canales
from src.scheduler import MatchScheduler
from src.display import (
    console,
    mostrar_partidos,
    mostrar_canales,
    mostrar_jobs,
    mostrar_grabaciones_disponibles,
)


def setup_logging(config: dict):
    log_dir = Path(config["logs"]["directorio"])
    log_dir.mkdir(parents=True, exist_ok=True)
    nivel = getattr(logging, config["logs"]["nivel"], logging.INFO)
    logging.basicConfig(
        level=nivel,
        format="%(asctime)s %(name)s %(levelname)s — %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "mundia2026.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


@click.group()
def cli():
    """Mundia2026 — Grabador anti-spoilers del Mundial 2026."""
    pass


# ──────────────────────────────────────────────────────────────────────────────
# daemon
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
def daemon():
    """Arranca el scheduler. Graba todos los partidos futuros automáticamente."""
    config = cargar_config()
    setup_logging(config)
    logger = logging.getLogger(__name__)

    scheduler = MatchScheduler()
    n = scheduler.programar_todos()

    if n == 0:
        console.print("[yellow]No hay partidos futuros para programar.[/yellow]")
        return

    proximos = scheduler.partidos_proximos(3)
    console.print(
        Panel(
            f"[bold green]Daemon activo[/bold green] — {n} partido(s) programados\n"
            f"[dim]Ctrl+C para salir[/dim]",
            title="Mundia2026",
            border_style="green",
        )
    )
    mostrar_partidos(proximos, "Próximos partidos programados")

    # Registrar SIGTERM para salida limpia
    def _salir(sig, frame):
        console.print("\n[yellow]Deteniendo scheduler...[/yellow]")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _salir)

    # Mantener vivo el proceso. Cada 60 s muestra los jobs activos si los hay.
    try:
        while True:
            time.sleep(60)
            jobs = scheduler.jobs_activos()
            if jobs:
                mostrar_jobs(jobs)
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler detenido.[/yellow]")


# ──────────────────────────────────────────────────────────────────────────────
# hoy
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
def hoy():
    """Muestra los partidos de hoy."""
    scheduler = MatchScheduler()
    partidos = scheduler.partidos_hoy()
    if not partidos:
        hoy_str = datetime.now().strftime("%d/%m/%Y")
        console.print(f"[dim]No hay partidos programados para hoy ({hoy_str}).[/dim]")
    else:
        mostrar_partidos(partidos, f"Partidos de hoy — {datetime.now().strftime('%d/%m/%Y')}")


# ──────────────────────────────────────────────────────────────────────────────
# siguiente
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("n", default=5, type=int)
def siguiente(n: int):
    """Muestra los próximos N partidos (default: 5)."""
    scheduler = MatchScheduler()
    partidos = scheduler.partidos_proximos(n)
    if not partidos:
        console.print("[dim]No hay partidos futuros.[/dim]")
    else:
        mostrar_partidos(partidos, f"Próximos {n} partidos")


# ──────────────────────────────────────────────────────────────────────────────
# grabar
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("partido_id", type=int)
@click.option("--canal", default=None, help="ID de canal específico (override config)")
@click.option("--esperar", is_flag=True, help="Esperar a que termine la grabación")
def grabar(partido_id: int, canal: str | None, esperar: bool):
    """Lanza la grabación de un partido por ID de forma inmediata."""
    config = cargar_config()
    setup_logging(config)

    scheduler = MatchScheduler()

    # Override de canal si se especifica
    if canal:
        if canal not in scheduler.canales:
            console.print(f"[red]Canal '{canal}' no encontrado.[/red]")
            console.print("[dim]Usa 'python main.py canales' para ver los disponibles.[/dim]")
            return
        # Inyectar el canal en el partido temporalmente
        partido = scheduler._buscar_partido(partido_id)
        if partido:
            partido = dict(partido)
            partido["canales"] = [canal]
            jobs = scheduler._lanzar_grabaciones(partido)
        else:
            console.print(f"[red]Partido #{partido_id} no encontrado.[/red]")
            return
    else:
        try:
            jobs = scheduler.grabar_ahora(partido_id)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return

    for j in jobs:
        console.print(f"[green]Grabando:[/green] {j.output_path.name}")

    if esperar:
        console.print("[dim]Esperando a que terminen las grabaciones...[/dim]")
        for j in jobs:
            j.esperar()
        console.print("[green]Grabaciones completadas.[/green]")
        mostrar_jobs(jobs)


# ──────────────────────────────────────────────────────────────────────────────
# test
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("canal_id")
@click.option("--segundos", default=60, help="Duración del test en segundos (default: 60)")
def test(canal_id: str, segundos: int):
    """Graba N segundos de prueba de un canal para verificar que funciona."""
    from src.recorder import RecordingJob

    config = cargar_config()
    setup_logging(config)
    canales = cargar_canales()

    if canal_id not in canales:
        console.print(f"[red]Canal '{canal_id}' no encontrado.[/red]")
        console.print("[dim]Usa 'python main.py canales' para ver los disponibles.[/dim]")
        return

    canal_cfg = canales[canal_id]
    output_dir = Path(config["grabaciones"]["directorio"]) / "tests"

    partido_test = {
        "id": 0,
        "fecha_es": datetime.now().strftime("%Y-%m-%d"),
        "hora_es": datetime.now().strftime("%H:%M"),
        "local": "TEST",
        "visitante": canal_id,
        "fase": "test",
        "grupo": None,
    }

    console.print(f"[cyan]Grabando {segundos}s de prueba del canal [bold]{canal_id}[/bold]...[/cyan]")

    job = RecordingJob(
        partido=partido_test,
        canal_id=canal_id,
        canal_config=canal_cfg,
        duracion_seg=segundos,
        output_dir=output_dir,
        config=config,
    )
    job.iniciar()
    job.esperar(timeout=segundos + 30)

    if job.error:
        console.print(f"[red]Error: {job.error}[/red]")
    elif job.completado:
        size_mb = job.output_path.stat().st_size / (1024 * 1024) if job.output_path.exists() else 0
        console.print(
            f"[green]✓ Test completado:[/green] {job.output_path.name} "
            f"({size_mb:.1f} MB)"
        )
    else:
        console.print("[yellow]La grabación no completó en el tiempo esperado.[/yellow]")


# ──────────────────────────────────────────────────────────────────────────────
# canales
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
def canales():
    """Lista todos los canales disponibles con sus detalles."""
    canales_cfg = cargar_canales()
    mostrar_canales(canales_cfg)


# ──────────────────────────────────────────────────────────────────────────────
# archivos
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
def archivos():
    """Lista las grabaciones disponibles (sin spoilers)."""
    config = cargar_config()
    mostrar_grabaciones_disponibles(config["grabaciones"]["directorio"])


if __name__ == "__main__":
    cli()
