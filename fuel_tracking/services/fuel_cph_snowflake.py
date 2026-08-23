# fuel_tracking/services/fuel_cph_snowflake.py
"""
Estimation journalière de consommation carburant GE par télémétrie (CPH),
pour les sites Sénégal sans capteur de cuve fiable — pipeline validé sur 90
jours / 3191 sites / 262719 journées-site (spec "CPH_SIMULATION_DEPLOYMENT_SPEC"
fournie par l'utilisateur, 2026-08).

Ce module ne fait QUE l'étape Snowflake : détection des intervalles où le GE
tourne réellement (GFMS_DATA_TRACKER_NC, télémétrie 5 minutes) + intégration
des énergies brutes (charge site, batterie DC) + runtime DSE de contrôle
(GENSET_REPORT). Il NE calcule PAS les litres : SPC_L_PER_KWH, PGE_KVA,
POWER_FACTOR et RECTIFIER_EFFICIENCY_RATIO viennent du fichier de référence
FuelCphGeParameter (Postgres, pas Snowflake) — appliqués en Python dans
fuel_cph_service.compute_monthly_cph_estimates, jamais ici.

Tables sources :
  - SITE_FILTERED (DB_GFMS_PROD.GOLD) : dimension site, même dédoublonnage
    par DATA_ID que fuel_consommation_snowflake.py.
  - GFMS_DATA_TRACKER_NC (DB_GFMS_PROD.GOLD) : télémétrie 5 minutes brute —
    ID, TIMESTAMP, LOAD_POWER (W), DG_TOTAL_RUNNING_TIME_MINUTES (compteur
    cumulatif), IM_CURRENT_BATTERY_CHARGE_VALUE (A), DC1_VOLTAGE/
    IM_BATTERY_VOLTAGE_VALUE (V). LOAD_POWER est le signal de charge site
    validé (écart médian 0% vs LOAD_REPORT.LOAD_AVG sur 90j) — ne PAS
    utiliser IM_LOAD_POWER_VALUE (diverge sur certaines configs) ni
    RECTIFIER_OUTPUT_POWER_KW (sortie redresseur, pas charge site).
  - GENSET_REPORT (DB_GFMS_ANALYTICS_PROD.GOLD — 3e base, distincte des deux
    ci-dessus, mais accessible avec les mêmes identifiants/rôle) :
    DG_RUNTIME_CONTROLLER, le runtime du contrôleur DSE, utilisé UNIQUEMENT
    pour valider que le compteur tracker (dg_runtime_interval_h) est fiable
    ce jour-là (tolérance 0.15h) — jamais pour le calcul d'énergie lui-même.

Détection d'intervalle "GE actif" (spec section 4) : pour 2 mesures
successives du même ID, run_minutes = delta du compteur cumulatif,
elapsed_minutes = delta de TIMESTAMP. Actif seulement si les deux sont entre
1 et 10 minutes (garde-fou contre trous de données, doublons, remise à zéro
du compteur). Comme aucune ligne du mois précédent n'est chargée, le tout
premier intervalle du mois de chaque site n'a pas de ligne précédente dans la
fenêtre et est donc perdu (LAG renvoie NULL) — perte négligeable (~5 min par
site par mois), assumée plutôt que de charger un mois de données en plus.

Contrôle batterie (spec section 5) : IM_CURRENT_BATTERY_CHARGE_VALUE entre 0
et 1000 A, COALESCE(DC1_VOLTAGE, IM_BATTERY_VOLTAGE_VALUE) entre 40 et 70 V —
sinon l'intervalle est comptabilisé dans ge_intervals (le site a bien tourné)
mais PAS dans valid_battery_intervals ni dans battery_dc_energy_kwh (la garde
de qualité BATTERY_DATA_NOT_READY si couverture < 95% est appliquée en Python,
pas ici).
"""
import logging
from datetime import date
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

FUEL_DATABASE = "DB_GFMS_PROD"              # SITE_FILTERED, GFMS_DATA_TRACKER_NC
FUEL_SCHEMA = "GOLD"
GENSET_DATABASE = "DB_GFMS_ANALYTICS_PROD"  # GENSET_REPORT — base distincte
GENSET_SCHEMA = "GOLD"

COUNTRY_SCOPE = "Senegal"

CHUNK_SIZE = 500


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


