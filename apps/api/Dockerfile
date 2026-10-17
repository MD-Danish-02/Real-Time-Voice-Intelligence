# syntax = docker/dockerfile:1.4
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-voice.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Install voice runtime packages; install CPU-optimized torch/torchaudio to avoid multi-gigabyte CUDA bloat on CPU hosts
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu torch torchaudio \
    && pip install --no-cache-dir -r requirements-voice.txt

COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY app/ ./app/
COPY tests/ ./tests/
RUN useradd -m app && chown -R app /app
USER app

EXPOSE 8000 10000-10020/udp
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
