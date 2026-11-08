FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ядро
COPY main.py dashboard.html furry_archiver.py migrate_production.py ./
COPY core/        ./core/
COPY blueprints/  ./blueprints/
COPY static/      ./static/
COPY locales/     ./locales/
COPY migrations/  ./migrations/

# застосунки (furry, дублікати, чат, онлайн, e621, inkbunny)
COPY apps/        ./apps/

CMD ["python", "-u", "main.py"]