def fetch_site_ge_specs(site_ids: list[str]) -> dict[str, dict]:
    """
    site_id -> {"pge_kva": Decimal|None, "ge_type": str|None} — depuis
    SITE_DG (jointure SITE_ESCO_CURRENT.ID = SITE_DG.S_ID, PAS DATA_ID, voir
    avertissement dans certification/services/snowflake_service.py : DATA_ID
    est un identifiant de télémétrie distinct, seul S_ID est la clé
    d'inventaire utilisée par SITE_DG — vérifié 4230/4230 vs 499/4230).

    Auto-source PGE_KVA/GE_TYPE pour le calcul CPH — vérifié réel et complet
    sur les 10 sites pilotes (2026-08), évite de les redemander dans le
    fichier de référence FuelCphGeParameter (qui ne garde que
    rectifier_efficiency_ratio/spc_l_per_kwh comme champs requis).
    """
    if not site_ids:
        return {}
    result: dict[str, dict] = {}
    conn = _connect()
    try:
        cursor = conn.cursor()
        genset_schema = f"{GENSET_DATABASE}.{GENSET_SCHEMA}"
        for i in range(0, len(site_ids), CHUNK_SIZE):
            chunk = site_ids[i:i + CHUNK_SIZE]
            placeholders = ",".join(f"%(sid{j})s" for j in range(len(chunk)))
            params = {f"sid{j}": sid for j, sid in enumerate(chunk)}
            cursor.execute(
                f"""
                SELECT s.SITE_ID, d.KVA, d.VENDOR, d.GENSET_TYPE
                FROM {genset_schema}.SITE_ESCO_CURRENT s
                JOIN {genset_schema}.SITE_DG d ON d.S_ID = s.ID
                WHERE s.SITE_ID IN ({placeholders})
                """,
                params,
            )
            for site_id, kva, vendor, genset_type in cursor.fetchall():
                ge_type = " ".join(p for p in (vendor, genset_type) if p) or None
                result[site_id] = {
                    "pge_kva": Decimal(str(kva)) if kva is not None else None,
                    "ge_type": ge_type,
                }
        return result
    finally:
        conn.close()


def fetch_monthly_runtime_fallback(year: int, month: int, site_ids: list[str] | None = None) -> dict[str, dict]:
    """
    site_id -> {"runtime_h": Decimal, "source": "DSE_CONTROLLER"|"DG_ON_CALCULATED"}
    — repli quand GFMS_DATA_TRACKER_NC.DG_TOTAL_RUNNING_TIME_MINUTES n'est
    jamais remonté pour un site (vérifié 2026-08 : arrive pour des sites qui
    ont pourtant de la télémétrie 5 min par ailleurs — LOAD_POWER etc. — mais
    dont le compteur GE spécifiquement n'est jamais peuplé).

    Utilise GENSET_REPORT (agrégat journalier), priorité DSE > DG-On calculé
    (même ordre que la règle métier documentée dans la spec CPH). Donne un
    Running Time exploitable mais PAS une estimation de litres : l'énergie
    (charge site + batterie) n'est intégrable qu'à partir du compteur 5 min,
    qu'on n'a justement pas pour ces sites.
    """
    if not site_ids:
        return {}
    d_start = date(year, month, 1)
    d_end_excl = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)

    conn = _connect()
    try:
        cursor = conn.cursor()
        db_schema = f"{FUEL_DATABASE}.{FUEL_SCHEMA}"
        genset_schema = f"{GENSET_DATABASE}.{GENSET_SCHEMA}"
        data_ids = _resolve_data_ids(cursor, db_schema, site_ids)
        if not data_ids:
            return {}

        cursor.execute(f"""
            WITH site_dim AS (
                SELECT DATA_ID, SITE_ID
                FROM (
                    SELECT DATA_ID, SITE_ID,
                           ROW_NUMBER() OVER (PARTITION BY DATA_ID ORDER BY SITE_ID) AS rn
                    FROM {db_schema}.SITE_FILTERED
                    WHERE COUNTRY = %(country)s
                )
                WHERE rn = 1
            )
            SELECT
                s.SITE_ID,
                SUM(g.DG_RUNTIME_CONTROLLER) AS sum_dse,
                SUM(g.DG_RUNTIME_CALCULATED) AS sum_calc
            FROM {genset_schema}.GENSET_REPORT g
            JOIN site_dim s ON s.DATA_ID = g.DATA_ID
            WHERE g.REPORT_DATE >= %(d_start)s AND g.REPORT_DATE < %(d_end_excl)s
              AND g.DATA_ID IN ({','.join(str(d) for d in data_ids)})
            GROUP BY s.SITE_ID
        """, {"country": COUNTRY_SCOPE, "d_start": d_start, "d_end_excl": d_end_excl})

        result: dict[str, dict] = {}
        for site_id, sum_dse, sum_calc in cursor.fetchall():
            if sum_dse is not None and sum_dse > 0:
                result[site_id] = {"runtime_h": Decimal(str(sum_dse)).quantize(Decimal("0.01")), "source": "DSE_CONTROLLER"}
            elif sum_calc is not None and sum_calc > 0:
                result[site_id] = {"runtime_h": Decimal(str(sum_calc)).quantize(Decimal("0.01")), "source": "DG_ON_CALCULATED"}
        return result
    finally:
        conn.close()


