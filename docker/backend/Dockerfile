# docker/backend/Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /code

# Pas de driver ODBC Microsoft installé ici (retiré 2026-08) : le build de
# prod échouait, réseau sortant indisponible même vers les dépôts Debian de
# base (deb.debian.org) — pas juste packages.microsoft.com, donc aucun
# apt-get quel qu'il soit ne peut aboutir dans cet environnement de build.
# Vérifié que rien dans le chemin de service en production n'en a besoin :
# EfmsService (pyodbc/SQL Server) est un chemin legacy remplacé par
# SnowflakeEfmsService pour toute donnée réellement servie ; seuls 2
# commandes de diagnostic manuel (diagnose_efms, export_efms_2025)
# utilisent encore pyodbc.connect(), désormais importé localement dans ces
# méthodes (voir certification/services/efms.py) pour que le reste de
# l'app (Celery, Django) reste important sans le driver système.
# Si ces diagnostics redeviennent nécessaires, installer unixodbc/
# msodbcsql17 dans une image séparée une fois l'accès réseau du serveur de
# build résolu côté infra — pas dans cette image.

# ── Dépendances Python ─────────────────────────────────────────
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Imports fichiers Suivi Carburant — rejoue les mêmes commandes qu'en local
# à chaque démarrage du conteneur (idempotent : update_or_create/get_or_create,
# sans risque à être relancé), pour que la prod ait les mêmes données que le
# local sans étape manuelle après déploiement. Un nouveau fichier mensuel
# (nouveau nom, nouveau --month) doit être ajouté ici ET commité dans
# data_imports/ (voir .gitignore) — pas de mise à jour automatique du mois.
# En arrière-plan (`&`) : des fichiers de 15-32 Mo (xlsb) à parser peuvent
# prendre plus d'une minute — si gunicorn n'écoute qu'une fois les imports
# terminés, le reverse-proxy renvoie 502 sur toutes les routes en attendant.
CMD ["sh", "-c", "(python manage.py import_base_ge --file=data_imports/base_ge.xlsx --month=2026-08; python manage.py import_kpis_par_site --file=data_imports/base_aout_26.xlsx --month=2026-08; python manage.py import_gardien_conso --file=data_imports/synthese_conso_fuel_juillet_2026.xlsb --month=2026-07; python manage.py import_commande_fuel --file=data_imports/commande_fuel_aout_2026.xlsb --month=2026-08) & gunicorn enertrack_backend.wsgi:application --bind 0.0.0.0:8000"]