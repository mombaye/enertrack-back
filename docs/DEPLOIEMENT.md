# Déploiement du backend

Une seule commande, depuis le dossier du projet sur le serveur :

```bash
./deploy.sh            # branche master
```

Ce qui est automatique (aucune autre action manuelle) :

| Étape | Fait par |
|---|---|
| Récupération du code (`git pull --ff-only`) | `deploy.sh` |
| Reconstruction de l'image et redémarrage de `web`, `celery`, `celery-beat` (même image, même version) | `deploy.sh` |
| Migrations de base de données | entrypoint du service `web` |
| Collecte des fichiers statiques, import Stan | entrypoint du service `web` |
| Reconstruction des tables Pilotage Grid si elles sont vides alors que les jeux de données existent | `fuel_post_deploy` (entrypoint, arrière-plan) |
| Recalcul des bilans de stock des 3 dernières périodes qui datent de l'ancienne règle (sans variation de stock) | `fuel_post_deploy` (entrypoint, arrière-plan) |

`fuel_post_deploy` est idempotent : relancé, il ne fait rien si tout est à jour (`--dry-run` pour simuler). Il ne crée ni ne modifie
jamais de certification et ne touche ni à ENOC ni à Snowflake.

L'import initial Pilotage Grid est **automatique** : le HTML d'origine est versionné (`data_imports/pilotage_grid/pilotage_grid_initial.html`) et `fuel_post_deploy` charge les jeux de données **absents** de la base (jamais d'écrasement d'une version importée depuis l'interface). Un simple `git pull` + redéploiement suffit. Reste manuel : l'import du target fuel — ou son dépôt dans `incoming/target_fuel/`.
