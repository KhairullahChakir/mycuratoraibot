import os
from pathlib import Path

from dotenv import load_dotenv

if not (os.getenv("RAILWAY_ENVIRONMENT_ID") or os.getenv("RAILWAY_PROJECT_ID")):
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL")
