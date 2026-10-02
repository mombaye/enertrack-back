# Suivi Carburant — calcul CPH et consommation estimée

Module : `fuel_tracking/` (backend) · onglets « Suivis Consommations » et « Contrôle CPH » (frontend).
Version de règle : `cph_engine.RULE_VERSION`.

## 1. Chaîne de données

```
Snowflake (lecture seule)  ──sync_fuel_daily_facts──▶  FuelSiteDailyFacts (site × jour, valeurs brutes, NULL ≠ 0)
Abaque CPH (xlsx)          ──import abaque──────────▶  CphCurve + CphInventoryMapping (+ correspondance automatique)
Base GE (type GE, kVA)     ──import_base_ge─────────▶  FuelConsommationMonthly.type_ge_fichier / pge_kva_fichier
Relevés de stock (xlsx/csv)──import observations────▶  FuelObservation
                                         │
                       cph_engine.compute_site_period (dates exactes, site × jour)
                                         │
              API /api/fuel-tracking/cph/ (cache versionné) ──▶ Suivis Consommations / Contrôle CPH / exports
```

Sources Snowflake : `GENSET_REPORT` (DSE, Day DG On, production GE), `GFMS_DATA_TRACKER_NC` (compteur),
`RECTIFIER_EFFICIENCY_STATUS` (P_DC, rendement), `AC_METER` (load AC indoor), `VW_FUEL_REPORT` (conso mesurée),
`SITE_ESCO_CURRENT` (inventaire, DG_COUNT). Aucune écriture côté Snowflake.

## 2. Règles de calcul (jour par jour, puis agrégation sur la période exacte)