def fetch_site_rectifier_efficiency(year: int, month: int, site_ids: list[str]) -> dict[str, Decimal]:
    """
    site_id -> Decimal ratio (0-1) — moyenne mensuelle de RECTIFIER_EFFICIENCY
    (GFMS_DATA_TRACKER_NC, colonne en %, 0-100), NULLIF(...,0) pour exclure
    les zéros (traités comme une absence de mesure plutôt qu'un vrai 0%
    d'efficacité, cohérent avec le traitement des placeholders ailleurs dans
    ce module). Repli utilisé quand FuelCphGeParameter.rectifier_efficiency_ratio
    n'est pas renseigné dans le fichier de référence.

    Couverture vérifiée 2026-08 sur les 483 sites Sénégal avec GE : ~46%
    (171/372 avec au moins une mesure), valeurs plausibles (médiane ~70%,
    9-95%) — assez fiable pour servir de repli, contrairement à SPC_L_PER_KWH
    qui n'a aucune source Snowflake exploitable (voir fuel_cph_service.py).
    """
    if not site_ids:
        return {}
    d_start = date(year, month, 1)
    d_end_excl = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)

    conn = _connect()
    try:
        cursor = conn.cursor()
        db_schema = f"{FUEL_DATABASE}.{FUEL_SCHEMA}"
        data_ids = _resolve_data_ids(cursor, db_schema, site_ids)
        if not data_ids:
            return {}

        cursor.execute(f"""
            WITH site_dim AS (
                SELECT DATA_ID, SITE_ID
                FROM (
                    SELECT DATA_ID, SITE_ID,
                           ROW_NUMBER() OVER (PARTITION BY DATA_ID ORDER BY SITE_ID) AS rn
                    FROM {db_schema}.SITE_FILTERED
                    WHERE COUNTRY = %(country)s
                )
                WHERE rn = 1
            )
            SELECT s.SITE_ID, AVG(NULLIF(t.RECTIFIER_EFFICIENCY, 0)) AS avg_eff
            FROM {db_schema}.GFMS_DATA_TRACKER_NC t
            JOIN site_dim s ON s.DATA_ID = t.ID
            WHERE t."TIMESTAMP" >= %(d_start)s AND t."TIMESTAMP" < %(d_end_excl)s
              AND t.ID IN ({','.join(str(d) for d in data_ids)})
            GROUP BY s.SITE_ID
            HAVING AVG(NULLIF(t.RECTIFIER_EFFICIENCY, 0)) IS NOT NULL
        """, {"country": COUNTRY_SCOPE, "d_start": d_start, "d_end_excl": d_end_excl})

        return {
            site_id: (Decimal(str(avg_eff)) / Decimal("100")).quantize(Decimal("0.001"))
            for site_id, avg_eff in cursor.fetchall()
        }
    finally:
        conn.close()


def _resolve_data_ids(cursor, db_schema: str, site_ids: list[str]) -> list[int]:
    """SITE_ID -> DATA_ID via SITE_FILTERED, par lots de CHUNK_SIZE (même
    principe que SnowflakeService._chunks — IN() Snowflake reste praticable
    jusqu'à quelques milliers d'éléments mais on borne par prudence)."""
    data_ids: list[int] = []
    for i in range(0, len(site_ids), CHUNK_SIZE):
        chunk = site_ids[i:i + CHUNK_SIZE]
        placeholders = ",".join(f"%(sid{j})s" for j in range(len(chunk)))
        params = {f"sid{j}": sid for j, sid in enumerate(chunk)}
        params["country"] = COUNTRY_SCOPE
        cursor.execute(
            f"""
            SELECT DISTINCT DATA_ID FROM {db_schema}.SITE_FILTERED
            WHERE COUNTRY = %(country)s AND SITE_ID IN ({placeholders})
            """,
            params,
        )
        data_ids.extend(int(row[0]) for row in cursor.fetchall() if row[0] is not None)
    return data_ids


