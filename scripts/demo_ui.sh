#!/usr/bin/env bash
# Démarre un backend de DÉMO (SQLite + données synthétiques) sur http://localhost:8000 pour voir l'interface sans toucher à la production.
# Connexion : utilisateur « demo » / mot de passe « demo-egrid-123 ».  Arrêt : Ctrl+C.   Réinitialiser : ./scripts/demo_ui.sh --reset
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/scripts:$PWD${PYTHONPATH:+:$PYTHONPATH}"
export DJANGO_SETTINGS_MODULE=demo_settings
[ "${1:-}" = "--reset" ] && rm -rf .demo && echo "Démo réinitialisée."
if [ ! -f .demo/demo.sqlite3 ]; then
  echo "→ Création de la base de démo (migrations + données synthétiques)…"
  python manage.py migrate --no-input >/dev/null
  python scripts/demo_seed.py
fi
echo "→ API de démo : http://localhost:8000   (demo / demo-egrid-123)"
exec python manage.py runserver 127.0.0.1:8000 --noreload
