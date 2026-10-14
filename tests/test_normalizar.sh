#!/bin/bash
# test_normalizar.sh — Cancerbero para normalizar.sh.
# Hermético: NO usa GPU (ENC=libx264) ni red. Fabrica una entrada con la MISMA patología que
# Caracol (frames duplicados: contenido a 5 fps servido a 30 fps => 25/30 frames son duplicados)
# y verifica el contrato de "se puede ver":
#   1) la salida existe y es un MP4 reproducible
#   2) el vídeo tiene PTS ESTRICTAMENTE MONÓTONOS (sin timeline rota). Ya NO se exige CFR: el
#      normalizador pasó a VFR (conserva timestamps reales) para no acelerar ni desincronizar.
#   2b) audio y vídeo SINCRONIZADOS (misma duración) — el guard que FALTABA y dejó pasar el bug
#       del 05-jul: el reloj plano forzado comprimía el vídeo y el audio quedaba 13-17 s más largo.
#   2c) velocidad real: duración de salida ≈ duración de entrada (vídeo no acelerado/comprimido)
#   3) mpdecimate SÍ redujo frames (la entrada tenía duplicados; se comprueba en crudo)
#   4) hay EXACTAMENTE 2 pistas de audio cuando se pasa SER (Fuente + SER)
#   5) sin SER, hay 1 pista de audio
set -euo pipefail
export LC_ALL=C   # pts_time/duration vienen con punto decimal; un locale con coma los truncaría (falso positivo)

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../normalizar.sh"
[ -x "$SCRIPT" ] || chmod +x "$SCRIPT"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FPS=30

pass(){ echo "  ✓ $*"; }
fail(){ echo "  ✗ $*" >&2; exit 1; }

echo "== test_normalizar =="

# --- Fixture: entrada rota (contenido 5 fps mostrado a 30 fps => duplicados) + audio propio ---
BROKEN="$WORK/broken.mkv"
ffmpeg -nostdin -y \
  -f lavfi -i "testsrc=size=320x240:rate=5" \
  -f lavfi -i "sine=frequency=1000:sample_rate=44100" \
  -vf "fps=${FPS}" -t 4 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$BROKEN" >/dev/null 2>&1
[ -s "$BROKEN" ] || fail "no se generó la entrada rota"

# frames crudos de la entrada vs frames únicos (debe haber duplicados que quitar)
IN_TOTAL=$(ffprobe -v error -select_streams v -count_packets -show_entries stream=nb_read_packets -of csv=p=0 "$BROKEN")
IN_UNIQ=$(ffmpeg -nostdin -i "$BROKEN" -an -vf mpdecimate -f null - 2>&1 | grep -Eio 'frame= *[0-9]+' | tail -1 | grep -Eo '[0-9]+')
[ "$IN_UNIQ" -lt "$IN_TOTAL" ] || fail "la entrada de prueba no tiene duplicados (total=$IN_TOTAL uniq=$IN_UNIQ)"
pass "entrada rota con duplicados: total=$IN_TOTAL únicos=$IN_UNIQ"

SER="$WORK/ser.mp3"
ffmpeg -nostdin -y -f lavfi -i "sine=frequency=440:sample_rate=44100" -t 4 -c:a libmp3lame "$SER" >/dev/null 2>&1
[ -s "$SER" ] || fail "no se generó el SER de prueba"

# --- Ejecutar normalizar con SER (2 audios) ---
OUT="$WORK/out.mp4"
ENC=libx264 FPS=$FPS CQ=28 "$SCRIPT" "$BROKEN" "$OUT" "$SER" >/dev/null 2>&1 || fail "normalizar.sh salió con error"
[ -s "$OUT" ] || fail "no se generó la salida"
pass "salida generada"

# 1) reproducible / demuxeable
ffprobe -v error "$OUT" >/dev/null 2>&1 || fail "la salida no es un fichero válido"
pass "la salida es un MP4 válido"

# 2) vídeo con PTS ESTRICTAMENTE MONÓTONOS (sin saltos atrás = sin timeline rota).
#    Ya NO se exige CFR ni "sin huecos": el VFR conserva los timestamps reales, así que puede haber
#    huecos legítimos (imagen mantenida donde la fuente se congeló de verdad). Lo injugable —y lo
#    que este test blinda— es un PTS que RETROCEDA.
ffprobe -v error -select_streams v -show_entries frame=pts_time -of csv=p=0 "$OUT" 2>/dev/null \
| awk -F',' 'NF{t=$1+0; if(p!=""){if(t<=p){print "NOMONO"; exit}} p=t} END{print "OK"}' \
| grep -q '^OK$' || fail "el vídeo de salida tiene PTS no monótonos (timeline rota)"
pass "PTS estrictamente monótonos (sin timeline rota)"

# 2b) SINCRONÍA A/V — el guard que faltaba. El vídeo no puede quedar mucho más corto que su audio.
VDUR=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration -of csv=p=0 "$OUT")
ADUR=$(ffprobe -v error -select_streams a:0 -show_entries stream=duration -of csv=p=0 "$OUT")
SKEW=$(awk "BEGIN{d=$ADUR-$VDUR; printf \"%.3f\", (d<0?-d:d)}")
awk "BEGIN{exit !($SKEW < 0.5)}" || fail "desincronía A/V: vídeo=${VDUR}s audio=${ADUR}s (desfase=${SKEW}s)"
pass "A/V sincronizados (vídeo=${VDUR}s audio=${ADUR}s desfase=${SKEW}s < 0.5s)"

# 2c) velocidad real: la salida NO puede estar comprimida respecto a la entrada (eso = acelerada).
INDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$BROKEN")
DDUR=$(awk "BEGIN{d=$INDUR-$VDUR; printf \"%.3f\", (d<0?-d:d)}")
awk "BEGIN{exit !($DDUR < 0.5)}" || fail "vídeo comprimido/acelerado: entrada=${INDUR}s salida=${VDUR}s (dif=${DDUR}s)"
pass "velocidad real (entrada=${INDUR}s ≈ salida=${VDUR}s)"

# 3) dedup efectivo: la salida NO puede tener más frames únicos que la entrada
OUT_UNIQ=$(ffmpeg -nostdin -i "$OUT" -an -vf mpdecimate -f null - 2>&1 | grep -Eio 'frame= *[0-9]+' | tail -1 | grep -Eo '[0-9]+')
[ "$OUT_UNIQ" -le "$IN_UNIQ" ] || fail "dedup no efectivo (salida únicos=$OUT_UNIQ > entrada únicos=$IN_UNIQ)"
pass "dedup efectivo (salida únicos=$OUT_UNIQ ≤ entrada únicos=$IN_UNIQ)"

# 4) exactamente 2 pistas de audio con SER
NA=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$OUT" | grep -c .)
[ "$NA" -eq 2 ] || fail "esperadas 2 pistas de audio (Fuente+SER), hay $NA"
pass "2 pistas de audio (Fuente + SER)"

# --- Ejecutar SIN SER (1 audio) ---
OUT1="$WORK/out1.mp4"
ENC=libx264 FPS=$FPS CQ=28 "$SCRIPT" "$BROKEN" "$OUT1" >/dev/null 2>&1 || fail "normalizar.sh (sin SER) salió con error"
NA1=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$OUT1" | grep -c .)
[ "$NA1" -eq 1 ] || fail "sin SER esperada 1 pista de audio, hay $NA1"
pass "sin SER: 1 pista de audio"

echo "== VERDE: test_normalizar OK =="