def _resolve_business_runtime(dse_h, dg_on_h, rectifier_h, is_hybrid_solar_ge) -> tuple[Decimal | None, str]:
    """
    Priorité exacte de la spec (section 6) : DSE en premier quel que soit le
    profil du site ; à défaut, DG-On calculé pour les sites NON hybrides
    solaire+GE ; à défaut, runtime redresseur (5 min) pour les hybrides
    solaire+GE sans DSE. `is_hybrid_solar_ge` vient de VW_INVOICE_DATA_REPORT
    (DG='Yes' AND Solar='Yes') — absent (None) traité comme non-hybride (cas
    très majoritaire observé, ~92% des sites avec GE ET solaire sont déjà
    hybrides quand le drapeau est connu, mais l'absence de ligne elle-même
    est le cas courant hors GE, donc pas un signal fiable de solaire).
    """
    if dse_h is not None and 0 <= dse_h <= 24:
        return dse_h, "DSE_CONTROLLER"
    if not is_hybrid_solar_ge and dg_on_h is not None and 0 <= dg_on_h <= 24:
        return dg_on_h, "DG_ON_CALCULATED"
    if is_hybrid_solar_ge and rectifier_h is not None:
        return rectifier_h, "RECTIFIER_STATUS_5MIN"
    return None, "NO_VALID_RUNTIME"


