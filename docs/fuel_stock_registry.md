# Registre unifié de stock et de mouvements carburant (Suivis Stock — Phase 1)

Socle Stock **sans** consommation réelle certifiée ni commande automatique. Les calculs CPH, les courbes GE et
les règles de commandes ne sont pas modifiés.

## Principes

- Une donnée absente reste `NULL`, jamais `0`.
- Rien n'est supprimé physiquement (`on_delete=PROTECT` sur lots → lignes → relevés / mouvements).
- Tout changement (validation, rejet, correction de cuve, mise à jour à la source, relance) est tracé dans
  `fuel_registry_correction` avec l'ancienne valeur, la nouvelle, l'auteur, la date et le motif.
- On ne fait jamais de moyenne entre deux relevés contradictoires, et on ne choisit jamais silencieusement.
- Snowflake reste une **information technique** : il est affiché dans l'historique et l'ancien tableau, mais n'est jamais retenu comme stock.

## Tables (migration `fuel_tracking/0043_stock_registry`, ajouts uniquement)

| Table | Rôle |
|---|---|
| `fuel_import_batch` | Fichier reçu : type (ENOC, LOLIFORCE, SCSS, CLOSING, HISTORICAL_REFUELING), empreinte SHA-256, statut RECEIVED / PROCESSING / VALIDATION_REQUIRED / ACCEPTED / REJECTED / FAILED, compteurs, doublon de, relance de + motif |
| `fuel_import_row` | Ligne brute (`raw_payload`), `site_id_raw` et `site_id_resolved`, `event_datetime`, statut ACCEPTED / WARNING / REJECTED / PENDING, messages `[{code, message}]` |
| `fuel_stock_observation` | Relevé : source, `source_event_id` (unique par source), site, cuve, date et heure, niveau brut, unité L / CM, litres, méthode (DIRECT_LITERS / TANK_CURVE / GEOMETRIC_CALCULATION / NONE), statut de conversion (VALID / PENDING_TANK_MAPPING / INVALID), compteur horaire, utilisateur source, référence, commentaire, preuve, statut de validation (PENDING / VALIDATED / REJECTED) et motif, `review_flags` |
| `fuel_movement` | Mouvement : source (ENOC, HISTORICAL_REFUELING, CLOSING), type (DELIVERY_EYDON, DELIVERY_TOTAL_CARD, ADD, TRANSFER_IN, TRANSFER_OUT, WITHDRAWAL, THEFT, DRAIN, CORRECTION), quantité demandée et quantité réalisée (`NULL` si non déclarée), fournisseur, ticket, bon, preuve, statut DECLARED / CLOSED / VALIDATED / REJECTED, `validated_by` / `validated_at`, alertes |
| `fuel_tank_master` | Cuve : forme (HORIZONTAL_CYLINDER, VERTICAL_CYLINDER, RECTANGULAR, HORIZONTAL_ELLIPTICAL, CUSTOM_CURVE), dimensions en m, capacité, abaque `[[cm, L], …]`, statut PENDING / VALIDATED / REJECTED |
| `fuel_guard_scope` | Périmètre de gardiennage : site, prestataire LOLIFORCE / SCSS, `active_from` / `active_to` |
| `fuel_registry_correction` | Piste d'audit |

`HORIZONTAL_ELLIPTICAL` s'ajoute à la liste demandée : le closing contient la forme « Cuve Cylindrique Ellyptique Horizontale ».
`HISTORICAL_REFUELING` s'ajoute aux types de lot pour l'historique refueling.

## Conversion cm → L (`services/tank_conversion.py`)

Aucun coefficient générique n'est utilisé. La conversion passe par une cuve **validée**, dans cet ordre :

1. abaque validé (`TANK_CURVE`, interpolation linéaire) ;
2. géométrie avec des dimensions validées (`GEOMETRIC_CALCULATION`) ;
3. sinon, litres à `NULL` et statut `PENDING_TANK_MAPPING`.

