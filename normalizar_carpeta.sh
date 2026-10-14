#!/bin/bash
# normalizar_carpeta.sh — normaliza cada fuente .mkv de una carpeta de partido a un MP4 fluido.
#
# Por qué normaliza TODAS y no solo "las rotas" (lección 04-jul, Canadá-Marruecos):
#   La duplicación del CDN pirata (Caracol/RCN de servidor-e.ejemplo) es A RÁFAGAS: el mismo fichero da
#   0% en unos tramos y 50% en otros, y hasta el framerate varía (27–54 fps). Cualquier detector por
#   muestreo de % de duplicados es una lotería (falla en ambos sentidos). Normalizar siempre es
#   robusto: sobre una fuente rota la arregla; sobre una ya limpia solo la re-codifica (a 30 fps, sin
#   daño real — el crudo .mkv se conserva siempre por si se quiere la calidad original). Se acaban
#   los falsos negativos que dejaban un partido sin poder verse.
#
# Conserva el .mkv crudo. No fatal: un fallo en una fuente no aborta las demás. Idempotente: si ya
# existe <fuente>_fluido.mp4 no la repite.
#
# Uso:  normalizar_carpeta.sh CARPETA
# Env:  ENC / FPS / CQ  se pasan a normalizar.sh (ENC=libx264 para máquinas sin GPU)
set -u

DIR="${1:?uso: normalizar_carpeta.sh CARPETA}"
[ -d "$DIR" ] || { echo "normcarpeta: no existe la carpeta: $DIR" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
NORM="$HERE/normalizar.sh"
[ -x "$NORM" ] || { echo "normcarpeta: falta normalizar.sh en $HERE" >&2; exit 1; }

SER=""; [ -f "$DIR/ser.mp3" ] && SER="$DIR/ser.mp3"

log(){ echo "[$(date '+%F %T')] normcarpeta: $*" | tee -a "$DIR/_normalizar.log"; }

log "=== $(basename "$DIR") — normalizando todas las fuentes ==="
shopt -s nullglob
hubo=0
for MKV in "$DIR"/*.mkv; do
  case "$MKV" in *_fluido.mkv) continue;; esac
  hubo=1
  base="$(basename "${MKV%.mkv}")"
  OUT="${MKV%.mkv}_fluido.mp4"
  if [ -s "$OUT" ]; then log "$base: ya existe ${base}_fluido.mp4 → salto"; continue; fi
  log "$base: normalizando → ${base}_fluido.mp4"
  if "$NORM" "$MKV" "$OUT" "$SER" >>"$DIR/_normalizar.log" 2>&1; then
    log "$base: OK → ${base}_fluido.mp4"
  else
    log "$base: FALLO al normalizar (se conserva el .mkv crudo)"
  fi
done
[ "$hubo" = 1 ] || log "no había .mkv que procesar en $DIR"
log "=== fin ==="
