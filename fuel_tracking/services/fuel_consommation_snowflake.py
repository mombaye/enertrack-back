# fuel_tracking/services/fuel_consommation_snowflake.py
"""
Consommation carburant mensuelle par site depuis Snowflake.

Depuis le 2026-08 (spec "Correction des 3 colonnes carburant") : la Conso
mesurée vient de DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT (vue quotidienne
qui joint déjà site/GE et nettoie pics isolés, valeurs négatives,
dépassements de capacité) au lieu de DB_GFMS_PROD.GOLD.CONSUMPTION_FUEL
(quasi vide, ~5 sites couverts sur 3253 — abandonnée). ATTENTION : la spec
demandait un filtre QUALITY_STATUS = 'VALID', mais vérifié en base le
2026-08 : la colonne ne prend QUE les valeurs 'OK' / 'NO_VALID_LEVEL' /
'LOW_QUALITY' — jamais 'VALID'. Filtrer sur 'VALID' littéral aurait rendu
la colonne vide à 100%. Corrigé ici pour filtrer sur 'OK'.

La requête part de la DIMENSION SITE (tous les sites Sénégal, SITE_FILTERED,
DB_GFMS_PROD.GOLD) et fait un LEFT JOIN vers les tables de fait — et non
l'inverse. Avec un JOIN direct depuis les tables de fait (version
précédente), un site sans donnée de conso ce mois-là disparaissait
complètement de son propre nom/typologie dès qu'il n'apparaissait dans
aucune table de fait en premier : constaté au 07/08, seuls 5 sites sur 3253
avaient un nom de site correctement rempli. Partir de SITE_FILTERED (tous
les sites) et faire les LEFT JOIN garantit un nom de site pour CHAQUE site
du périmètre, que sa conso soit renseignée ou non.

Tables sources :
  - SITE_FILTERED (DB_GFMS_PROD.GOLD ; DATA_ID, SITE_ID, SITE_NAME, COUNTRY,
    TYPOLOGY, SITE_TYPE, DG_COUNT, POWER_SUPPLY, ...) : dimension site, base
    de la requête. Contient quelques doublons de DATA_ID — dédupliqué ici
    via QUALIFY ROW_NUMBER()=1.
  - VW_FUEL_REPORT (DB_GFMS_ANALYTICS_DEV.GOLD ; DATA_ID, COUNTRY, SITE_ID,
    DATE, QUALITY_STATUS, VALID_POINT_COUNT, RAW_POINT_COUNT,
    ISOLATED_SPIKE_COUNT, OVER_CAPACITY_POINT_COUNT, DROP_DETECTED,
    ESTIMATED_DROP_VOLUME_L, REFILL_DETECTED, ESTIMATED_REFILL_VOLUME_L) :
    conso mesurée quotidienne (baisse de niveau détectée = consommation
    réelle), déjà nettoyée des pics isolés / valeurs négatives / dépassements
    de capacité. Colonnes qualité conservées pour audit dans l'API.
  - GE_PROD_KWH (DB_GFMS_PROD.GOLD ; ID, DATE, GE_PROD_KWH) : production
    électrique du groupe électrogène/jour — sert au calcul de la conso
    spécifique (L/kWh), au ratio pondéré mensuel (SUM/SUM), pas une moyenne
    de ratios journaliers.
  - GFMS_FUEL_SENSOR_MONITORING_DATA (DB_GFMS_PROD.GOLD) : état du capteur,
    conservé tel quel pour la colonne "Statut capteur" (distinct de
    QUALITY_STATUS qui est un indicateur de qualité par jour, pas un statut
    de capteur global).
  - TANK_LEVEL_AVG (DB_GFMS_PROD.GOLD) : niveau moyen de cuve/jour — sert
    UNIQUEMENT à l'estimation par delta (conso_estimee_snowflake_l, calculée
    dans sync_fuel_consommation.py), distincte de la conso MESURÉE via
    VW_FUEL_REPORT ci-dessus. Les 2 indicateurs restent séparés, jamais
    fusionnés (cf. spec 2026-08 : "ne pas remplacer une consommation
    mesurée Snowflake par une quantité ENOC : ce sont deux indicateurs
    distincts" — même principe appliqué ici entre les 2 sources Snowflake).

  VW_FUEL_REPORT est sur DB_GFMS_ANALYTICS_DEV (pas PROD) — c'est la base
  indiquée dans la spec, vérifiée accessible avec les mêmes identifiants.
  Le ID/DATA_ID de ces tables est un identifiant numérique interne, pas le
  site_id texte (ex: "SN0876") ; la jointure passe par SITE_FILTERED.
"""
import calendar as _cal
import logging
from datetime import date
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

