#!/bin/bash
# test_grabar_multi_mkv.sh — Cancerbero para el fix de saltos (03-jul).
#
# Contrato que blinda:
#   1. El motor grabа el vídeo a .mkv (NO a .mp4) → evita la reescritura de timestamps del muxer MP4
#      que destrozaba la línea de tiempo de los directos HLS (Portugal-Croacia, 585k avisos).
#   2. Cada .mkv es reproducible y tiene duración > 0 acorde a la fuente.
#   3. NO se genera ningún .mp4 de vídeo.
#   4. RESUMEN.txt se genera y lista las fuentes.
#
# Hermético: fuentes y SER se generan localmente con ffmpeg (lavfi). Sin red. Overrides por env
# (DEST_BASE, SER_URL) — soportados por el motor justo para esto.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE="$HERE/../grabar_partido_multi.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail(){ echo "  ✗ FALLO: $*"; exit 1; }
ok(){   echo "  ✓ $*"; }

command -v ffmpeg  >/dev/null || fail "ffmpeg no disponible"
command -v ffprobe >/dev/null || fail "ffprobe no disponible"
command -v python3 >/dev/null || fail "python3 no disponible (para el HTTP local)"
[ -x "$ENGINE" ] || fail "motor no ejecutable: $ENGINE"

echo "== Preparando fuentes locales (6s, h264+aac en .ts, como un directo HLS) =="
SRC_DUR=6
for s in src1 src2; do
  ffmpeg -nostdin -y -f lavfi -i "testsrc=duration=${SRC_DUR}:size=320x240:rate=25" \
    -f lavfi -i "sine=frequency=1000:duration=${SRC_DUR}" \
    -c:v libx264 -preset ultrafast -c:a aac -shortest "$WORK/$s.ts" >/dev/null 2>&1 \
    || fail "no se pudo generar la fuente $s"
done
ffmpeg -nostdin -y -f lavfi -i "sine=frequency=440:duration=${SRC_DUR}" "$WORK/ser.mp3" >/dev/null 2>&1 \
  || fail "no se pudo generar el SER local"
ok "fuentes y SER locales listos"

echo "== Sirviendo las fuentes por HTTP local (los -reconnect del motor solo valen sobre http) =="
# El motor graba streams HTTP reales; el flag -reconnect solo existe para el protocolo http.
# Servimos las fuentes en 127.0.0.1 (sin red externa) para ejercer el motor tal cual, con sus flags.
python3 -c '
import http.server, socketserver, sys, os
os.chdir(sys.argv[1])
httpd = socketserver.TCPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
print(httpd.server_address[1], flush=True)
httpd.serve_forever()
' "$WORK" >"$WORK/port.txt" 2>/dev/null &
SRV_PID=$!
trap 'kill "$SRV_PID" 2>/dev/null; rm -rf "$WORK"' EXIT
for _ in $(seq 1 50); do [ -s "$WORK/port.txt" ] && break; sleep 0.1; done
PORT="$(cat "$WORK/port.txt" 2>/dev/null)"
[ -n "$PORT" ] || fail "no arrancó el HTTP local"
BASE="http://127.0.0.1:$PORT"
ok "HTTP local en $BASE"

echo "== Ejecutando el motor (target 1 min; las fuentes duran 6s, así que corta ahí) =="
CARPETA="test_mkv_$$"
DEST_BASE="$WORK/out" SER_URL="$BASE/ser.mp3" NORMALIZAR=0 \
  "$ENGINE" "$CARPETA" 1 \
    "fuenteA::$BASE/src1.ts" \
    "fuenteB::$BASE/src2.ts" >/dev/null 2>&1

DIR="$WORK/out/$CARPETA"
[ -d "$DIR" ] || fail "no se creó la carpeta de salida $DIR"

echo "== Aserciones =="
# 1 + 3: hay .mkv por fuente y NINGÚN .mp4 de vídeo.
for etq in fuenteA fuenteB; do
  [ -f "$DIR/$etq.mkv" ] || fail "falta $etq.mkv (¿el motor volvió a .mp4?)"
  [ -f "$DIR/$etq.mp4" ] && fail "se generó $etq.mp4 — el fix .mkv no se aplicó"
  ok "$etq.mkv presente y sin .mp4"
done
if compgen -G "$DIR/*.mp4" >/dev/null 2>&1; then
  fail "aparecieron .mp4 de vídeo en la salida: $(ls "$DIR"/*.mp4)"
fi
ok "cero .mp4 de vídeo en la salida"

# 2: cada .mkv es reproducible y con duración plausible (>= 4s, cerca de los 6s de la fuente).
for etq in fuenteA fuenteB; do
  d=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$DIR/$etq.mkv" 2>/dev/null | cut -d. -f1)
  d=${d:-0}
  [ "$d" -ge 4 ] || fail "$etq.mkv duración sospechosa: ${d}s (esperado ~${SRC_DUR}s)"
  # que tenga stream de vídeo legible
  vc=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 "$DIR/$etq.mkv" 2>/dev/null)
  [ -n "$vc" ] || fail "$etq.mkv sin stream de vídeo legible"
  ok "$etq.mkv reproducible (${d}s, códec $vc)"
done

# 4: RESUMEN.txt generado y lista las fuentes.
[ -f "$DIR/RESUMEN.txt" ] || fail "no se generó RESUMEN.txt"
grep -q "fuenteA" "$DIR/RESUMEN.txt" && grep -q "fuenteB" "$DIR/RESUMEN.txt" \
  || fail "RESUMEN.txt no lista las fuentes"
ok "RESUMEN.txt generado y con las fuentes"

echo "== TODO OK: el motor graba a .mkv, reproducible, sin .mp4 =="
