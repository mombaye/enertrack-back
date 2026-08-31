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

# Imports fichiers Suivi Carburant — rejoue les mêmes commandes qu'en local
# à chaque démarrage du conteneur (idempotent : update_or_create/get_or_create,
# sans risque à être relancé), pour que la prod ait les mêmes données que le
# local sans étape manuelle après déploiement. Un nouveau fichier mensuel
# (nouveau nom, nouveau --month) doit être ajouté ici ET commité dans
# data_imports/ (voir .gitignore) — pas de mise à jour automatique du mois.
CMD ["sh", "-c", "python manage.py import_base_ge --file=data_imports/base_ge.xlsx --month=2026-08; python manage.py import_kpis_par_site --file=data_imports/base_aout_26.xlsx --month=2026-08; python manage.py import_gardien_conso --file=data_imports/synthese_conso_fuel_juillet_2026.xlsb --month=2026-07; python manage.py import_commande_fuel --file=data_imports/commande_fuel_aout_2026.xlsb --month=2026-08; gunicorn enertrack_backend.wsgi:application --bind 0.0.0.0:8000"]