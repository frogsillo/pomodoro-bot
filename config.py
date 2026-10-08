"""Configuración global. Lee .env y expone constantes."""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Directorios
AUDIO_DIR = BASE_DIR / "audio"
DATA_DIR = BASE_DIR / "data"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "pomodoro.db"

# Modo de prueba: POMODORO_TEST=1 en .env
TEST_MODE = os.getenv("POMODORO_TEST", "0").strip().lower() in ("1", "true", "yes", "on")

if TEST_MODE:
    WORK_SECONDS = 10
    SHORT_BREAK_SECONDS = 5
    LONG_BREAK_SECONDS = 8
else:
    WORK_SECONDS = 25 * 60
    SHORT_BREAK_SECONDS = 5 * 60
    LONG_BREAK_SECONDS = 15 * 60

POMODOROS_PER_CYCLE = 4

# Audio( el audio de mi xanxita que planeo colocar aca --> los tipos de formato son ineccesarios pero estaba conociendo 
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac", ".aac", ".webm"}
MAX_SOUND_BYTES = 10 * 1024 * 1024  # 10 MB