Formules (dimensions en m, h = niveau en m, résultat × 1000 pour les litres) :

| Forme | Formule |
|---|---|
| Parallélépipède | L·l·h |
| Cylindre vertical | π(D/2)²·h |
| Cylindre horizontal | L·[r²·acos((r−h)/r) − (r−h)·√(2rh−h²)] |
| Elliptique horizontal | (l/H) × cylindre horizontal de diamètre H |

La formule du cylindre horizontal a été vérifiée sur le closing. Pour le site ACAD (L 2,84 m, D 1,6 m, niveau 0,18 m), le fichier donne 353,18 L et la formule donne 353,2 L.

Contrôles appliqués :

- niveau négatif → INVALID ;
- niveau au-dessus de la hauteur physique → INVALID ;
- volume au-dessus de la capacité × (1 + `FUEL_CAPACITY_TOLERANCE_PCT`, 5 % par défaut) → INVALID ;
- dimensions manquantes → PENDING_TANK_MAPPING ;
- cuve non validée → PENDING_TANK_MAPPING ;
- plusieurs cuves sans `tank_id` → PENDING_TANK_MAPPING, sans jamais attribuer le relevé à la cuve 1.

Le stock d'un site est la somme des cuves validées relevées le même jour. Si une cuve validée n'a pas de relevé ce jour-là, le stock vaut `NULL` et le motif est affiché.

## Validation automatique d'un relevé

`VALIDATED` uniquement si **toutes** ces conditions sont réunies :

- conversion valide ;
- capacité de cuve validée, ce qui permet le contrôle de dépassement ;
- pour Loliforce / SCSS, site dans le périmètre de ce prestataire à la date du relevé ;
- aucun `review_flag`.

Sinon le relevé est `PENDING` avec son motif, ou `REJECTED` s'il est physiquement invalide (négatif, au-dessus de la hauteur ou de la capacité).

Périmètre de gardiennage :

- pas de périmètre configuré → À VALIDER ;
- autre prestataire configuré → À VALIDER, **jamais rejeté** ;
- aucun site n'est supposé Loliforce par défaut.

## Comparaison Loliforce / SCSS

Conditions : même site et même cuve, relevés en litres espacés de moins de 48 h, capacité validée.

La tolérance est max(100 L, 10 % de la capacité) :

- écart dans la tolérance → `COHERENT` ;
- écart au-delà → `STOCK_CONFLICT`. Les deux relevés validés automatiquement repassent À VALIDER, avec une trace. Un relevé validé par un humain n'est jamais rétrogradé.

## Stock retenu (`services/stock_registry.py::fuel_stock_retained`)

Relevés pris en compte : `VALIDATED`, conversion `VALID`, litres renseignés, sans dépassement de la capacité validée.

Pour chaque site, le service retient le plus récent et renvoie :

- la source, la date et l'heure, l'âge ;
- le niveau brut, l'unité, la méthode, le statut ;
- la capacité, le taux de remplissage, la preuve ;
- `unavailable_reason` quand aucun stock n'est retenu.

Sources éligibles : ENOC, LOLIFORCE, SCSS (jamais SNOWFLAKE ni CLOSING).

## Répertoire monté

`FUEL_IMPORT_ROOT` vaut par défaut `/mnt/Suivi_carburant_prod`. `docker-compose.yml` monte `${FUEL_IMPORT_HOST_PATH:-/mnt/Suivi_carburant_prod}` dans les services `web` et `celery`.

```
incoming/loliforce/   incoming/scss/   incoming/closing/   ← dépôt
processing/   accepted/   rejected/   archive/AAAA-MM/
```

Il n'y a pas de dossier `incoming/eydon` : voir la conclusion Eydon plus bas. L'historique refueling est un import ponctuel, par commande ou par upload.

Déroulé du traitement d'un fichier :

