FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Explicit source copies: no .env, tests, virtual environments, or local dumps.
COPY main.py init_database.py ./
COPY app/ ./app/
RUN python -m compileall -q main.py init_database.py app \
    && useradd --create-home --uid 10001 bot

USER bot
STOPSIGNAL SIGTERM
CMD ["python", "main.py"]
