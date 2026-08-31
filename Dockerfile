FROM python:3.11-slim

# Log non bufferizzati, niente .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/calcetto.sqlite3

WORKDIR /app

# Dipendenze prima del codice: cache dei layer piu' efficace.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Il file SQLite vive qui, su un volume (vedi docker-compose.yml).
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-m", "bot.main"]
