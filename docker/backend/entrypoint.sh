#!/bin/sh
set -e

echo "Attente de PostgreSQL..."
until python -c "
import psycopg2, os, sys
try:
    psycopg2.connect(
        dbname=os.environ['POSTGRES_DB'],
        user=os.environ['POSTGRES_USER'],
        password=os.environ['POSTGRES_PASSWORD'],
        host=os.environ['POSTGRES_HOST'],
        port=os.environ.get('POSTGRES_PORT', '5432'),
    )
    sys.exit(0)
except Exception:
    sys.exit(1)
"; do
    echo "  PostgreSQL non prêt, attente 2s..."
    sleep 2
done
echo "PostgreSQL prêt."

echo "Application des migrations..."
python manage.py migrate --no-input

echo "Collecte des fichiers statiques..."
python manage.py collectstatic --no-input

echo "Import auto fichier Stan (data_imports/stan/)..."
python manage.py auto_import_stan

echo "Démarrage de Gunicorn..."
exec gunicorn enertrack_backend.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 3 \
    --timeout 120
