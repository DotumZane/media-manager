FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/DotumZane/media-manager" \
    org.opencontainers.image.title="Media Manager" \
    org.opencontainers.image.description="A self-hosted MediaValidator with a guided web setup page."

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg file ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY validator.py /app/validator.py
COPY web /app/web

ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8080

EXPOSE 8080

CMD ["python", "-u", "/app/validator.py"]