FUEL_DATABASE = "DB_GFMS_PROD"
FUEL_SCHEMA = "GOLD"
FUEL_REPORT_DATABASE = "DB_GFMS_ANALYTICS_DEV"  # VW_FUEL_REPORT — base distincte de FUEL_DATABASE

# Périmètre actuel du module fuel-tracking : Sénégal uniquement (voir
# "Commande FUEL ESCO SENEGAL", historique du module). Snowflake couvre
# plusieurs pays (Burkina Faso, Tchad, Cameroun, Côte d'Ivoire...) — on ne
# récupère que le Sénégal pour rester dans le périmètre attendu.
#
# Tous les sites Sénégal sont récupérés (avec et sans GE) — le filtre "avec
# GE uniquement" (site ayant au moins un groupe électrogène, seuls capables
# de consommer du fuel) se fait côté app via le champ has_genset, pas ici :
# vérifié au 07/08, sur 3322 sites Sénégal, ~2949 sont Solar/Grid seuls
# (DG_COUNT=0).
COUNTRY_SCOPE = "Senegal"


def _connect():
    import snowflake.connector

    kwargs = dict(
        account=settings.SNOWFLAKE_ACCOUNT,
        user=settings.SNOWFLAKE_USER,
        warehouse=settings.SNOWFLAKE_WAREHOUSE,
        role=settings.SNOWFLAKE_ROLE,
        database=FUEL_DATABASE,
        schema=FUEL_SCHEMA,
    )
    if settings.SNOWFLAKE_PRIVATE_KEY_PATH:
        kwargs["authenticator"] = "SNOWFLAKE_JWT"
        kwargs["private_key_file"] = settings.SNOWFLAKE_PRIVATE_KEY_PATH
        if settings.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE:
            kwargs["private_key_file_pwd"] = settings.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE
    else:
        kwargs["password"] = settings.SNOWFLAKE_PASSWORD
    return snowflake.connector.connect(**kwargs)