1. Calcul de l'empreinte. Un fichier identique déjà reçu est refusé comme doublon technique, sans réimport.
2. Écriture d'une copie d'archive immuable.
3. Chargement des lignes brutes.
4. Contrôles ligne à ligne.
5. Calcul du statut du lot.
6. Déplacement du fichier original, intact, vers `accepted/` ou `rejected/`, avec le rapport `<fichier>.rapport_lot_<id>.csv`.

Relance : elle est explicite et tracée, avec un motif obligatoire, et retraite la copie d'archive dans un nouveau lot lié.

Fréquence : la tâche Celery `fuel_tracking.process_fuel_import_incoming` tourne toutes les 15 min (minutes 7, 22, 37 et 52).

## Fichiers analysés (octobre 2026)

| Fichier | Contenu | Traitement |
|---|---|---|
| Template remontées Loliforce | 12 feuilles de zone, blocs « SEMAINE n » qui répètent l'en-tête, parfois **sans colonne Site ID**, plus une feuille « Compile » | Relevés LOLIFORCE. Colonnes lues par leur nom à chaque bloc. Bloc sans Site ID : site retrouvé par son nom, seulement si ce nom est unique (`SITE_RESOLVED_BY_NAME`). « Compile » est ignorée (valeurs recopiées, litres calculés hors référentiel). |
| REMONTE CARBURANT OCTOBRE 2026 (SCSS) | 1 feuille, en-tête en ligne 4, colonne CONTACT AGENT, relevés surtout en cm | Relevés SCSS (CONTACT AGENT → `source_user`) |
| Closing `.xlsb` | Feuille « Synthese conso fuel », en-tête en ligne 15 : forme, longueur, largeur, hauteur, diamètre, capacité (322 sites renseignés), GE et capteurs | Référentiel cuves **À VALIDER** uniquement. Aucune observation : les quantités calculées ne sont pas certifiées. GE et capteurs sont conservés en brut. |
| Suivi refueling (historique) | Feuille « Suivi refueling … » (ticket FMS, Ajout / retrait, source TOTAL / EYDON / Ponction, quantité demandée, niveaux avant / après, compteurs, cuve, statut, validateur) et feuille « Coef cuve » (533 lignes, sites multi-cuves) | Mouvements historiques : quantité réalisée à `NULL`, seule la quantité demandée est connue, aucun relevé créé. « Coef cuve » donne des propositions de cuves pour les sites sans cuve. |

Lecture des cellules de niveau :

| Saisie | Traitement |
|---|---|
| « 11 cm », « 16,5CM », « 100 L » | Valeur et unité lues |
| « Cuve 1=7 Cuve 2=11 », « C1: 8cm / C2: 24cm » | Cuves explicitement numérotées |
| « 7 et 11 », « 50/19 » | Plusieurs valeurs sans numéro de cuve : aucune attribution |
| « Pas de jauge », « illisible », « cadran » | `MISSING_LEVEL` |
| « 3 Bidons 20L » | `UNKNOWN_UNIT` |

Essai sur les fichiers réels, avec la base locale de 3 651 sites, aucune cuve validée et aucun périmètre configuré :

| Fichier | Résultat |
|---|---|
| Loliforce | 11 771 lignes, 10 075 relevés créés et tous à valider (cuves à valider, périmètre non configuré), 1 931 lignes rejetées (date absente, site inconnu, unité inconnue…), 400 doublons, 418 conflits de valeurs pour un même site à la même date |
| Closing | 322 cuves proposées, 40 sites inconnus |
| Refueling | 653 mouvements, 152 cuves proposées |

## ENOC — audit du connecteur existant

Connecteur existant, réutilisé tel quel :

- `sync_enoc_fuel_movements` lit `enoc_mongo_service.fetch_fuel_movements`, c'est-à-dire MongoDB `fuel_requests` + `fuel_operations` en lecture seule, toutes les 5 min (l'API REST ENOC est bloquée par filtrage IP) ;
- il alimente `FuelEnocMovement`, avec le payload brut conservé dans `raw_payload`.

