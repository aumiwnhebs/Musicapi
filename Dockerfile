FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade --force-reinstall yt-dlp

COPY main.py .

RUN if [ -f cookies.txt ]; then cp cookies.txt /app/cookies.txt; fi

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

ENTRYPOINT ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "--threads", "4", "main:app"]