def fetch_monthly_consumption(year: int, month: int) -> dict[str, dict]:
    """
    Retourne {site_id: {site_name, country, typology, site_type, dg_count,
    power_supply, conso_snowflake_l, nb_jours_data, conso_specifique_moy_l_kwh,
    sensor_status, quality_status, valid_point_count, raw_point_count,
    isolated_spike_count, over_capacity_point_count, refill_detected,
    estimated_refill_volume_l}} — une entrée pour CHAQUE site du périmètre
    (Sénégal), même sans donnée de conso ce mois-là.

    conso_specifique_moy_l_kwh est le ratio pondéré mensuel SUM(conso)/
    SUM(production GE), PAS une moyenne de ratios journaliers (cf. spec
    2026-08) — calculé ici en Python à partir des 2 sommes déjà agrégées
    par la requête, plutôt qu'en SQL, pour éviter toute division par une
    valeur nulle côté Snowflake.
    """
    d_start = date(year, month, 1)
    d_end = date(year, month, _cal.monthrange(year, month)[1])

    conn = _connect()
    try:
        cursor = conn.cursor()
        db_schema = f"{FUEL_DATABASE}.{FUEL_SCHEMA}"
        report_schema = f"{FUEL_REPORT_DATABASE}.{FUEL_SCHEMA}"

        cursor.execute(f"""
            WITH site_dim AS (
                SELECT DATA_ID, SITE_ID, SITE_NAME, COUNTRY, TYPOLOGY, SITE_TYPE, DG_COUNT, POWER_SUPPLY
                FROM (
                    SELECT DATA_ID, SITE_ID, SITE_NAME, COUNTRY, TYPOLOGY, SITE_TYPE, DG_COUNT, POWER_SUPPLY,
                           ROW_NUMBER() OVER (PARTITION BY DATA_ID ORDER BY SITE_ID) AS rn
                    FROM {db_schema}.SITE_FILTERED
                    WHERE COUNTRY = %(country)s
                )
                WHERE rn = 1
            ),
            -- Conso MESURÉE — VW_FUEL_REPORT (base ANALYTICS_DEV, indiquée par
            -- la spec). QUALITY_STATUS filtré sur 'OK' (valeur réelle vérifiée
            -- en base — la spec mentionnait 'VALID', qui n'existe pas dans la
            -- colonne, ça aurait rendu la conso systematiquement vide).
            conso AS (
                SELECT
                    DATA_ID,
                    SUM(CASE
                        WHEN QUALITY_STATUS = 'OK' AND VALID_POINT_COUNT >= 2 AND DROP_DETECTED = TRUE
                        THEN ESTIMATED_DROP_VOLUME_L
                    END) AS total_l,
                    COUNT(CASE
                        WHEN QUALITY_STATUS = 'OK' AND VALID_POINT_COUNT >= 2 AND DROP_DETECTED = TRUE
                        THEN 1
                    END) AS nb_jours,
                    SUM(RAW_POINT_COUNT) AS raw_point_count,
                    SUM(VALID_POINT_COUNT) AS valid_point_count,
                    SUM(ISOLATED_SPIKE_COUNT) AS isolated_spike_count,
                    SUM(OVER_CAPACITY_POINT_COUNT) AS over_capacity_point_count,
                    MAX(IFF(REFILL_DETECTED, 1, 0)) AS refill_detected,
                    SUM(ESTIMATED_REFILL_VOLUME_L) AS estimated_refill_volume_l,
                    MAX_BY(QUALITY_STATUS, DATE) AS quality_status
                FROM {report_schema}.VW_FUEL_REPORT
                WHERE COUNTRY = %(country)s AND DATE >= %(d_start)s AND DATE <= %(d_end)s
                GROUP BY DATA_ID
            ),
            -- Production GE — pour la conso spécifique (ratio pondéré mensuel).
            ge_prod AS (
                SELECT ID, SUM(GE_PROD_KWH) AS ge_prod_kwh
                FROM {db_schema}.GE_PROD_KWH
                WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND GE_PROD_KWH IS NOT NULL
                GROUP BY ID
                HAVING SUM(GE_PROD_KWH) > 0
            ),
            capteur AS (
                SELECT TRY_CAST(ID AS NUMBER) AS data_id, FUEL_SENSOR_MONITORING_STATUS AS status
                FROM {db_schema}.GFMS_FUEL_SENSOR_MONITORING_DATA
                WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s
                QUALIFY ROW_NUMBER() OVER (PARTITION BY TRY_CAST(ID AS NUMBER) ORDER BY DATE DESC) = 1
            ),
            niveau AS (
                SELECT
                    ID,
                    MIN_BY(TANK_LEVEL_AVG, DATE) AS niveau_debut,
                    MAX_BY(TANK_LEVEL_AVG, DATE) AS niveau_fin,
                    COUNT(DATE) AS nb_releves
                FROM {db_schema}.TANK_LEVEL_AVG
                WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s
                  AND TANK_LEVEL_AVG IS NOT NULL
                  -- Plafond de plausibilité : aucune cuve de secours ne dépasse
                  -- 50 000 L. Constaté au 07/08 : certains sites ont des
                  -- relevés clairement corrompus (ex: DKR_2853 oscillant entre
                  -- 346 L et 20 000 000 L d'un jour à l'autre) — sans ce filtre,
                  -- MIN_BY/MAX_BY ci-dessous produisaient des "estimations" de
                  -- plusieurs millions de litres.
                  AND TANK_LEVEL_AVG > 0 AND TANK_LEVEL_AVG <= 50000
                GROUP BY ID
                HAVING COUNT(DATE) >= 2
            )
            SELECT
                s.SITE_ID, s.SITE_NAME, s.COUNTRY, s.TYPOLOGY, s.SITE_TYPE, s.DG_COUNT, s.POWER_SUPPLY,
                c.total_l, c.nb_jours, ca.status,
                n.niveau_debut, n.niveau_fin, n.nb_releves,
                gp.ge_prod_kwh,
                c.raw_point_count, c.valid_point_count, c.isolated_spike_count,
                c.over_capacity_point_count, c.refill_detected, c.estimated_refill_volume_l, c.quality_status
            FROM site_dim s
            LEFT JOIN conso c ON c.DATA_ID = s.DATA_ID
            LEFT JOIN ge_prod gp ON gp.ID = s.DATA_ID
            LEFT JOIN capteur ca ON ca.data_id = s.DATA_ID
            LEFT JOIN niveau n ON n.ID = s.DATA_ID
        """, {"country": COUNTRY_SCOPE, "d_start": d_start, "d_end": d_end})

        result: dict[str, dict] = {}
        for (site_id, site_name, country, typology, site_type, dg_count, power_supply,
             total_l, nb_jours, status, niveau_debut, niveau_fin, nb_releves, ge_prod_kwh,
             raw_point_count, valid_point_count, isolated_spike_count,
             over_capacity_point_count, refill_detected, estimated_refill_volume_l, quality_status) in cursor.fetchall():
            try:
                has_genset = float(dg_count) > 0 if dg_count is not None else False
            except (TypeError, ValueError):
                has_genset = False

            conso_l = Decimal(str(total_l)) if total_l is not None else None
            # Ratio pondéré mensuel — pas de moyenne de ratios journaliers.
            conso_specifique = None
            if conso_l is not None and ge_prod_kwh is not None and ge_prod_kwh > 0:
                conso_specifique = (conso_l / Decimal(str(ge_prod_kwh))).quantize(Decimal("0.001"))

            result[site_id] = {
                "site_name": site_name,
                "country": country,
                "typology": typology,
                "site_type": site_type,
                "dg_count": dg_count,
                "power_supply": power_supply,
                "has_genset": has_genset,
                "conso_snowflake_l": conso_l,
                "nb_jours_data": int(nb_jours or 0),
                "conso_specifique_moy_l_kwh": conso_specifique,
                "ge_prod_kwh": Decimal(str(ge_prod_kwh)) if ge_prod_kwh is not None else None,
                "sensor_status": status,
                "quality_status": quality_status,
                "raw_point_count": int(raw_point_count) if raw_point_count is not None else None,
                "valid_point_count": int(valid_point_count) if valid_point_count is not None else None,
                "isolated_spike_count": int(isolated_spike_count) if isolated_spike_count is not None else None,
                "over_capacity_point_count": int(over_capacity_point_count) if over_capacity_point_count is not None else None,
                "refill_detected": bool(refill_detected) if refill_detected is not None else False,
                "estimated_refill_volume_l": Decimal(str(estimated_refill_volume_l)) if estimated_refill_volume_l is not None else None,
                "niveau_debut": Decimal(str(niveau_debut)) if niveau_debut is not None else None,
                "niveau_fin": Decimal(str(niveau_fin)) if niveau_fin is not None else None,
                "niveau_nb_releves": int(nb_releves) if nb_releves is not None else None,
            }
        return result
    finally:
        conn.close()
