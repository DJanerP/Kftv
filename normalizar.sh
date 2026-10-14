#!/bin/bash
# normalizar.sh — repara un vídeo con la timeline ROTA de los feeds del CDN colombiano
# (Caracol/RCN en servidor-e.ejemplo): un transcoder pirata DUPLICA ~50% de los frames e INFLA los
# timestamps (60 s de fútbol repartidos en cientos de segundos de reloj). Con -c copy el fichero
# se congela y salta: no se puede ver. Ghana y otros feeds nativos (CFR limpio) no lo sufren.
#
# Técnica (verificada 05-jul con el reloj del partido quemado en pantalla):
#   mpdecimate   -> descarta los frames duplicados (deja solo el contenido real)
#   -fps_mode vfr-> CONSERVA los timestamps reales de los frames que sobreviven (NO se reestampa
#                   un reloj plano a FPS). Motivo: el feed pirata rellena ~24 fps reales con
#                   duplicados hasta fingir 29.97; si tras dedup se fuerza 30 fps constantes
#                   (setpts=N/30/TB), el tiempo se COMPRIME a ráfagas → el vídeo sale ACELERADO
#                   (+14% a +49% según el tramo) y el audio queda DESINCRONIZADO 13-17 s. Con VFR
#                   audio y vídeo quedan clavados (<0.1 s) y a velocidad real; donde la fuente se
#                   congeló de verdad (no transmitió vídeo), la imagen se mantiene fija esos
#                   segundos —honesto: enseña solo lo que llegó— en vez de acelerar todo lo demás.
#   -> MP4 VFR fluido y sincronizado. En RTX 2070 (h264_nvenc) va a ~10x tiempo real.
#
# Audio (David: "ambas pistas"): pista 0 = audio del propio vídeo resincronizado; pista 1 = SER
# (si se pasa el .mp3). Así se elige en el reproductor la que mejor sincronice.
#
# Uso:
#   normalizar.sh ENTRADA.mkv [SALIDA.mp4] [SER.mp3]
# Variables de entorno (para test / máquinas sin GPU):
#   ENC=libx264   codificador (por defecto h264_nvenc)
#   FPS=30        fps de salida
#   CQ=25         calidad (cq nvenc / se mapea a crf en x264)
set -euo pipefail

IN="${1:?uso: normalizar.sh ENTRADA.mkv [SALIDA.mp4] [SER.mp3]}"
OUT="${2:-${IN%.*}_fluido.mp4}"
SER="${3:-}"
FPS="${FPS:-30}"
ENC="${ENC:-h264_nvenc}"
CQ="${CQ:-25}"

[ -f "$IN" ] || { echo "normalizar: no existe la entrada: $IN" >&2; exit 1; }
[ -z "$SER" ] || [ -f "$SER" ] || { echo "normalizar: no existe el SER: $SER" >&2; exit 1; }

# ¿La entrada trae audio propio?
HAVE_A="$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$IN" 2>/dev/null | head -1 || true)"

ARGS=(-nostdin -y -i "$IN")
[ -n "$SER" ] && ARGS+=(-i "$SER")

# Vídeo: dedup conservando los timestamps reales (VFR). NO se fuerza un reloj plano a FPS —ver
# cabecera— para no acelerar el vídeo ni desincronizar el audio. FPS queda como env de compat
# (ya no reestampa el vídeo); la duración real la marcan los propios frames supervivientes.
ARGS+=(-filter:v "mpdecimate" -fps_mode vfr -c:v "$ENC")
if [ "$ENC" = "h264_nvenc" ]; then
  ARGS+=(-preset p5 -cq "$CQ")
else
  ARGS+=(-preset veryfast -crf "$CQ")
fi

# Mapeo: vídeo primero, luego las pistas de audio disponibles.
ARGS+=(-map 0:v:0)
AI=0
if [ -n "$HAVE_A" ]; then
  # el audio propio también viene con timestamps rotos -> resincronizar
  ARGS+=(-map 0:a:0 "-filter:a:${AI}" "aresample=async=1:first_pts=0" "-metadata:s:a:${AI}" "title=Fuente")
  AI=$((AI+1))
fi
if [ -n "$SER" ]; then
  ARGS+=(-map "1:a:0" "-metadata:s:a:${AI}" "title=SER")
fi
ARGS+=(-c:a aac -b:a 160k -movflags +faststart "$OUT")

echo "normalizar: $IN -> $OUT  (vfr enc=$ENC${SER:+ +SER})"
ffmpeg "${ARGS[@]}"
echo "normalizar: OK -> $OUT"