Le registre lit `FuelEnocMovement` (`services/stock_enoc.py`) **sans aucune nouvelle connexion**. Il est appelé à la fin de la tâche ENOC existante.

| ENOC | Registre |
|---|---|
| `level_before` / `level_after` + unité | `fuel_stock_observation`, `source_event_id` = `<request_code>:before` / `:after` |
| opération / livraison | `fuel_movement`, `source_event_id` = `request_code` |
| photo / ticket / bon | `proof_reference` / `ticket_reference` / `delivery_note` |
| technicien | `source_user` |
| identifiant ENOC | `source_event_id` (unique, donc pas de doublon) |

Quantités :

- elles sont relues dans `raw_payload`. `FuelEnocMovement` met 0 par défaut, ce que le registre ne reprend pas : la valeur reste `NULL` ;
- une demande sans opération réalisée est marquée `DELIVERY_REQUESTED_ONLY` et n'est jamais comptée comme livrée ;
- une modification côté ENOC met à jour le registre et trace l'ancienne valeur.

## Eydon — conclusion

Constat dans le code : `fetch_fuel_movements` ne transmet **ni fournisseur, ni ticket, ni bon de livraison, ni photo**. Les colonnes `supplier` et `delivery_note_number` de `FuelEnocMovement` restent donc vides. Les documents Mongo complets sont toutefois conservés dans `raw_payload`.

Preuve à produire sur les données réelles :

```
docker compose exec web python manage.py sync_stock_registry_enoc --audit-eydon
```

La commande donne le taux de remplissage de chaque champ nécessaire (site, date et heure, quantité livrée, fournisseur, ticket, bon, photo, technicien, statut, commentaire), les types d'opération et les clés Mongo observées.

L'historique refueling montre que les livraisons Eydon sont aujourd'hui tracées par ticket FMS :

- 63 lignes avec la source EYDON ;
- 40 lignes avec le motif « Dépotage par camion EYDON ».

