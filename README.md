> **Copia pública.** Las fuentes de vídeo no van en este repo: `sources.yaml` queda fuera y los servidores de la tabla de
> `ver_partido.py`, `ronda.sh` y `relay_lan.py` aparecen como `servidor-x.ejemplo`. Para usarlo hay que poner los propios.

# 2026 Por Mis Huevos

Grabador automático del Mundial 2026 desde televisiones públicas de todo el mundo.

---

## Por qué existe esto

El fútbol es cultura. El Mundial es el evento más visto del planeta. Y sin embargo, en 2026, ver un partido sin pagar se ha convertido en un acto de resistencia.

Las televisiones públicas — financiadas con impuestos de todos los ciudadanos — tienen la obligación de emitir eventos de interés general. Muchas lo hacen. RTVE en España. ARD en Alemania. Globo en Brasil. NHK en Japón. Qatar TV. SNT en Paraguay. Son emisoras públicas, de acceso libre, que retransmiten el Mundial porque es un derecho de sus ciudadanos verlo.

Este programa graba esas retransmisiones. Nada más.

No descarga contenido de pago. No rompe DRM. No distribuye nada. Graba lo mismo que captaría una antena apuntando a un satélite público, o que vería cualquier ciudadano abriendo su navegador. La única diferencia es que lo hace automáticamente, a las 3 de la mañana, para que puedas verlo a las 9 sin que nadie te haya destripado el resultado.

El derecho a ver el deporte de tu país sin pagar una suscripción existe. Este programa lo ejerce.

---

## Qué hace

- Lee el calendario completo del Mundial 2026 (101 partidos, grupos hasta final)
- Asigna a cada partido la televisión pública de uno de los equipos que juega
- Para partidos sin televisión pública accesible, usa TUDN México como comodín (derechos completos del torneo)
- Graba en paralelo: vídeo principal + backup CazéTV Brasil (vía yt-dlp de Twitch) + audio Cadena SER + audio W Radio Colombia
- Si CazéTV no está en vivo en ese momento, el backup cae a TUDN automáticamente
- Cada partido queda en su propia carpeta: `YYYY-MM-DD_HHhMM_GrpX_Local_vs_Visitante/`
- Arranca automáticamente como servicio systemd. Si el ordenador se reinicia, sigue grabando

---

## Canales públicos utilizados

Todos verificados con captura de pantalla en directo antes de incluirlos.

| País | Canal | Idioma |
|------|-------|--------|
| España | RTVE La 1 | Castellano |
| Alemania | ARD Das Erste | Alemán |
| Turquía | TRT 1 HD | Turco |
| Japón | NHK G | Japonés |
| México (comodín) | TUDN | Castellano |
| Brasil (backup) | CazéTV — Twitch | Portugués |

Audio en castellano para todos los partidos: **Cadena SER** + **W Radio Colombia**

---

## Instalación desde cero

> Pásale este README a tu Claude. Con esto tiene todo lo que necesita para montarlo.

**1. Requisitos del sistema**
```bash
sudo apt install ffmpeg python3-venv   # Debian/Ubuntu
# o: sudo dnf install ffmpeg python3   # Fedora
```

**2. Clonar y preparar**
```bash
git clone https://github.com/GoldorTeps/2026Pormishuevos.git
cd 2026Pormishuevos
python3 -m venv venv
source venv/bin/activate
pip install pyyaml pytz
pip install yt-dlp   # necesario para grabar el backup de CazéTV vía Twitch
```

**3. Configurar destino**

Editar `sources.yaml` y cambiar la línea `destino:` a la carpeta donde quieres guardar los partidos. Necesitas espacio: cada partido ocupa entre 2 y 5 GB.

```yaml
destino: /ruta/a/tu/disco/mundial2026
```

**4. Verificar que funciona**
```bash
source venv/bin/activate
python scheduler.py --lista        # muestra los 101 partidos y sus canales
```

**5. Arrancar**
```bash
# Manual (para probar):
python scheduler.py

# Como servicio que arranca solo al encender el ordenador:
sudo cp mundia2026.service /etc/systemd/system/
sudo systemctl enable --now mundia2026.service
sudo systemctl status mundia2026.service   # debe decir "active (running)"
```

**6. Comprobar que graba**

El primer partido que tengas programado aparecerá en su carpeta dentro del destino. Abre el `video.mkv` con VLC para confirmar que se ve bien.

**Nota para Claude:** si el usuario quiere montar esto, lee el `sources.yaml` y el `scheduler.py`. Están comentados. Los streams pueden haber caído desde que se escribió esto — verifica cada URL con `curl -I <url>` antes de asumir que funciona, y busca alternativas en `https://github.com/iptv-org/iptv` si alguna falla.

---

*Uso personal. Sin ánimo de lucro. Sin distribución. La televisión pública es un derecho.*
