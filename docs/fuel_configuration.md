# Rubrique Configuration — module Carburant

Envoi des fichiers sources depuis l'interface (Suivi Carburant → Configuration), à la place du dépôt dans le
répertoire partagé monté. Le répertoire partagé reste lu par la tâche `process_fuel_import_incoming` : les deux voies
utilisent la même chaîne d'import (`stock_import_pipeline.process_bytes`), avec archive, anti-doublon par empreinte
SHA-256 et lignes rejetées tracées.

Types acceptés : `CLOSING` (Synthèse / Closing mensuel), `TARGET_FUEL`, `SCSS`, `LOLIFORCE`, `TERRAIN_MENSUEL`,
`TANK_MODELS`, `HISTORICAL_REFUELING`. Formats : `.xlsx`, `.xlsm`, `.xlsb`.

## Accès par code

- Le code à 6 chiffres n'est jamais stocké en clair : le serveur compare son empreinte (`FUEL_CONFIG_CODE_HASH`,
  hachage Django PBKDF2).
- 5 essais faux par utilisateur → blocage 15 minutes.
- Code juste → jeton signé, lié à l'utilisateur connecté, valable 2 heures, envoyé dans l'en-tête `X-Fuel-Config-Token`
  (ajouté à `CORS_ALLOW_HEADERS`).
- La vraie protection reste la connexion EnerTrack : un code à 6 chiffres seul est faible.

Le code est **unique et fixe** (décision du 2026-11-12) : tout autre code est refusé. Il n'est volontairement pas
modifiable par variable d'environnement (une variable `FUEL_CONFIG_CODE_HASH` dans `.env` est ignorée). Le changer
demande de modifier l'empreinte dans `enertrack_backend/settings.py`, avec revue et déploiement.

## API

| Méthode | Adresse | Rôle |
| --- | --- | --- |
| POST | `/api/fuel-tracking/configuration/acces/` | `{"code"}` → `{token, expires_in}` ; 403 `CONFIG_BAD_CODE` (+ `essais_restants`), 429 `CONFIG_LOCKED` |
| GET | `/api/fuel-tracking/configuration/fichiers/` | 20 derniers lots, lieu de stockage (`REPERTOIRE_PARTAGE` ou `VOLUME_PLATEFORME`) |
| POST | `/api/fuel-tracking/configuration/fichiers/` | multipart `file` + `source_type` → 202 `{ticket, etat: "EN_COURS"}` |
| GET | `/api/fuel-tracking/configuration/fichiers/?ticket=…` | `EN_COURS`, `TERMINE` (+ `batch`) ou `ECHEC` (+ `detail`) |

## Traitement en arrière-plan

Mesuré le 2026-11-12 en préproduction isolée : la Synthèse de septembre (`.xlsb`, 30 Mo) demande environ 105 s de
traitement, trop près du délai de gunicorn (120 s). L'envoi est donc :

1. écrit sur le volume `media_data` (`/code/media/fuel_imports/_config_pending/<ticket>/`), monté dans `web`,
   `celery` et `celery-interactive` ;
2. traité par la tâche `fuel_tracking.process_config_upload` sur la file `interactive` (limite 30 min) ;
3. le résultat est écrit dans `result.json`, la copie temporaire est supprimée (l'archive reste dans `archive/AAAA-MM`).

Sans résultat après 35 minutes, l'envoi est signalé `ECHEC` (worker interrompu) : renvoyer le fichier.
Broker indisponible à l'envoi → 503, rien n'est laissé en attente.

Résultat de la Synthèse de septembre en préproduction : 3 378 lignes, 3 377 acceptées, 1 à valider, 0 rejetée ;
renvoi du même fichier reconnu comme doublon.

## Stockage

Si le répertoire `FUEL_IMPORT_ROOT` est monté, il reste utilisé. Sinon, les fichiers vont sur le volume persistant
`media_data` (`/code/media/fuel_imports`), conservé entre les déploiements.

## Variables d'environnement

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `FUEL_CONFIG_TOKEN_TTL_S` | 7200 | Durée du jeton (s) |
| `FUEL_CONFIG_MAX_ATTEMPTS` | 5 | Essais faux avant blocage |
| `FUEL_CONFIG_LOCK_S` | 900 | Durée du blocage (s) |
| `FUEL_CONFIG_MAX_UPLOAD_MB` | 50 | Taille maximale d'un fichier |

## À vérifier au déploiement

- Le proxy inverse devant l'API doit accepter des corps d'au moins 50 Mo (`client_max_body_size 50m` pour nginx).
- Le compteur d'essais utilise le cache Django, déjà sur Redis (`CACHES`) : la limite vaut pour tous les processus web.
