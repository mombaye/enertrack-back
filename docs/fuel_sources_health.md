# Santé des sources — module Carburant

Un état par source, calculé uniquement à partir des journaux de la plateforme
(`Fuel*SyncRun`, `FuelImportBatch`). Aucune requête vers Snowflake ou ENOC.

| État | Signification |
| --- | --- |
| OK | Dernier passage réussi et plus récent que le seuil |
| PARTIEL | Passage réussi, mais une sous-source manquait (ex. compteur horaire `GFMS_DATA_TRACKER_NC` illisible : heures compteur laissées absentes) |
| RETARD | Dernier succès plus ancien que le seuil |
| ECHEC | Dernier passage en échec : message d'erreur exact, tronqué à 300 caractères |
| BLOQUE | Un passage est « en cours » depuis plus longtemps que le seuil |
| JAMAIS | Aucune exécution ni aucun fichier enregistré |

## Où le lire

- API : `GET /api/fuel-tracking/consommations/sante/` → champ `etat_sources` (ajouté ; les autres champs sont inchangés).
- Commande : `python manage.py check_fuel_sources` (`--strict` : code retour 1 si une source n'est pas OK).
- Tâche planifiée `fuel-sources-health-hourly` (minute 29) : chaque source non OK est journalisée en `ERROR`
  (`WARNING` pour PARTIEL) avec le préfixe `[fuel_sources]`, pour l'alerte par la supervision des logs.

## Seuils (variables d'environnement, valeurs par défaut à valider avec le métier)

| Variable | Défaut | Source |
| --- | --- | --- |
| `FUEL_SOURCE_MAX_AGE_CONSO_MIN` | 60 | Snowflake, conso mensuelle (synchro toutes les 5 min) |
| `FUEL_SOURCE_MAX_AGE_STOCK_MIN` | 60 | Snowflake, stock capteur (5 min) |
| `FUEL_SOURCE_MAX_AGE_FACTS_MIN` | 180 | Snowflake, faits journaliers (toutes les heures) |
| `FUEL_SOURCE_MAX_AGE_ENOC_MIN` | 60 | ENOC (5 min) |
| `FUEL_SOURCE_MAX_AGE_SCSS_DAYS` | 8 | Fichier SCSS (hebdomadaire) |
| `FUEL_GUARD_READING_MAX_AGE_DAYS` | 16 | Fichier Loliforce (déjà utilisé par la couverture gardiennage) |
| `FUEL_SOURCE_MAX_AGE_CLOSING_DAYS` | 45 | Closing mensuel |
| `FUEL_SOURCE_MAX_AGE_TARGET_DAYS` | 45 | Target fuel |

## Correctif du 2026-11-12 : faits journaliers Snowflake

`GENSET_REPORT` n'a pas de colonne `REPORT_DATE` : la colonne de date est `DATE`.
Avant le correctif, toute la requête des faits journaliers échouait (`000904 invalid identifier`),
et le contrôle CPH ne recevait aucune donnée. Les commandes `diagnose_cph_sources` et
`diagnose_cph_site` avaient la même erreur.

La vue `DB_GFMS_PROD.GOLD.GFMS_DATA_TRACKER_NC` est en erreur de compilation côté Snowflake (`002057`).
La synchro teste désormais la vue avant la requête (`SELECT 1 … LIMIT 0`). Si la vue est illisible,
la synchro passe sans le compteur : les heures du compteur restent absentes (jamais 0), et le passage
est enregistré en succès avec un `AVERTISSEMENT`, qui apparaît en état PARTIEL.

## Base de la vue `VW_FUEL_REPORT`

Variable `FUEL_REPORT_DATABASE`, valeur par défaut `DB_GFMS_ANALYTICS_DEV` (comportement inchangé).
Quand l'admin Snowflake aura publié la vue en production, mettre `FUEL_REPORT_DATABASE=DB_GFMS_ANALYTICS_PROD`
dans l'environnement du serveur, sans changer le code. Seul un identifiant simple est accepté : le nom est inséré dans le SQL.
