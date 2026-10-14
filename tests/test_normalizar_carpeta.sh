#!/bin/bash
# test_normalizar_carpeta.sh — Cancerbero de normalizar_carpeta.sh.
# Hermético (ENC=libx264, sin red). Monta una carpeta de partido con DOS fuentes (una rota con
# frames duplicados y una limpia) + un ser.mp3, y verifica el contrato:
#   1) TODAS las fuentes .mkv → su <fuente>_fluido.mp4 (normalización incondicional; el detector por
#      umbral se retiró el 04-jul porque la duplicación a ráfagas lo hacía poco fiable)
#   2) cada _fluido.mp4 tiene 2 pistas de audio (Fuente + SER) y audio/vídeo SINCRONIZADOS
#      (el normalizador es VFR desde el 05-jul: conserva timestamps reales, ya no fuerza CFR)
#   3) los .mkv crudos se conservan
#   4) idempotencia: una segunda pasada NO regenera los _fluido ya existentes
set -euo pipefail
export LC_ALL=C   # duration viene con punto decimal; un locale con coma lo truncaría (falso positivo)

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../normalizar_carpeta.sh"
[ -x "$SCRIPT" ] || chmod +x "$SCRIPT"
[ -x "$HERE/../normalizar.sh" ] || chmod +x "$HERE/../normalizar.sh"

WORK="$(mktemp -d)"; DIR="$WORK/partido"; mkdir -p "$DIR"
trap 'rm -rf "$WORK"' EXIT

pass(){ echo "  ✓ $*"; }
fail(){ echo "  ✗ $*" >&2; exit 1; }
echo "== test_normalizar_carpeta =="

# fuente ROTA: 5 fps mostrado a 30 fps → duplicados
ffmpeg -nostdin -y -f lavfi -i "testsrc=size=320x240:rate=5" -f lavfi -i "sine=frequency=1000" \
  -vf "fps=30" -t 4 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$DIR/rota.mkv" >/dev/null 2>&1
# fuente LIMPIA: 25 fps CFR real (ruido temporal → cada frame distinto)
ffmpeg -nostdin -y -f lavfi -i "testsrc=size=320x240:rate=25" -f lavfi -i "sine=frequency=800" \
  -vf "noise=alls=40:allf=t+u" -t 4 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$DIR/limpia.mkv" >/dev/null 2>&1
ffmpeg -nostdin -y -f lavfi -i "sine=frequency=440" -t 4 -c:a libmp3lame "$DIR/ser.mp3" >/dev/null 2>&1
[ -s "$DIR/rota.mkv" ] && [ -s "$DIR/limpia.mkv" ] && [ -s "$DIR/ser.mp3" ] || fail "no se montó el fixture"
pass "fixture montado (rota + limpia + ser)"

ENC=libx264 CQ=28 "$SCRIPT" "$DIR" >/dev/null 2>&1 || fail "normalizar_carpeta salió con error"

# 1 + 2) TODAS las fuentes normalizadas, con 2 pistas de audio y vídeo CFR
for f in rota limpia; do
  OUT="$DIR/${f}_fluido.mp4"
  [ -s "$OUT" ] || fail "no se normalizó $f.mkv"
  na=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$OUT" | grep -c .)
  [ "$na" -eq 2 ] || fail "${f}_fluido.mp4 debería tener 2 audios (Fuente+SER), tiene $na"
  vdur=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration -of csv=p=0 "$OUT")
  adur=$(ffprobe -v error -select_streams a:0 -show_entries stream=duration -of csv=p=0 "$OUT")
  skew=$(awk "BEGIN{d=$adur-$vdur; printf \"%.3f\",(d<0?-d:d)}")
  awk "BEGIN{exit !($skew < 0.5)}" || fail "${f}_fluido.mp4 desincronía A/V (vídeo=${vdur}s audio=${adur}s desfase=${skew}s)"
  pass "$f.mkv → ${f}_fluido.mp4 (2 audios, A/V sync ${skew}s)"
done

# 3) crudos conservados
[ -f "$DIR/rota.mkv" ] && [ -f "$DIR/limpia.mkv" ] || fail "se perdió algún .mkv crudo"
pass "los .mkv crudos se conservan"

# 4) idempotencia: no regenera los ya existentes
before=$(stat -c '%Y' "$DIR/rota_fluido.mp4")
sleep 1
ENC=libx264 CQ=28 "$SCRIPT" "$DIR" >/dev/null 2>&1 || fail "2ª pasada salió con error"
after=$(stat -c '%Y' "$DIR/rota_fluido.mp4")
[ "$before" = "$after" ] || fail "la 2ª pasada regeneró rota_fluido.mp4 (no es idempotente)"
pass "idempotente (2ª pasada no regenera lo existente)"

echo "== VERDE: test_normalizar_carpeta OK =="
