# docker/backend/Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /code

# ── 1. Drivers système + ODBC Microsoft ──────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gnupg2 \
        apt-transport-https \
        unixodbc \
        unixodbc-dev \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor > /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] \
        https://packages.microsoft.com/debian/11/prod bullseye main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Dépendances Python ─────────────────────────────────────────
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

CMD ["gunicorn", "enertrack_backend.wsgi:application", "--bind", "0.0.0.0:8000"]