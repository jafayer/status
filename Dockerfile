FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATUS_PAGE_HOST=0.0.0.0 \
    STATUS_PAGE_PORT=8080 \
    STATUS_PAGE_CONFIG=/app/config/status-page.yaml

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY status_page /app/status_page
COPY main.py /app/main.py

RUN mkdir -p /app/data \
    && pip install --upgrade pip \
    && pip install .

EXPOSE 8080

VOLUME ["/app/data"]

CMD ["status-page"]
