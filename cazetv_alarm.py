#!/usr/bin/env python3
"""
Alarma CazéTV — se ejecuta justo antes del partido Portugal vs RD Congo (19:00h).
Prueba si yt-dlp puede extraer la URL del directo y notifica por Telegram + escritorio GTK.
"""
import sys
import subprocess
from pathlib import Path

KHAURON_DIR = Path(__file__).parent.parent / 'Khauron'
sys.path.insert(0, str(KHAURON_DIR))

import site
_venv_site = KHAURON_DIR / "venv" / "lib" / "python3.11" / "site-packages"
if _venv_site.exists():
    site.addsitedir(str(_venv_site))

from indexador.storage import crear_storage


def test_cazetv():
    url = 'https://www.youtube.com/@cazeTv/live'
    result = subprocess.run(
        ['yt-dlp', '--geo-bypass-country', 'BR',
         '-f', 'best[protocol=m3u8_native]/best',
         '--get-url', '--no-warnings', url],
        capture_output=True, text=True, timeout=90,
    )
    if result.returncode == 0:
        urls = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        if urls:
            return True, urls[0]
    return False, (result.stderr.strip().splitlines() or ['sin output'])[-1]


if __name__ == '__main__':
    print("Probando CazéTV (Portugal vs RD Congo 19:00h)...")
    ok, detalle = test_cazetv()

    if ok:
        msg     = "✅ CazéTV EN VIVO — Portugal vs RD Congo (19:00h) se grabará automáticamente."
        resumen = f"URL extraída: {detalle[:120]}"
        print("OK:", detalle[:100])
    else:
        msg     = "❌ CazéTV sin stream — Portugal vs RD Congo (19:00h). ¿Revisar manualmente?"
        resumen = f"yt-dlp devolvió: {detalle}"
        print("FALLO:", detalle)

    # Insertar resultado en preguntas_queue y disparar escritorio + Telegram via watcher
    st = crear_storage()
    st.encolar_pregunta(
        tipo='libre',
        pregunta=msg,
        contexto={'resumen': resumen, 'proyecto': 'Mundia2026', 'es_alarma': True, 'canal_alarma': 'ambos'},
        canal='desktop',
    )
    st.cerrar()

    # Disparar escritorio ahora (sin esperar al timer de 5 min)
    subprocess.run(['systemctl', '--user', 'restart', 'khauron-validar.service'], timeout=10)
    # El watcher también enviará el Telegram cuando se ejecute
    subprocess.run(['python3', str(KHAURON_DIR / 'desktop' / 'khauron_alarma_watcher.py')], timeout=30)

    print("Notificación enviada — escritorio + Telegram.")
