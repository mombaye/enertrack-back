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


## Validation §11 sur la production (lecture seule)

Après `git pull` + redéploiement (la migration 0056 s'applique seule) :

```bash
docker compose exec web python manage.py fuel_validation_prod --json /code/data_imports/validation_prod.json
```
Affiche la période exacte, les bilans par statut, les couvertures (target, CPH complète/partielle, instrumentée, Closing), 7 contrôles PASS/FAIL
(Ponctions = 0 L, aucun NULL transformé en 0, périodes identiques, CPH complète ≠ partielle, certifiée vide avant validation, variation négative jamais
certifiée, target jamais dérivé d'une puissance ou d'une courbe) et des sites réels pour chaque situation clé. Rien n'est écrit, certifié ni recalculé.
Avant : `docker compose exec web python manage.py fuel_auto_balances --mois 2` si les bilans de la période ne sont pas calculés.

Nouveau type de fichier : « Remontées terrain mensuelles » (`TERRAIN_MENSUEL`, dossier `incoming/terrain_mensuel/` ou upload) — complémentaire,
jamais prioritaire, mouvements non retenus tant qu'ils ne sont pas validés.


## Closing mensuel : toutes les lignes du fichier (migration 0058)

Le fichier Closing est désormais conservé **ligne à ligne** (`FuelClosingLine`) et rattaché au **mois comptable du fichier** (cellule « Dernier jour du mois de SEPTEMBRE »),
et non plus à la date de relevé de chaque site. « Consommation Closing » = Σ de la colonne « Qtité cons considérée » de TOUTES les lignes du fichier.
Le format 2026 (« Total Livraison » qui contient déjà les rajouts/retraits, target FI, Analysis #1 à #9, région, on/off-grid) est lu ; un contrôle de cohérence compare les
totaux lus, importés et le bloc de KPI du fichier (écart > 1 L = avertissement dans le rapport du lot).

Après `git pull` + `./deploy.sh` (la migration 0058 s'applique seule) : **relancer l'import du fichier de septembre 2026** (écran Suivi Stock → Imports,
« Relancer » sur le lot Closing de septembre, ou nouvel import), puis `docker compose exec web python manage.py fuel_auto_balances --mois 2` pour recalculer les bilans.
Test de non-régression sur le vrai fichier : `FUEL_TEST_CLOSING_FILE=/chemin/Esco...xlsb python manage.py test fuel_tracking.tests_closing_september.RealSeptemberFileTest`.
