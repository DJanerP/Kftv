"""
Carga de configuración: config.yaml + channels.yaml
"""
import json
import yaml
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"


def cargar_config() -> dict:
    """Carga config/config.yaml."""
    ruta = CONFIG_DIR / "config.yaml"
    if not ruta.exists():
        raise FileNotFoundError(f"Archivo de configuración no encontrado: {ruta}")
    with open(ruta, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"config.yaml malformado: {e}") from e
    if not data:
        raise ValueError(f"config.yaml está vacío o mal formado: {ruta}")
    return data


def cargar_canales() -> dict:
    """Carga config/channels.yaml. Retorna dict {canal_id: config}."""
    ruta = CONFIG_DIR / "channels.yaml"
    if not ruta.exists():
        raise FileNotFoundError(f"Archivo de canales no encontrado: {ruta}")
    with open(ruta, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"channels.yaml malformado: {e}") from e
    canales = (data or {}).get("canales", {})
    # Descartar entradas con URL vacía o None para evitar errores silenciosos más adelante
    validos = {cid: cfg for cid, cfg in canales.items() if cfg and cfg.get("url")}
    if len(validos) < len(canales):
        import logging
        logging.getLogger(__name__).warning(
            f"channels.yaml: {len(canales) - len(validos)} canal(es) sin URL ignorados."
        )
    return validos


def cargar_partidos() -> list:
    """Carga partidos.json."""
    ruta = BASE_DIR / "partidos.json"
    if not ruta.exists():
        raise FileNotFoundError(f"Archivo de partidos no encontrado: {ruta}")
    with open(ruta, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"partidos.json malformado: {e}") from e
    if not isinstance(data, list):
        raise ValueError(f"partidos.json debe ser una lista JSON: {ruta}")
    return data
