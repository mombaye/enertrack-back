#!/bin/sh
# Déploiement du backend en une commande : code, image, migrations (entrypoint), tâches post-déploiement, redémarrage web + celery + celery-beat.
# L'entrypoint du service web applique les migrations, collecte les statiques et lance fuel_post_deploy ; celery et celery-beat repartent sur la même image.
#   usage :  ./deploy.sh            (branche master)
set -e
BRANCH="${1:-master}"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"
echo "Version déployée : $(git log --oneline -1)"
docker compose up -d --build web celery celery-interactive celery-beat
echo "Attente du service web…"
for i in $(seq 1 30); do
  if docker compose exec -T web python manage.py check >/dev/null 2>&1; then
    docker compose exec -T web python manage.py showmigrations --plan | grep -c '\[ \]' | sed 's/^0$/Migrations : toutes appliquées/; t; s/^/Migrations en attente : /'
    docker compose ps
    exit 0
  fi
  sleep 3
done
echo "ATTENTION : le service web ne répond pas" >&2
exit 1