| Étape | Règle |
|---|---|
| Runtime GE (h) | Priorité stricte DSE > redresseur (off-grid uniquement) > Day DG On > compteur terrain. Source utilisable si disponible ≥ 50 % des jours. DSE = 0 valide ; absent = NULL (jamais `COALESCE(…, 0)`). Bornes 0-24 h. |
| Puissance GE (kW) | Chaîne de repli tracée jour par jour (`power_trace`) : **1.** `DIRECT_DSE_PRODUCTION` = `DG_PRODUCTION_KWH / runtime` (toute configuration) ; **2.** `PDC_REDRESSEUR` (outdoor) = `P_DC pendant GE / η` (0 < η ≤ 1) ; **3.** `INDOOR_PDC_LOAD_AC` (indoor) = `P_DC pendant GE / η + load AC historique`. Chaque méthode est tentée, retenue ou rejetée avec un code ; une méthode au-delà de `1,05 × kVA × 0,8` est rejetée et la suivante tentée ; NULL seulement si toutes échouent. La batterie n'est jamais ajoutée (`BATTERIE_NON_INTEGREE`). `ACT_ACTIVE_POWER_AVG` n'est jamais une puissance GE directe. |
| Load AC historique | Médiane de `max(0, P_AC − P_DC entrée)` sur les jours réseau sans GE (60 j, ≥ 5 j), `P_AC = ACT_ACTIVE_POWER_AVG ÷ 1000`. Statuts : `LOAD_AC_QUALIFIE`, `LOAD_AC_MESURE_ZERO` (vrai zéro mesuré), `LOAD_AC_INDISPONIBLE`, `LOAD_AC_INCOHERENT` (P_AC < P_DC entrée : périmètre du compteur ou unité → NULL, jamais 0 par écrêtage), `LOAD_AC_UNITE_SUSPECTE` (valeur brute < 50 « W » pour ≥ 0,3 kW d'entrée DC → NULL). |
| Charge GE | `P_GE / (kVA × 0,8)` (kVA de la courbe appliquée). Refus si `P_GE > 1,05 × kVA × 0,8`. < 50 % = extrapolation signalée. |
| CPH (L/h) | `a × charge² + b × charge + c` (courbe de l'abaque PRP 50 Hz). CPH ≤ 0 refusé. |
| Conso estimée (L) | `runtime × CPH`. Complète si tous les jours sont calculés, sinon « partielle » (jours indiqués). |
| Conso mesurée vue (L) | `VW_FUEL_REPORT` : `QUALITY_STATUS='OK' AND VALID_POINT_COUNT>=2` ; baisse détectée → volume ; ni baisse ni remplissage → 0 ; sinon NULL. |
| Écart | `mesurée − estimée`, % = écart / estimée × 100, sur les jours où les deux existent. Aucun écart si aucun jour n'a de CPH (un GE à l'arrêt toute la période reste un vrai 0). Seuils : OK ≤ max(100 L ; 10 %), à justifier ≤ max(200 L ; 20 %). |
| Conso spécifique | `conso / Σ(P_GE × runtime)` (L/kWh), estimée et mesurée. Hors [0,20 ; 0,50] = alerte, jamais bloquant. |

Statuts séparés : `statut_cph` = `CPH_CALCULE` / `CPH_PARTIEL` / `CPH_NON_CALCULE` ; `statut_rapprochement_calcul` =
`RAPPROCHEMENT_CALCULE` / `RAPPROCHEMENT_NON_CALCULE_STOCK_ABSENT` / `RAPPROCHEMENT_NON_CALCULE_MOUVEMENTS_ABSENTS` /
`RAPPROCHEMENT_NON_CALCULE_CPH_INCOMPLET`. Un CPH calculé sans relevé de stock reste `CPH_CALCULE`.
Jours postérieurs à la dernière donnée Snowflake : `PERIODE_INCOMPLETE` (affiché « PÉRIODE INCOMPLÈTE »).

Codes de rejet puissance : `P_DSE_INDISPONIBLE`, `P_DSE_INCOHERENTE`, `PDC_GE_INDISPONIBLE`, `PDC_NEGATIF`,
`RENDEMENT_REDRESSEUR_INVALIDE`, `LOAD_AC_*`, `CONFIGURATION_INCONNUE`, `PUISSANCE_HORS_LIMITE`.

Motifs d'absence de CPH (par jour et par site) : `RUNTIME_INDISPONIBLE`, `RUNTIME_NON_QUALIFIE`, `PUISSANCE_INDISPONIBLE`,
`PUISSANCE_HORS_LIMITE`, `PUISSANCE_NOMINALE_ABSENTE`, `RENDEMENT_REDRESSEUR_INVALIDE`, `COURBE_CPH_MANQUANTE`,
`MAPPING_GE_A_VALIDER`, `MODELE_GE_AMBIGU`, `SITE_MULTI_GE`, `TYPE_GE_ABSENT`.

## 3. Correspondance plaque GE → courbe (`services/cph_matching.py`)

Deux statuts distincts :
- `mapping_status` : `AUTO_VALIDE_COMPATIBLE`, `VALIDE_MANUELLEMENT`, `A_VALIDER`, `MODELE_AMBIGU`, `COURBE_CPH_MANQUANTE`,
  `REJETE` (+ au niveau site `TYPE_GE_ABSENT`, `SITE_MULTI_GE`) ;
- `curve_source_status` : `VALIDE_CONSTRUCTEUR`, `HISTORIQUE_A_VALIDER`, `ARCHIVE`, `DISTRIBUTEUR` — jamais modifié par un mapping.

Score (candidat unique de l'abaque) : modèle normalisé identique 60 (variante de suffixe ≤ 2 lettres : 40) + marque compatible 20
+ puissance compatible 20 (`|kVA inventaire − kVA courbe| / kVA courbe ≤ 15 %`). Auto-validation si score ≥ 70 et aucune
contradiction (modèle, marque, puissance, 60 Hz, courbes comparables aux consommations différentes). Courbes en double
(mêmes points 50/75/100 %) ignorées. Validation / correction / rejet manuels (admin, manager) prioritaires ; un rejet n'est
jamais réactivé automatiquement. Historique complet : `CphMappingHistory` (admin Django et référentiel).

## 4. Configuration (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `FUEL_CPH_COUNTRIES` | `Senegal` | Pays synchronisés |
| `FUEL_ENOC_DELIVERIES_CONNECTED` | `0` | Tant que 0, rapprochement stock = `LIVRAISONS_ENOC_A_CONTROLER` |
| `FUEL_CPH_P_DC_TO_KW_DIVISOR` / `FUEL_CPH_EFFICIENCY_TO_RATIO_DIVISOR` | `1000` / `100` | Unités Snowflake (W → kW, % → ratio) |
| `FUEL_CPH_AC_POWER_TO_KW_DIVISOR` | `1000` | Unité `ACT_ACTIVE_POWER_AVG` (W → kW) |
| `FUEL_CPH_MATCH_POWER_TOLERANCE` | `0.15` | Tolérance puissance du mapping automatique |
| `FUEL_CPH_NOMINAL_POWER_FACTOR` | `0.8` | Puissance active nominale = kVA × facteur |
| `FUEL_CPH_SFC_MIN_L_KWH` / `FUEL_CPH_SFC_MAX_L_KWH` | `0.20` / `0.50` | Plage de plausibilité L/kWh |
| `FUEL_CPH_STALE_AFTER_DAYS` | `3` | Données Snowflake considérées périmées au-delà |
| `FUEL_CPH_MIN_COVERAGE` | `0.5` | Couverture CPH minimale (7 derniers jours) avant alerte |
| `FUEL_CPH_MAX_SFC_ALERT_SHARE` | `0.2` | Part max de sites en alerte L/kWh avant alerte globale |

## 5. Exploitation

| Quand | Tâche / commande |
|---|---|
| Toutes les heures (hh:17) | `sync_fuel_daily_facts_recent` : J-3 → aujourd'hui |
| Chaque nuit (02:41) | `resync_fuel_daily_facts_nightly` : J-35 → aujourd'hui (données tardives, conso mesurée) |
| Chaque jour (06:53) | `check_cph_health_daily` : anomalies journalisées en `ERROR` (`[fuel_cph][SANTE]`) |
| À la demande | `python manage.py sync_fuel_daily_facts --start AAAA-MM-JJ --end AAAA-MM-JJ [--dry-run]` |
| À la demande | `python manage.py check_cph_health` (code retour 1 si anomalie critique) · `GET /api/fuel-tracking/cph/health/` |
| Recette | `python manage.py cph_recette --start 2026-09-01 --end 2026-09-30` : compteurs par étape, 6 couvertures, synthèse des blocages, 10 exemples |
| Audit d'un site | `python manage.py diagnose_cph_site DBL_0068 --start 2026-09-01 --end 2026-09-30 [--snowflake]` : jointures, runtime, méthodes de puissance jour par jour, valeurs Active Power Avg lues (relecture Snowflake en lecture seule avec `--snowflake`) |
| À la demande | `python manage.py import_cph_abaque --file ABAQUE_CPH_GE_PRP_50HZ.xlsx` (ou bouton dans Contrôle CPH) |

Les deux synchros partagent un verrou (cache) et ne se chevauchent pas. Le calcul de période est mis en cache 10 min,
clé = paramètres + empreinte des données (synchro, abaque, mappings, relevés, réglages) : jamais servi périmé. Redis
indisponible → calcul direct.

## 6. Déploiement et retour arrière

- Déploiement : `migrate` au démarrage du conteneur applique `0038` → `0042` (schéma et données séparés, contrainte
  PostgreSQL). `0039` / `0041` exécutent la correspondance automatique et créent l'historique initial.
- Retour arrière : `python manage.py migrate fuel_tracking 0037` (les colonnes ajoutées sont supprimées ; les courbes
  posées par l'automatique sont retirées, les validations humaines conservées), puis redéployer la version précédente.
  Vérifié sur PostgreSQL 16 : 0041 → 0037 → 0041 redonne exactement le même résultat.
- Après un premier déploiement : la resynchro nocturne remplit la conso mesurée des 35 derniers jours ; pour un mois
  plus ancien, lancer `sync_fuel_daily_facts --start … --end …`.

## 7. Couvertures (API `synthesis.couvertures`)

Six KPI distincts, chacun avec numérateur, dénominateur et définition — jamais un « taux de couverture » unique :
conso mesurée (÷ sites GE), runtime qualifié (÷ sites GE), mapping GE → courbe (÷ sites GE avec type connu),
puissance qualifiée (÷ sites avec runtime qualifié et GE en marche), CPH calculé (÷ sites GE), rapprochement stock
(÷ sites avec CPH calculé). `synthesis.diagnostic_blocages` donne par motif : sites, jours, runtime concerné, conso
mesurée disponible et volume potentiel indicatif (runtime bloqué × CPH moyen du site sur ses jours calculés).