- **Si l'audit montre fournisseur, quantité livrée et preuve renseignés :** ENOC est la source unique. Le mapping est déjà en place (`DELIVERY_EYDON`, fournisseur EYDON) et aucun fichier supplémentaire n'est nécessaire.
- **Sinon (situation attendue d'après le code) :** il manque le fournisseur, la quantité **livrée**, distincte de la demande, et la preuve. Modèle minimal de fichier de repli proposé, **non développé sans validation métier** :
  `Site ID ; Date/heure livraison ; Quantité livrée (L) ; N° ticket FMS ; N° bon de livraison ; Preuve (lien photo) ; Technicien ; Statut (Livré/Annulé) ; Commentaire`.

## Commandes

```
python manage.py process_fuel_import_incoming [--init]
python manage.py import_fuel_stock_file --type LOLIFORCE|SCSS|CLOSING|HISTORICAL_REFUELING --file <chemin> [--dry-run]
python manage.py relaunch_fuel_import --batch <id> --reason "<motif>"
python manage.py sync_stock_registry_enoc [--audit-eydon]
python manage.py import_fuel_guard_scope --file perimetre.csv [--dry-run]   # site_id;provider;active_from;active_to;comment
```

## API (`/api/fuel-tracking/`)

| Méthode | Route | Droits |
|---|---|---|
| GET | `stock/registre/`, `stock/registre/<site_id>/` | lecture |
| POST | `stock/observations/<id>/valider/`, `stock/observations/<id>/rejeter/` | admin / manager, motif obligatoire |
| POST | `stock/mouvements/<id>/valider/`, `stock/mouvements/<id>/rejeter/` | admin / manager, motif obligatoire |
| GET | `stock/cuves/` | lecture |
| POST | `stock/cuves/` | admin / manager |
| PATCH | `stock/cuves/<id>/` | admin / manager, correction ou validation tracée, relevés du site reconvertis |
| GET | `stock/imports/` | lecture |
| POST | `stock/imports/` (upload) | admin / manager |
| GET | `stock/imports/<id>/lignes/` | lecture |
| POST | `stock/imports/<id>/relancer/` | admin / manager |
| GET | `stock/gardiennage/` | lecture |
| POST | `stock/gardiennage/` | admin / manager |
| POST | `stock/gardiennage/<id>/cloturer/` | admin / manager, aucune suppression |

L'ancien `GET stock/` (Snowflake + ENOC) est inchangé.

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `FUEL_IMPORT_ROOT` | `/mnt/Suivi_carburant_prod` | Chemin dans le conteneur |
| `FUEL_IMPORT_HOST_PATH` | `/mnt/Suivi_carburant_prod` | Chemin hôte monté par Docker Compose |
| `FUEL_CAPACITY_TOLERANCE_PCT` | `5` | Tolérance de dépassement de capacité, en % |

## Limites connues

- Les cuves issues du closing et de « Coef cuve » sont **à valider**. Tant qu'elles ne le sont pas, les relevés en cm restent sans litres. C'est voulu.
- Le périmètre de gardiennage est vide à l'installation. Tant qu'il n'est pas configuré, les relevés Loliforce / SCSS sont À VALIDER.
- Sites multi-cuves dans « Coef cuve » : la numérotation 1, 2 suit l'ordre du fichier et doit être confirmée (`TANK_ORDER_TO_CONFIRM`).
- La correspondance des types d'opération ENOC est heuristique (AJOUT, PONCTION, RETRAIT…). Les types non reconnus sont signalés (`MISSING_MOVEMENT_TYPE`) et à confirmer avec `--audit-eydon`.

---

# Itération 2 — Cuves : bibliothèque de modèles, affectation site → cuve, conversion calibrée, file « À traiter »

## Deux référentiels distincts
- `fuel_tank_model` : bibliothèque de MODÈLES issue du calculateur de Steve (`Senegal_Tank_DB_Calculateur.xlsx`, onglet
  « Tank DB », déposé dans `incoming/modeles_cuves/` ou importé avec le type `TANK_MODELS`). Statut COMPLET (écart plaque /
  géométrie ≤ 5 %), A_CONFIRMER (> 5 %), INCOMPLET (forme / cotes / profil non reconnus). Un modèle n'est jamais une cuve de site.
- `fuel_site_tank` (ancienne table `fuel_tank_master`, renommée, migration 0044) : cuve RÉELLEMENT présente sur un site.
  Statuts VALIDEE, PROPOSEE, A_CONFIRMER, INCOMPLETE, PLUSIEURS_CUVES_A_IDENTIFIER, REJETEE. Cotes en cm selon la famille du
  calculateur, profils de fond, capacité de plaque (+ source), modèle relié (PROPOSED / CONFIRMED / REJECTED), preuve,
  fichier / onglet / ligne source.

## Moteur (services/tank_geometry.py, version steve-v1)
Reprise exacte de la Notice du calculateur : 6 familles, 8 profils de fond, tables Ψ / Φ extraites de l'onglet « Profils »
(`fuel_tracking/data/tank_profiles_steve_v1.json`). Recoupé avec l'onglet « Barèmes » (8 cuves, 0 à 200 cm) : écart max
4·10⁻¹² L ; exemple du Calculateur (cuve #2, 27 cm) : 645,217 L géométrique, 592,084 L calibré. Volume calibré =
géométrique × k, k = plaque / géométrie pleine. Jamais de « litres par cm » générique.

## Affectation
- Closing : forme + cotes reconnues + capacité déclarée cohérente (≤ 5 %) → VALIDEE ; la capacité du Closing est recalculée
  depuis les cotes (fonds plats) : elle n'est jamais prise pour une capacité de plaque.
- Refueling (colonne CUVE, valeur ronde) : capacité de plaque appliquée si cohérente à 5 % avec la géométrie ; site sans cuve →
  cuve A_CONFIRMER (capacité connue, cotes à relever). « Coef cuve » : propositions (PROPOSEE / PLUSIEURS_CUVES_A_IDENTIFIER).
- Modèle proposé uniquement si famille + cotes correspondent sans ambiguïté (± 3 cm section, ± 5 cm longueur), jamais sur la
  capacité. Confirmer le modèle (UI « Corriger cuve ») applique sa géométrie et sa plaque, chaque champ tracé.
- `python manage.py evaluate_site_tanks [--dry-run]` réévalue les cuves non validées humainement et reconvertit les relevés.

## Conversion et stock retenu
Cuve VALIDEE requise. Plaque cohérente → volume CALIBRÉ retenu ; sans plaque (ou plaque incohérente) → volume géométrique
conservé comme valeur TECHNIQUE (statut GEOMETRIC_ONLY), retenu seulement par validation manuelle explicite. Chaque relevé
conserve niveau brut, unité, volumes géométrique / calibré / retenu, capacité, remplissage, règle, modèle, statut, codes
d'anomalie. cm + litres : les deux conservés, écart calculé (> max(100 L ; 10 %) → à traiter). Relevé > 7 jours à la
réception : signalé, conservé. Stock retenu : cuve VALIDEE, jour le plus récent, priorité `FUEL_STOCK_SOURCE_PRIORITY`
(TERRAIN_PREUVE > LOLIFORCE > SCSS > ENOC) ; jamais si un relevé du jour vise une cuve non référencée. Conflit Loliforce /
SCSS : `FUEL_GUARD_CONFLICT_PRIORITY` (vide = exception).

Statuts affichés : AUTO_VALIDE, VALIDE_MANUELLEMENT, A_TRAITER (toujours avec anomalie + règle + action), REJETE, NON_EXPLOITABLE.

## « À traiter — actions requises » (`GET stock/a-traiter/`)
Cuves à confirmer, cuves incomplètes, plusieurs cuves à identifier, relevés cm non convertibles, relevés dépassant la
capacité, conflits Loliforce / SCSS, gardiennage à configurer, mouvements ENOC non classifiés, mouvements sans preuve, sites
sans relevé récent — chaque ligne : site, date, source, anomalie, valeur brute, attendu, règle échouée, action, historique.

## ENOC
`fuel_enoc_type_mapping` : classification administrable et versionnée (RELEVE_STOCK, LIVRAISON_CONFIRMEE, DEMANDE_LIVRAISON,
AJOUT, TRANSFERT_ENTRANT/SORTANT, RETRAIT, VOL, VIDANGE, CARTE_TOTAL, INCONNU). Type non configuré → INCONNU (file
d'exceptions, valeur brute visible), jamais un mouvement réel. Demande sans opération → DEMANDE_LIVRAISON, jamais retenue.
CARTE_TOTAL → contrôle, jamais retenue. LIVRAISON_CONFIRMEE retenue seulement avec site, date, quantité et preuve / référence.
`python manage.py enoc_types` (dictionnaire réel + suggestion non appliquée) ; `--set VALEUR CATEGORIE --reason "…"`.

## Profil énergétique (colonnes séparées de la TYPOLOGIE, inchangée)
Environnement (Gestion des sites, type installé), Raccordement (Snowflake GRID_SUPPLY_MODIFIED), GE (Snowflake DG_COUNT, sinon
périmètre fuel), Solaire (Gestion des sites « non encore solarisé »). Jamais déduit du code de typologie ; « Inconnu » sinon.
Audit : `python manage.py audit_site_energy_fields` ou `GET stock/audit-profil-energetique/`.

## Nouveaux endpoints
`GET stock/a-traiter/[?categorie=]`, `GET/POST stock/enoc/types/`, `GET stock/modeles-cuves/`,
`GET stock/audit-profil-energetique/` — tous additifs.
