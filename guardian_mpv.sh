#!/bin/bash
PAUSA=--pause
while true; do
  mpv --title="DERBI" --mute=yes $PAUSA --input-ipc-server=/tmp/mpv_derbi --cache=yes --cache-secs=600 \
      --demuxer-max-bytes=800MiB --demuxer-readahead-secs=600 --demuxer-lavf-o=live_start_index=0 \
      --video-sync=display-resample --hwdec=auto http://servidor-g.ejemplo:8080/Match_TV_HD/index.m3u8
  echo "$(date +%H:%M:%S) mpv video salió ($?), relanzo en 2 s"; PAUSA=""; sleep 2
done
