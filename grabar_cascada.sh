#!/bin/bash
# grabar_cascada.sh — graba un partido probando una CASCADA de fuentes hasta dar con una viva.
# La que no esté disponible se salta; si se agotan todas → "NO HA SIDO POSIBLE".
# Uso: grabar_cascada.sh "Nombre Partido" DURACION_MIN URL1 URL2 URL3 ...
set -u
NOMBRE="$1"; DUR_MIN="$2"; shift 2
DEST="/mnt/khauron/mundial2026"
FRAMES="$DEST/frames"; mkdir -p "$FRAMES"
TS=$(date +%Y%m%d_%H%M)
SLUG=$(echo "$NOMBRE" | tr ' /:' '___')
LOG="$DEST/cascada_${SLUG}_${TS}.log"

log(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
log "=== Cascada para '$NOMBRE' (${DUR_MIN} min) ==="

i=0
for URL in "$@"; do
  i=$((i+1))
  log "Fuente $i: $URL"
  # ¿stream vivo con pista de vídeo? (ffprobe corto)
  if timeout 20 ffprobe -v error -select_streams v:0 -show_entries stream=codec_type -of csv=p=0 "$URL" 2>/dev/null | grep -q video; then
    FR="$FRAMES/${SLUG}_src${i}_${TS}.jpg"
    timeout 25 ffmpeg -y -i "$URL" -frames:v 1 -vf scale=480:-1 "$FR" 2>/dev/null
    OUT="${DEST}/${SLUG}_${TS}.mp4"
    log "  -> VIVA. Frame verificación: $FR"
    log "  -> Grabando ${DUR_MIN} min en $OUT"
    ffmpeg -y -i "$URL" -t $((DUR_MIN*60)) -c copy "$OUT" >>"$LOG" 2>&1
    SZ=$(du -h "$OUT" 2>/dev/null | cut -f1)
    log "  -> GRABACIÓN OK desde fuente $i ($SZ): $OUT"
    echo "FUENTE_USADA=$i URL=$URL ARCHIVO=$OUT FRAME=$FR"
    exit 0
  else
    log "  -> no válida (sin vídeo / no alcanzable). Siguiente."
  fi
done
log "NO HA SIDO POSIBLE grabar '$NOMBRE' — todas las fuentes agotadas."
exit 1
