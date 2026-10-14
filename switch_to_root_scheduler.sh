#!/bin/bash
# Cambio de scheduler: main.py daemon → scheduler.py
# Ejecutar a las 02:05h cuando Canadá vs Qatar haya superado la ventana de 120min

cd /home/david/Portfolio/Mundia2026

echo "[$(date)] === Iniciando cambio de scheduler ===" >> /mnt/khauron/mundial2026/switch.log

# 1. Matar scheduler nuevo (main.py daemon)
pkill -f "python main.py daemon" 2>/dev/null && echo "[$(date)] scheduler main.py terminado" >> /mnt/khauron/mundial2026/switch.log || echo "[$(date)] main.py no estaba activo" >> /mnt/khauron/mundial2026/switch.log

# 2. Matar timer manual de México (para evitar doble grabación)
pkill -f "sleep 14463" 2>/dev/null && echo "[$(date)] timer México terminado" >> /mnt/khauron/mundial2026/switch.log || echo "[$(date)] timer México no encontrado" >> /mnt/khauron/mundial2026/switch.log

# 3. Limpiar PIDFILE muerto
rm -f mundia.pid

# 4. Iniciar scheduler raíz
source venv/bin/activate
nohup python scheduler.py >> /mnt/khauron/mundial2026/mundia.log 2>&1 &
echo "[$(date)] scheduler.py iniciado con PID $!" >> /mnt/khauron/mundial2026/switch.log
