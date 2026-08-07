# fuel_tracking/services/fuel_consommation_snowflake.py
"""
Consommation carburant mensuelle par site depuis Snowflake DB_GFMS_PROD.GOLD
(base de production distincte de DB_GFMS_ANALYTICS_PROD utilisée par
financial/conso_service.py pour l'électricité — voir exploration du 07/08).

La requête part de la DIMENSION SITE (tous les sites Sénégal, SITE_FILTERED)
et fait un LEFT JOIN vers les 3 tables de fait — et non l'inverse. Avec un
JOIN direct depuis les tables de fait (version précédente), un site sans
donnée de conso ce mois-là disparaissait complètement de son propre nom/
typologie dès qu'il n'apparaissait dans aucune des 3 tables de fait en
premier : constaté au 07/08, seuls 5 sites sur 3253 avaient un nom de site
correctement rempli, alors que la table de statut capteur couvrait ~382
sites — le nom de site n'était rempli que pour les 5 sites qui avaient une
ligne CONSUMPTION_FUEL, jamais pour les sites vus uniquement via les 2
autres tables. Partir de SITE_FILTERED (tous les sites) et faire les LEFT
JOIN garantit un nom de site pour CHAQUE site du périmètre, que sa conso
soit renseignée ou non.

Tables sources :
  - SITE_FILTERED (DATA_ID, SITE_ID, SITE_NAME, COUNTRY, TYPOLOGY, SITE_TYPE,
    DG_COUNT, POWER_SUPPLY, ...) : dimension site, base de la requête.
    Contient quelques doublons de DATA_ID (8697 lignes pour 8403 DATA_ID
    distincts au 07/08) — dédupliqué ici via QUALIFY ROW_NUMBER()=1.
  - CONSUMPTION_FUEL (ID, DATE, CONSUMPTION_FUEL) : conso carburant/jour.
  - AVGSPECIFICFUELCONSO_L_KWH (ID, DATE, valeur) : conso spécifique L/kWh/jour.
  - GFMS_FUEL_SENSOR_MONITORING_DATA (DATE, ID, FUEL_SENSOR_MONITORING_STATUS) :
    état du capteur — couvre beaucoup plus de sites que les 2 tables de conso
    ci-dessus (un site "MONITORED" n'a pas forcément assez de points de
    mesure valides pour produire une conso mensuelle calculée).
  Le ID de ces 3 tables est un identifiant numérique interne (DATA_ID), pas
  le site_id texte (ex: "SN0876") ; la jointure passe par SITE_FILTERED.
"""
import calendar as _cal
import logging
from datetime import date
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

FUEL_DATABASE = "DB_GFMS_PROD"
FUEL_SCHEMA = "GOLD"

# Périmètre actuel du module fuel-tracking : Sénégal uniquement (voir
# "Commande FUEL ESCO SENEGAL", historique du module). Snowflake couvre
# plusieurs pays (Burkina Faso, Tchad, Cameroun, Côte d'Ivoire...) — on ne
# récupère que le Sénégal pour rester dans le périmètre attendu.
#
# On filtre aussi sur DG_COUNT > 0 (site avec au moins un groupe électrogène) :
# vérifié au 07/08, sur 3322 sites Sénégal, ~2949 sont Solar/Grid seuls
# (DG_COUNT=0, aucune conso fuel possible par construction) — ne garder que
# les ~373 sites avec GE évite un tableau pollué de sites structurellement
# sans conso.
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
    sensor_status}} — une entrée pour CHAQUE site du périmètre (Sénégal),
    même sans donnée de conso ce mois-là.
    """
    d_start = date(year, month, 1)
    d_end = date(year, month, _cal.monthrange(year, month)[1])

    conn = _connect()
    try:
        cursor = conn.cursor()
        db_schema = f"{FUEL_DATABASE}.{FUEL_SCHEMA}"

        cursor.execute(f"""
            WITH site_dim AS (
                SELECT DATA_ID, SITE_ID, SITE_NAME, COUNTRY, TYPOLOGY, SITE_TYPE, DG_COUNT, POWER_SUPPLY
                FROM (
                    SELECT DATA_ID, SITE_ID, SITE_NAME, COUNTRY, TYPOLOGY, SITE_TYPE, DG_COUNT, POWER_SUPPLY,
                           ROW_NUMBER() OVER (PARTITION BY DATA_ID ORDER BY SITE_ID) AS rn
                    FROM {db_schema}.SITE_FILTERED
                    WHERE COUNTRY = %(country)s
                      AND TRY_CAST(DG_COUNT AS NUMBER) > 0
                )
                WHERE rn = 1
            ),
            conso AS (
                SELECT ID, SUM(CONSUMPTION_FUEL) AS total_l, COUNT(DATE) AS nb_jours
                FROM {db_schema}.CONSUMPTION_FUEL
                WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND CONSUMPTION_FUEL IS NOT NULL
                GROUP BY ID
            ),
            specifique AS (
                SELECT ID, AVG(AVGSPECIFICFUELCONSO_L_KWH) AS avg_val
                FROM {db_schema}.AVGSPECIFICFUELCONSO_L_KWH
                WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND AVGSPECIFICFUELCONSO_L_KWH IS NOT NULL
                GROUP BY ID
            ),
            capteur AS (
                SELECT TRY_CAST(ID AS NUMBER) AS data_id, FUEL_SENSOR_MONITORING_STATUS AS status
                FROM {db_schema}.GFMS_FUEL_SENSOR_MONITORING_DATA
                WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s
                QUALIFY ROW_NUMBER() OVER (PARTITION BY TRY_CAST(ID AS NUMBER) ORDER BY DATE DESC) = 1
            )
            SELECT
                s.SITE_ID, s.SITE_NAME, s.COUNTRY, s.TYPOLOGY, s.SITE_TYPE, s.DG_COUNT, s.POWER_SUPPLY,
                c.total_l, c.nb_jours, sp.avg_val, ca.status
            FROM site_dim s
            LEFT JOIN conso c ON c.ID = s.DATA_ID
            LEFT JOIN specifique sp ON sp.ID = s.DATA_ID
            LEFT JOIN capteur ca ON ca.data_id = s.DATA_ID
        """, {"country": COUNTRY_SCOPE, "d_start": d_start, "d_end": d_end})

        result: dict[str, dict] = {}
        for site_id, site_name, country, typology, site_type, dg_count, power_supply, total_l, nb_jours, avg_val, status in cursor.fetchall():
            result[site_id] = {
                "site_name": site_name,
                "country": country,
                "typology": typology,
                "site_type": site_type,
                "dg_count": dg_count,
                "power_supply": power_supply,
                "conso_snowflake_l": Decimal(str(total_l)) if total_l is not None else None,
                "nb_jours_data": int(nb_jours or 0),
                "conso_specifique_moy_l_kwh": Decimal(str(avg_val)) if avg_val is not None else None,
                "sensor_status": status,
            }
        return result
    finally:
        conn.close()