def fetch_daily_tracker_energy(year: int, month: int, site_ids: list[str] | None = None) -> dict[str, dict[date, dict]]:
    """
    Retourne {site_id: {date: {
        country, data_id,
        ge_intervals, valid_battery_intervals,
        dg_runtime_interval_h, dg_runtime_controller_h,
        dg_runtime_business_h, dg_runtime_business_source,
        site_load_energy_kwh, battery_dc_energy_kwh,
    }}} — uniquement les (site, date) avec au moins un intervalle GE détecté
    ce jour-là (contrairement à fetch_monthly_consumption, pas une ligne par
    site pour chaque jour du mois : un site sans marche GE ce jour n'a
    simplement pas d'entrée).

    dg_runtime_controller_h (DSE) reste la valeur utilisée pour la
    validation de l'intervalle (tolérance 0.15h, spec section 6) — jamais
    remplacée par le repli. dg_runtime_business_h/source est le runtime
    "métier" à 3 sources (DSE > DG-On calculé > redresseur 5 min pour les
    hybrides solaire+GE sans DSE), calculé jour par jour via
    _resolve_business_runtime — c'est la colonne DG_RUNTIME_BUSINESS_H/
    DG_RUNTIME_BUSINESS_SOURCE du schéma de sortie documenté (spec section
    2.2), distincte de la validation d'intervalle.

    `site_ids`, si fourni, restreint le scan de GFMS_DATA_TRACKER_NC (coûteux
    à l'échelle du pays — voir avertissement de volume ci-dessous) à ces
    sites uniquement. Utilisé pour valider le pilote (175 sites) avant un
    déploiement complet Sénégal (~3191 sites).

    AVERTISSEMENT VOLUME : GFMS_DATA_TRACKER_NC est à la granularité 5
    minutes. Un scan complet Sénégal sur un mois représente plusieurs
    dizaines de millions de lignes fenêtrées (LAG) — aucun service existant
    de ce module ne fait ce type de scan pays entier. Toujours valider sur un
    périmètre restreint (site_ids) avant un premier appel sans restriction.
    """
    d_start = date(year, month, 1)
    d_end_excl = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)

    conn = _connect()
    try:
        cursor = conn.cursor()
        db_schema = f"{FUEL_DATABASE}.{FUEL_SCHEMA}"
        genset_schema = f"{GENSET_DATABASE}.{GENSET_SCHEMA}"

        data_id_filter_sql = ""
        site_id_filter_sql = ""
        if site_ids:
            data_ids = _resolve_data_ids(cursor, db_schema, site_ids)
            if not data_ids:
                return {}
            # DATA_ID est un entier renvoyé par Snowflake lui-même (jamais une
            # entrée utilisateur) — interpolation directe sûre, même principe
            # que les noms de table/schéma qualifiés ailleurs dans ce module.
            data_id_filter_sql = f"AND t.ID IN ({','.join(str(d) for d in data_ids)})"
            site_placeholders = ",".join(f"%(rsid{j})s" for j in range(len(site_ids)))
            site_id_filter_sql = f"AND SITE_ID IN ({site_placeholders})"

        params = {"country": COUNTRY_SCOPE, "d_start": d_start, "d_end_excl": d_end_excl}
        if site_ids:
            params.update({f"rsid{j}": sid for j, sid in enumerate(site_ids)})

        cursor.execute(f"""
            WITH site_dim AS (
                SELECT DATA_ID, SITE_ID, COUNTRY
                FROM (
                    SELECT DATA_ID, SITE_ID, COUNTRY,
                           ROW_NUMBER() OVER (PARTITION BY DATA_ID ORDER BY SITE_ID) AS rn
                    FROM {db_schema}.SITE_FILTERED
                    WHERE COUNTRY = %(country)s
                )
                WHERE rn = 1
            ),
            tracker_ordered AS (
                SELECT
                    t.ID AS data_id,
                    t."TIMESTAMP" AS ts,
                    CAST(t."TIMESTAMP" AS DATE) AS day,
                    t.LOAD_POWER AS load_power,
                    t.DG_TOTAL_RUNNING_TIME_MINUTES AS runtime_minutes,
                    t.IM_CURRENT_BATTERY_CHARGE_VALUE AS battery_current,
                    COALESCE(t.DC1_VOLTAGE, t.IM_BATTERY_VOLTAGE_VALUE) AS battery_voltage,
                    LAG(t.DG_TOTAL_RUNNING_TIME_MINUTES) OVER (PARTITION BY t.ID ORDER BY t."TIMESTAMP") AS prev_runtime_minutes,
                    LAG(t."TIMESTAMP") OVER (PARTITION BY t.ID ORDER BY t."TIMESTAMP") AS prev_ts
                FROM {db_schema}.GFMS_DATA_TRACKER_NC t
                WHERE t."TIMESTAMP" >= %(d_start)s AND t."TIMESTAMP" < %(d_end_excl)s
                  AND t.DG_TOTAL_RUNNING_TIME_MINUTES IS NOT NULL
                  {data_id_filter_sql}
            ),
            tracker_intervals AS (
                SELECT *,
                    runtime_minutes - prev_runtime_minutes AS run_minutes,
                    DATEDIFF('minute', prev_ts, ts) AS elapsed_minutes,
                    CASE
                        WHEN battery_current BETWEEN 0 AND 1000
                         AND battery_voltage BETWEEN 40 AND 70
                        THEN battery_current * battery_voltage / 1000.0
                    END AS battery_dc_kw
                FROM tracker_ordered
            ),
            ge_active_intervals AS (
                SELECT *
                FROM tracker_intervals
                WHERE run_minutes BETWEEN 1 AND 10
                  AND elapsed_minutes BETWEEN 1 AND 10
            ),
            daily_energy AS (
                SELECT
                    data_id,
                    day,
                    COUNT(*) AS ge_intervals,
                    COUNT(battery_dc_kw) AS valid_battery_intervals,
                    SUM(run_minutes) / 60.0 AS dg_runtime_interval_h,
                    SUM(CASE WHEN load_power IS NOT NULL THEN load_power / 1000.0 * run_minutes / 60.0 END) AS site_load_energy_kwh,
                    SUM(battery_dc_kw * run_minutes / 60.0) AS battery_dc_energy_kwh
                FROM ge_active_intervals
                GROUP BY data_id, day
            ),
            rectifier_daily AS (
                -- Runtime redresseur (spec section 3/6) : DB_GFMS_PROD.GOLD.
                -- RECTIFIER_EFFICIENCY_STATUS, colonne RECTIFIER_STATUS en
                -- Good/Aged/Warning/Down (PAS le RECTIFIER_STATUS binaire de
                -- GFMS_DATA_TRACKER_NC — table distincte, vérifiée 2026-08).
                SELECT SITE_ID, CAST("TIMESTAMP" AS DATE) AS day,
                       COUNT_IF(RECTIFIER_STATUS IN ('Good','Aged','Warning')) * 5.0 / 60 AS runtime_h
                FROM {db_schema}.RECTIFIER_EFFICIENCY_STATUS
                WHERE COUNTRY = %(country)s AND "TIMESTAMP" >= %(d_start)s AND "TIMESTAMP" < %(d_end_excl)s
                  {site_id_filter_sql}
                GROUP BY SITE_ID, CAST("TIMESTAMP" AS DATE)
            ),
            hybrid_daily AS (
                -- Classification hybride solaire+GE (spec section 6) :
                -- VW_INVOICE_DATA_REPORT.DG/Solar, grain (Site ID, Date).
                SELECT "Site ID" AS SITE_ID, "Date" AS day,
                       ("DG" = 'Yes' AND "Solar" = 'Yes') AS is_hybrid_solar_ge
                FROM {genset_schema}.VW_INVOICE_DATA_REPORT
                WHERE "Country" = %(country)s AND "Date" >= %(d_start)s AND "Date" < %(d_end_excl)s
                  {site_id_filter_sql}
            )
            SELECT
                s.SITE_ID, s.COUNTRY, d.data_id, d.day, d.ge_intervals, d.valid_battery_intervals,
                d.dg_runtime_interval_h, d.site_load_energy_kwh, d.battery_dc_energy_kwh,
                -- Bornage 0-24h à la source : DG_RUNTIME_CONTROLLER/CALCULATED
                -- contiennent parfois des valeurs aberrantes (constaté 2026-08 :
                -- jusqu'à 1 192 095 h pour UNE journée — clairement un compteur
                -- cumulatif mal réinitialisé, pas un runtime journalier réel).
                -- Nullifié ici plutôt que filtré en Python pour qu'aucune valeur
                -- corrompue ne soit jamais stockée, même rejetée.
                CASE WHEN g.DG_RUNTIME_CONTROLLER BETWEEN 0 AND 24 THEN g.DG_RUNTIME_CONTROLLER END AS DG_RUNTIME_CONTROLLER,
                CASE WHEN g.DG_RUNTIME_CALCULATED BETWEEN 0 AND 24 THEN g.DG_RUNTIME_CALCULATED END AS DG_RUNTIME_CALCULATED,
                r.runtime_h AS rectifier_runtime_h, h.is_hybrid_solar_ge
            FROM daily_energy d
            JOIN site_dim s ON s.DATA_ID = d.data_id
            LEFT JOIN {genset_schema}.GENSET_REPORT g
                ON g.DATA_ID = d.data_id AND g.REPORT_DATE = d.day
            LEFT JOIN rectifier_daily r ON r.SITE_ID = s.SITE_ID AND r.day = d.day
            LEFT JOIN hybrid_daily h ON h.SITE_ID = s.SITE_ID AND h.day = d.day
        """, params)

        result: dict[str, dict[date, dict]] = {}
        for (site_id, country, data_id, day, ge_intervals, valid_battery_intervals,
             dg_runtime_interval_h, site_load_energy_kwh, battery_dc_energy_kwh,
             dse_h, dg_on_h, rectifier_h, is_hybrid_solar_ge) in cursor.fetchall():

            dse_dec = Decimal(str(dse_h)) if dse_h is not None else None
            dg_on_dec = Decimal(str(dg_on_h)) if dg_on_h is not None else None
            rectifier_dec = Decimal(str(rectifier_h)).quantize(Decimal("0.01")) if rectifier_h is not None else None
            business_h, business_source = _resolve_business_runtime(dse_dec, dg_on_dec, rectifier_dec, bool(is_hybrid_solar_ge))

            result.setdefault(site_id, {})[day] = {
                "country": country,
                "data_id": int(data_id) if data_id is not None else None,
                "ge_intervals": int(ge_intervals or 0),
                "valid_battery_intervals": int(valid_battery_intervals or 0),
                "dg_runtime_interval_h": Decimal(str(dg_runtime_interval_h)) if dg_runtime_interval_h is not None else None,
                "dg_runtime_controller_h": dse_dec,
                "dg_runtime_business_h": business_h,
                "dg_runtime_business_source": business_source,
                "site_load_energy_kwh": Decimal(str(site_load_energy_kwh)) if site_load_energy_kwh is not None else None,
                "battery_dc_energy_kwh": Decimal(str(battery_dc_energy_kwh)) if battery_dc_energy_kwh is not None else None,
            }
        return result
    finally:
        conn.close()
