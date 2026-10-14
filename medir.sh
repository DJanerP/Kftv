#!/bin/bash
# medir.sh NOMBRE URL — 30 s de decodificación real: errores, fps, resolución.
n=$1; u=$2; L=$(dirname "$0")/logs/medir_$n.log
timeout 45 ffmpeg -loglevel warning -user_agent "Mozilla/5.0" -i "$u" -t 30 -f null - > $L 2>&1 </dev/null
err=$(grep -cE "decode_slice_header|non-existing SPS|Invalid NAL|corrupt|Error" $L)
info=$(timeout 15 ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate -of csv=p=0 "$u" 2>/dev/null | head -1)
echo "$n errores30s=$err  $info"
