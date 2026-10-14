#!/bin/bash
# grabar_partido_multi.sh — graba un partido desde VARIAS fuentes EN PARALELO + audio radio.
#
# Por qué paralelo y no cascada (lección 29→30 jun):
#   La cascada elegía la 1ª fuente "con vídeo" aunque emitiera OTRA cosa (Caracol daba Brasil-Japón
#   en vez de PB-Marruecos) y no hacía failover si la señal caía a mitad (Alemania cortó en min 76).
#   Grabando todas las candidatas a la vez, basta con que UNA tenga el partido íntegro. Disco sobra.
#
# Por qué .mkv y no .mp4 (lección 03-jul, Portugal-Croacia):
#   El directo HLS (.ts) trae timestamps no monótonos. El muxer de MP4 los "corrige" a la fuerza
#   (585.000 avisos "Non-monotonous DTS" en Caracol) y destroza la línea de tiempo → el fichero da
#   tantos saltos que no se puede ver. MKV tolera esos timestamps sin reescribirlos: mismo -c copy,
#   cero CPU, cero pérdida de calidad, y el vídeo se ve fluido. RCN (mismo -c copy a mp4) casi no lo
#   sufrió por suerte; el .mkv lo elimina de raíz para todas las fuentes.
#
# Uso:
#   grabar_partido_multi.sh "CARPETA" DUR_MIN "ETIQUETA1::URL1" "ETIQUETA2::URL2" ...
#
# Genera dentro de /mnt/khauron/mundial2026/CARPETA/:
#   <etiqueta>.mkv        un vídeo por fuente (MKV: no destroza timestamps de directos HLS)
#   <etiqueta>.log        log de ffmpeg de cada fuente
#   _verif_<etiqueta>.jpg frame de verificación capturado al arrancar (para revisar el CONTENIDO sin abrir el mp4)
#   ser.mp3 + ser.log     audio de Cadena SER (respaldo en español si el vídeo no es español/LATAM)
#   RESUMEN.txt           veredicto honesto: duración real de cada salida vs la esperada
set -u

DEST_BASE="${DEST_BASE:-/mnt/khauron/mundial2026}"
SER_URL="${SER_URL:-https://playerservices.streamtheworld.com/api/livestream-redirect/CADENASER.mp3}"  # ver sources.yaml: audio.ser (override por env para tests)

CARPETA="$1"; DUR_MIN="$2"; shift 2
DUR_SEG=$((DUR_MIN*60))
DIR="$DEST_BASE/$CARPETA"
mkdir -p "$DIR"

# Auto-resume tras corte/reinicio: si ya hay vídeo de un intento previo, no lo pisamos;
# esta tanda va a una subcarpeta resume_HHMMSS para luego poder unir las partes.
if compgen -G "$DIR/*.mkv" >/dev/null 2>&1; then
  DIR="$DIR/resume_$(date +%H%M%S)"
  mkdir -p "$DIR"
fi
RES="$DIR/RESUMEN.txt"

# Flags de reconexión HTTP: sobreviven a microcaídas del CDN (no a un 404 total — para eso está el paralelo).
RECONNECT=(-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 30 -rw_timeout 15000000)

log(){ echo "[$(date '+%F %T')] $*" | tee -a "$DIR/_lanzamiento.log"; }
log "=== $CARPETA — ${DUR_MIN} min — $# fuentes de vídeo + SER ==="

PIDS=()
LABELS=()
OUTS=()

for TOKEN in "$@"; do
  ETIQ="${TOKEN%%::*}"
  URL="${TOKEN#*::}"
  OUT="$DIR/${ETIQ}.mkv"
  FLOG="$DIR/${ETIQ}.log"
  VERIF="$DIR/_verif_${ETIQ}.jpg"
  log "Fuente '$ETIQ' -> $URL"

  # Frame de verificación temprano (no bloquea la grabación; sirve para revisar contenido en seguida).
  ( timeout 30 ffmpeg -nostdin -y "${RECONNECT[@]}" -i "$URL" -frames:v 1 -q:v 3 "$VERIF" >/dev/null 2>&1 ) &

  # Grabación de la fuente. Contenedor MKV: -c copy (sin recodificar) pero sin la reescritura de
  # timestamps que hace el muxer MP4 y que destroza la línea de tiempo de los directos HLS.
  ( ffmpeg -nostdin -y "${RECONNECT[@]}" -i "$URL" -t "$DUR_SEG" -c copy "$OUT" >"$FLOG" 2>&1 ) &
  PIDS+=($!); LABELS+=("$ETIQ"); OUTS+=("$OUT")
done

# Audio Cadena SER en paralelo.
SER_OUT="$DIR/ser.mp3"
( ffmpeg -nostdin -y "${RECONNECT[@]}" -i "$SER_URL" -t "$DUR_SEG" -c copy "$SER_OUT" >"$DIR/ser.log" 2>&1 ) &
SER_PID=$!
log "Audio SER -> ser.mp3"

log "Grabando ${DUR_MIN} min en paralelo... (PIDs vídeo: ${PIDS[*]} | SER: $SER_PID)"
wait "${PIDS[@]}" "$SER_PID" 2>/dev/null

# ── Veredicto honesto: duración real, no "OK" por existir ──
dur(){ ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null | cut -d. -f1; }
OBJETIVO=$DUR_SEG
{
  echo "RESUMEN — $CARPETA"
  echo "Generado: $(date '+%F %T')   Objetivo: ${DUR_MIN} min (${DUR_SEG}s)"
  echo "Revisa los _verif_*.jpg para confirmar QUÉ emitía cada fuente."
  echo "------------------------------------------------------------"
  for i in "${!OUTS[@]}"; do
    d=$(dur "${OUTS[$i]}"); d=${d:-0}
    pct=$(( d*100 / (OBJETIVO>0?OBJETIVO:1) ))
    estado="PARCIAL"; [ "$pct" -ge 90 ] && estado="COMPLETA"
    printf "  %-18s %4dm / %dm  (%d%%)  %s\n" "${LABELS[$i]}" "$((d/60))" "$((OBJETIVO/60))" "$pct" "$estado"
  done
  sd=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$SER_OUT" 2>/dev/null | cut -d. -f1); sd=${sd:-0}
  printf "  %-18s %4dm  (audio SER)\n" "ser.mp3" "$((sd/60))"
} | tee "$RES" | tee -a "$DIR/_lanzamiento.log"

# ── Normalización automática (post-grabación) de fuentes con timeline rota ──
# Caracol/RCN (CDN pirata) llegan con ~50% de frames duplicados y timestamps inflados: el .mkv crudo
# se ve a saltos. normalizar_carpeta.sh detecta las fuentes rotas y genera <fuente>_fluido.mp4 fluido
# (mpdecimate + reloj limpio, GPU). Se lanza DETACHADO para no bloquear el cron/supervisor; el .mkv
# crudo siempre se conserva. Desactivable con NORMALIZAR=0 (tests, máquinas sin GPU).
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "${NORMALIZAR:-1}" = "1" ] && [ -x "$SELF_DIR/normalizar_carpeta.sh" ]; then
  log "Lanzando normalización automática en segundo plano → $DIR/_normalizar.log"
  setsid nohup "$SELF_DIR/normalizar_carpeta.sh" "$DIR" >/dev/null 2>&1 &
fi

log "=== FIN $CARPETA ==="
