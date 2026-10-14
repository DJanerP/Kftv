#!/bin/bash
# Ronda de fuentes: un frame de cada una, mosaico. Uso: ronda.sh salida.jpg
S=$(dirname "$0")/logs; OUT=${1:-$S/ronda.jpg}; D=$S/ronda_fr; mkdir -p $D; rm -f $D/*.jpg
declare -A F=(
 [ultra]=http://servidor-g.ejemplo:8080/Match_TV_HD/index.m3u8
 [a0dz]=http://servidor-c.ejemplo:9998/play/a0dz/index.m3u8
 [a07z]=http://servidor-a.ejemplo:8000/play/a07z/index.m3u8
 [a080]=http://servidor-a.ejemplo:8000/play/a080/index.m3u8
 [a081]=http://servidor-a.ejemplo:8000/play/a081/index.m3u8
 [a07n]=http://servidor-a.ejemplo:8000/play/a07n/index.m3u8
 [a074]=http://servidor-a.ejemplo:8000/play/a074/index.m3u8
 [a09l]=http://servidor-a.ejemplo:8000/play/a09l/index.m3u8
 [a08c]=http://servidor-a.ejemplo:8000/play/a08c/index.m3u8
 [a06a]=http://servidor-a.ejemplo:8000/play/a06a/index.m3u8
 [a027]=http://servidor-b.ejemplo:8000/play/a027/index.m3u8
 [a028]=http://servidor-b.ejemplo:8000/play/a028/index.m3u8
)
for n in "${!F[@]}"; do
  ( timeout 22 ffmpeg -y -loglevel quiet -user_agent "Mozilla/5.0" -i "${F[$n]}" -frames:v 1 \
      -vf "scale=640:360,drawtext=text=$n:fontcolor=yellow:fontsize=30:box=1:boxcolor=black@0.5:x=8:y=8" \
      $D/$n.jpg </dev/null >/dev/null 2>&1 ) &
done; wait
montage -geometry 640x360+2+2 -tile 4x3 $D/*.jpg "$OUT"
echo "$OUT: $(ls $D/*.jpg | wc -l) frames"
