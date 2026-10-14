"""
Presentación de información con Rich.
Sin spoilers: nunca muestra resultados.
"""
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.text import Text

console = Console()

FECHA_FMT = "%Y-%m-%d %H:%M"


def _parse_dt(partido: dict) -> datetime:
    return datetime.strptime(f"{partido['fecha_es']} {partido['hora_es']}", FECHA_FMT)


def _tiempo_restante(partido: dict) -> str:
    ahora = datetime.now()
    inicio = _parse_dt(partido)
    delta = inicio - ahora
    if delta.total_seconds() < 0:
        seg = abs(delta.total_seconds())
        if seg < 7200:
            return f"[yellow]en curso ({int(seg/60)} min)[/yellow]"
        return "[dim]pasado[/dim]"
    horas = int(delta.total_seconds() // 3600)
    minutos = int((delta.total_seconds() % 3600) // 60)
    if horas == 0:
        return f"[green]en {minutos} min[/green]"
    if horas < 24:
        return f"[cyan]en {horas}h {minutos}m[/cyan]"
    dias = horas // 24
    return f"[blue]en {dias}d {horas % 24}h[/blue]"


def mostrar_partidos(partidos: list[dict], titulo: str = "Partidos"):
    tabla = Table(
        title=titulo,
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
    )
    tabla.add_column("ID", style="dim", width=4, justify="right")
    tabla.add_column("Fecha", width=10)
    tabla.add_column("Hora (ES)", width=8, justify="center")
    tabla.add_column("Partido", min_width=30)
    tabla.add_column("Fase", width=16)
    tabla.add_column("Sede", width=20)
    tabla.add_column("Faltan", width=16)

    for p in partidos:
        fase_color = {
            "Fase de Grupos": "white",
            "Ronda de 32": "yellow",
            "Octavos de Final": "orange1",
            "Cuartos de Final": "red",
            "Semifinal": "magenta",
            "Final": "bold gold1",
            "Tercer y Cuarto Puesto": "dim",
        }.get(p.get("fase", ""), "white")

        grupo = f" [dim](Gr.{p['grupo']})[/dim]" if p.get("grupo") else ""

        tabla.add_row(
            str(p["id"]),
            p["fecha_es"],
            p["hora_es"],
            f"[bold]{p['local']}[/bold] vs [bold]{p['visitante']}[/bold]",
            f"[{fase_color}]{p.get('fase', '')}{grupo}[/{fase_color}]",
            f"[dim]{p.get('ciudad', '')}[/dim]",
            _tiempo_restante(p),
        )

    console.print(tabla)


def mostrar_canales(canales: dict):
    tabla = Table(
        title="Canales disponibles",
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="dim",
    )
    tabla.add_column("ID", style="cyan", width=18)
    tabla.add_column("Nombre", width=24)
    tabla.add_column("País", width=6, justify="center")
    tabla.add_column("Tipo", width=6, justify="center")
    tabla.add_column("Idioma", width=20)
    tabla.add_column("Método", width=8, justify="center")
    tabla.add_column("Notas", min_width=30)

    for cid, cfg in canales.items():
        geo = " [red]🔒geo[/red]" if cfg.get("geo_bloqueado") else ""
        tabla.add_row(
            cid,
            cfg.get("nombre", cid),
            cfg.get("pais", "??"),
            cfg.get("tipo", "tv"),
            cfg.get("idioma", ""),
            f"[dim]{cfg.get('method', '?')}[/dim]",
            f"{cfg.get('notas', '')}{geo}",
        )

    console.print(tabla)


def mostrar_jobs(jobs: list) -> None:
    if not jobs:
        console.print("[dim]No hay grabaciones activas.[/dim]")
        return

    tabla = Table(title="Grabaciones activas", box=box.SIMPLE, header_style="bold green")
    tabla.add_column("Canal", width=18)
    tabla.add_column("Partido", min_width=30)
    tabla.add_column("Archivo", min_width=40)
    tabla.add_column("Estado", width=12)

    for j in jobs:
        p = j.partido
        estado = (
            "[green]activa[/green]" if not j.completado and not j.error
            else ("[red]error[/red]" if j.error else "[dim]completada[/dim]")
        )
        tabla.add_row(
            j.canal_id,
            f"{p['local']} vs {p['visitante']}",
            j.output_path.name,
            estado,
        )

    console.print(tabla)


def mostrar_grabaciones_disponibles(directorio) -> None:
    """Lista los archivos grabados SIN spoilers (busca en subdirectorios también)."""
    from pathlib import Path
    d = Path(directorio)
    # rglob para cubrir tanto grabaciones en raíz como en subdirectorios por partido
    archivos = sorted(
        f for f in d.rglob("*")
        if f.is_file() and f.suffix.lower() in {".ts", ".mp4", ".mp3", ".mkv"}
    )

    if not archivos:
        console.print(Panel("[dim]No hay grabaciones disponibles todavía.[/dim]", title="Grabaciones"))
        return

    tabla = Table(
        title="Grabaciones disponibles",
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="dim",
    )
    tabla.add_column("#", width=4, justify="right")
    tabla.add_column("Archivo", min_width=50)
    tabla.add_column("Tamaño", width=10, justify="right")

    for i, f in enumerate(archivos, 1):
        size_mb = f.stat().st_size / (1024 * 1024)
        if size_mb > 1000:
            size_str = f"{size_mb/1024:.1f} GB"
        else:
            size_str = f"{size_mb:.0f} MB"
        tabla.add_row(str(i), f.name, size_str)

    console.print(tabla)
    console.print(
        f"[dim]Carpeta: {d}[/dim]"
    )
