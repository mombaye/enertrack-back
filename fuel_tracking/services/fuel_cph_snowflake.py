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


# fetch_monthly_runtime_fallback (repli SUM(GENSET_REPORT) sur tout le mois,
# priorité DSE > DG-On uniquement, sans redresseur/tracker ni bornage 0-24h)
# a été RETIRÉ 2026-09 : la nouvelle union de jours de fetch_daily_tracker_energy
# (daily_energy ∪ genset_daily ∪ rectifier_daily, voir day_universe ci-dessous)
# couvre nativement, jour par jour et via les 8 règles de _resolve_business_runtime,
# exactement les cas que ce repli grossier tentait de rattraper après coup —
# le garder aurait fait cohabiter 2 moteurs de priorité pouvant diverger.


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


# Codes possibles de dg_runtime_business_source / dg_runtime_business_status
# (retour de _resolve_business_runtime) — spec "règles" 2026-09, remplace
# l'ancienne priorité DSE > DG-On > Redresseur > Tracker (audit 2026-09 :
# le tracker doit passer AVANT DG-On/Redresseur quand le DSE est absent, pas
# après ; DSE=0 doit être distingué d'un DSE réellement absent).
RUNTIME_DSE_CONTROLLER = "DSE_CONTROLLER"
RUNTIME_DSE_ZERO_CONFIRMED = "DSE_ZERO_CONFIRMED"
RUNTIME_DSE_ZERO_CONFLICT = "DSE_ZERO_SOURCE_CONFLICT"
RUNTIME_TRACKER_5MIN = "TRACKER_5MIN"
RUNTIME_DG_ON_CALCULATED = "DG_ON_CALCULATED"
RUNTIME_RECTIFIER_5MIN = "RECTIFIER_STATUS_5MIN"
RUNTIME_NO_VALID = "NO_VALID_RUNTIME"

# Sources "physiques" reconnues — sous-ensemble des statuts ci-dessus pour
# lesquels dg_runtime_business_source doit être renseigné (les 4 autres
# statuts — ZERO_CONFIRMED/CONFLICT/NO_VALID, + NOT_APPLICABLE_NO_GE
# appliqué en aval par sync_fuel_cph selon has_genset Postgres — ne
# désignent aucune source physique, seulement dg_runtime_business_status).
_PHYSICAL_SOURCES = {RUNTIME_DSE_CONTROLLER, RUNTIME_TRACKER_5MIN, RUNTIME_DG_ON_CALCULATED, RUNTIME_RECTIFIER_5MIN}


def _resolve_business_runtime(dse_h, dg_on_h, rectifier_h, is_hybrid_solar_ge, tracker_h=None) -> tuple[Decimal | None, str | None, str, str | None]:
    """
    Retourne (runtime_h, runtime_source, runtime_status, rejection_reason).

    runtime_status porte TOUJOURS l'un des 8 codes de règle ci-dessous ;
    runtime_source ne porte qu'un sous-ensemble (DSE_CONTROLLER/TRACKER_5MIN/
    DG_ON_CALCULATED/RECTIFIER_STATUS_5MIN) — None pour les 4 autres statuts,
    qui ne désignent aucune source physique gagnante.

    Règles exactes (remplace l'ancienne priorité DSE > DG-On > Redresseur >
    Tracker, corrigée suite à l'audit 2026-09 : "la télémétrie 5 min tracker
    est repassée à zéro dans l'API alors que Snowflake en contient encore
    pour septembre" — le tracker doit primer sur DG-On/Redresseur quand le
    DSE est absent, pas l'inverse) :
      1. Sans GE                                    -> NOT_APPLICABLE_NO_GE
         (appliqué en aval, cette fonction ne reçoit que des sites avec GE —
         voir sync_fuel_cph.py, has_genset vient de Postgres, pas Snowflake)
      2. DSE > 0                                    -> DSE_CONTROLLER
      3. DSE = 0, aucune autre source strictement positive
                                                     -> DSE_ZERO_CONFIRMED
         (0h métier réel, PAS un rejet : la valeur DSE=0 est prise au mot
         puisque rien d'autre ne la contredit)
      4. DSE = 0, tracker/DG-On/redresseur strictement positif ce jour
                                                     -> DSE_ZERO_SOURCE_CONFLICT
         (conflit de sources signalé tel quel — AUCUN repli automatique sur
         l'autre source, contrairement à l'ancien comportement qui traitait
         silencieusement DSE=0 comme invalide et retombait sur le palier
         suivant)
      5. DSE absent, tracker 5 min valide            -> TRACKER_5MIN
      6. DSE absent, tracker absent, site NON hybride solaire+GE,
         DG-On calculé valide                        -> DG_ON_CALCULATED
      7. DSE absent, tracker absent, site hybride solaire+GE,
         redresseur valide                           -> RECTIFIER_STATUS_5MIN
      8. Sinon                                       -> NO_VALID_RUNTIME

    `is_hybrid_solar_ge` vient de VW_INVOICE_DATA_REPORT (DG='Yes' AND
    Solar='Yes') — absent (None) traité comme non-hybride (cas très
    majoritaire observé, ~92% des sites avec GE ET solaire sont déjà
    hybrides quand le drapeau est connu, mais l'absence de ligne elle-même
    est le cas courant hors GE, donc pas un signal fiable de solaire).
    """
    other_positive = any(v is not None and v > 0 for v in (tracker_h, dg_on_h, rectifier_h))

    if dse_h is not None:
        if dse_h > 0:
            return dse_h, RUNTIME_DSE_CONTROLLER, RUNTIME_DSE_CONTROLLER, None
        if other_positive:
            candidats = ", ".join(
                f"{label}={v}" for label, v in (("tracker", tracker_h), ("DG-On", dg_on_h), ("redresseur", rectifier_h))
                if v is not None and v > 0
            )
            return None, None, RUNTIME_DSE_ZERO_CONFLICT, f"DSE=0 alors que {candidats} positif ce jour — conflit de sources, aucun repli automatique appliqué."
        return Decimal("0"), None, RUNTIME_DSE_ZERO_CONFIRMED, None

    # DSE absent : tracker > DG-On (non-hybride) > redresseur (hybride).
    if tracker_h is not None and tracker_h > 0:
        return tracker_h, RUNTIME_TRACKER_5MIN, RUNTIME_TRACKER_5MIN, None
    if not is_hybrid_solar_ge and dg_on_h is not None and dg_on_h > 0:
        return dg_on_h, RUNTIME_DG_ON_CALCULATED, RUNTIME_DG_ON_CALCULATED, None
    if is_hybrid_solar_ge and rectifier_h is not None and rectifier_h > 0:
        return rectifier_h, RUNTIME_RECTIFIER_5MIN, RUNTIME_RECTIFIER_5MIN, None
    return None, None, RUNTIME_NO_VALID, "Aucune source de runtime disponible ce jour (ni DSE, ni tracker, ni DG-On, ni redresseur)."


def fetch_daily_tracker_energy(year: int, month: int, site_ids: list[str] | None = None) -> dict[str, dict[date, dict]]:
    """
    Retourne {site_id: {date: {
        country, data_id,
        ge_intervals, valid_battery_intervals,
        dg_runtime_interval_h, dg_runtime_controller_h,
        dg_runtime_business_h, dg_runtime_business_source, dg_runtime_business_status,
        dg_runtime_business_rejection_reason,
        site_load_energy_kwh, battery_dc_energy_kwh, load_kw,
    }}}.

    Univers des jours retournés (corrige un bug 2026-09 : une journée DSE/
    DG-On sans intervalle tracker actif ce jour-là était auparavant absente
    du résultat — d'où "35 cas DG-On ont un runtime mais aucun ne calcule de
    CPH, l'API retourne MISSING_LOAD_POWER" alors que la charge existe bien
    dans LOAD_REPORT pour ces jours) : UNION de
      - jours où le tracker 5 min a détecté une activité GE (daily_energy) ;
      - jours où GENSET_REPORT a une ligne (DSE et/ou DG-On, même si les deux
        sont NULL — la borne 0-24h peut aussi les avoir nullifiés) ;
      - jours où RECTIFIER_EFFICIENCY_STATUS a une ligne.
    Un jour "GENSET_REPORT sans tracker actif" n'a PAS de site_load_energy_kwh
    tracker (intégration 5 min impossible sans intervalle) mais reçoit
    load_kw (LOAD_REPORT, jointure obligatoire LOAD_REPORT.ID = GENSET_REPORT.
    DATA_ID AND LOAD_REPORT.DATE = GENSET_REPORT.REPORT_DATE, LOAD_AVG/1000)
    — fuel_cph_service.compute_daily_status l'utilise comme énergie de repli
    (load_kw × runtime_h) quand le tracker n'a rien ce jour-là.

    dg_runtime_controller_h (DSE) reste la valeur utilisée pour la
    validation de l'intervalle tracker (tolérance 0.15h) — jamais remplacée
    par le repli, et seulement quand un intervalle tracker existe (voir
    compute_daily_status : aucune validation d'intervalle n'est possible ni
    nécessaire les jours sans tracker). dg_runtime_business_h/source/status/
    rejection_reason est le runtime "métier" résolu par _resolve_business_runtime
    (8 règles — DSE > tracker > DG-On [non-hybride]/redresseur [hybride],
    DSE=0 distingué en confirmé/conflit) — c'est CETTE valeur (pas
    dg_runtime_interval_h) qui alimente le Running Time agrégé/affiché.

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
        data_id_filter_sql_g = ""
        site_id_filter_sql = ""
        if site_ids:
            data_ids = _resolve_data_ids(cursor, db_schema, site_ids)
            if not data_ids:
                return {}
            # DATA_ID est un entier renvoyé par Snowflake lui-même (jamais une
            # entrée utilisateur) — interpolation directe sûre, même principe
            # que les noms de table/schéma qualifiés ailleurs dans ce module.
            data_id_filter_sql = f"AND t.ID IN ({','.join(str(d) for d in data_ids)})"
            data_id_filter_sql_g = f"AND g.DATA_ID IN ({','.join(str(d) for d in data_ids)})"
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
            ),
            genset_daily AS (
                -- DSE + DG-On (GENSET_REPORT) + charge du jour (LOAD_REPORT,
                -- jointure obligatoire ID=DATA_ID/DATE=REPORT_DATE — spec
                -- 2026-09 "corriger la jointure suivante dans le pipeline").
                -- Une ligne par (data_id, day) dès que GENSET_REPORT en a une,
                -- même si DSE et DG-On sont tous deux NULL/nullifiés : c'est
                -- justement l'univers de jours qui manquait à daily_energy
                -- (tracker-only) pour que les jours DSE/DG-On sans intervalle
                -- tracker actif ce jour-là ne soient plus silencieusement
                -- absents du résultat.
                SELECT
                    g.DATA_ID AS data_id,
                    g.REPORT_DATE AS day,
                    -- Bornage 0-24h à la source : DG_RUNTIME_CONTROLLER/CALCULATED
                    -- contiennent parfois des valeurs aberrantes (constaté 2026-08 :
                    -- jusqu'à 1 192 095 h pour UNE journée — clairement un compteur
                    -- cumulatif mal réinitialisé, pas un runtime journalier réel).
                    -- Nullifié ici plutôt que filtré en Python pour qu'aucune valeur
                    -- corrompue ne soit jamais stockée, même rejetée.
                    CASE WHEN g.DG_RUNTIME_CONTROLLER BETWEEN 0 AND 24 THEN g.DG_RUNTIME_CONTROLLER END AS dse_h,
                    CASE WHEN g.DG_RUNTIME_CALCULATED BETWEEN 0 AND 24 THEN g.DG_RUNTIME_CALCULATED END AS dg_on_h,
                    l.LOAD_AVG / 1000.0 AS load_kw,
                    -- Colonnes fuel DSE (FUEL_LEVEL_START/END/CONSUMED) — présentes
                    -- dans GENSET_REPORT mais jusqu'ici non récupérées. Stockées
                    -- telles quelles (brut DSE) dans controller_fuel_* pour audit et
                    -- croisement avec VW_FUEL_REPORT. Valeurs négatives = corrompues,
                    -- nullifiées à la source.
                    CASE WHEN g.FUEL_LEVEL_START >= 0 THEN CAST(g.FUEL_LEVEL_START AS DECIMAL(12,3)) END AS fuel_level_start,
                    CASE WHEN g.FUEL_LEVEL_END >= 0 THEN CAST(g.FUEL_LEVEL_END AS DECIMAL(12,3)) END AS fuel_level_end,
                    CASE WHEN g.FUEL_CONSUMED >= 0 THEN CAST(g.FUEL_CONSUMED AS DECIMAL(12,3)) END AS fuel_consumed
                FROM {genset_schema}.GENSET_REPORT g
                LEFT JOIN {genset_schema}.LOAD_REPORT l
                    ON l.ID = g.DATA_ID AND l.DATE = g.REPORT_DATE
                WHERE g.REPORT_DATE >= %(d_start)s AND g.REPORT_DATE < %(d_end_excl)s
                  {data_id_filter_sql_g}
            ),
            day_universe AS (
                -- Union des 3 sources de journées possibles (spec 2026-09,
                -- point 1/2/3) — un site/jour peut n'apparaître QUE dans
                -- genset_daily (DSE/DG-On sans tracker actif) ou QUE dans
                -- rectifier_daily (redresseur seul), pas seulement dans
                -- daily_energy comme avant.
                SELECT data_id, day FROM daily_energy
                UNION
                SELECT data_id, day FROM genset_daily
                UNION
                SELECT sd.DATA_ID AS data_id, r.day AS day
                FROM rectifier_daily r
                JOIN site_dim sd ON sd.SITE_ID = r.SITE_ID
            )
            SELECT
                s.SITE_ID, s.COUNTRY, u.data_id, u.day,
                d.ge_intervals, d.valid_battery_intervals,
                d.dg_runtime_interval_h, d.site_load_energy_kwh, d.battery_dc_energy_kwh,
                gd.dse_h AS DG_RUNTIME_CONTROLLER, gd.dg_on_h AS DG_RUNTIME_CALCULATED, gd.load_kw,
                r.runtime_h AS rectifier_runtime_h, h.is_hybrid_solar_ge,
                gd.fuel_level_start, gd.fuel_level_end, gd.fuel_consumed
            FROM day_universe u
            JOIN site_dim s ON s.DATA_ID = u.data_id
            LEFT JOIN daily_energy d ON d.data_id = u.data_id AND d.day = u.day
            LEFT JOIN genset_daily gd ON gd.data_id = u.data_id AND gd.day = u.day
            LEFT JOIN rectifier_daily r ON r.SITE_ID = s.SITE_ID AND r.day = u.day
            LEFT JOIN hybrid_daily h ON h.SITE_ID = s.SITE_ID AND h.day = u.day
        """, params)

        result: dict[str, dict[date, dict]] = {}
        for (site_id, country, data_id, day, ge_intervals, valid_battery_intervals,
             dg_runtime_interval_h, site_load_energy_kwh, battery_dc_energy_kwh,
             dse_h, dg_on_h, load_kw, rectifier_h, is_hybrid_solar_ge,
             fuel_level_start, fuel_level_end, fuel_consumed) in cursor.fetchall():

            dse_dec = Decimal(str(dse_h)) if dse_h is not None else None
            dg_on_dec = Decimal(str(dg_on_h)) if dg_on_h is not None else None
            rectifier_dec = Decimal(str(rectifier_h)).quantize(Decimal("0.01")) if rectifier_h is not None else None
            tracker_dec = Decimal(str(dg_runtime_interval_h)) if dg_runtime_interval_h is not None else None
            load_kw_dec = Decimal(str(load_kw)).quantize(Decimal("0.001")) if load_kw is not None else None
            business_h, business_source, business_status, rejection_reason = _resolve_business_runtime(
                dse_dec, dg_on_dec, rectifier_dec, bool(is_hybrid_solar_ge), tracker_h=tracker_dec
            )

            result.setdefault(site_id, {})[day] = {
                "country": country,
                "data_id": int(data_id) if data_id is not None else None,
                "ge_intervals": int(ge_intervals or 0),
                "valid_battery_intervals": int(valid_battery_intervals or 0),
                "dg_runtime_interval_h": tracker_dec,
                "dg_runtime_controller_h": dse_dec,
                "dg_runtime_business_h": business_h,
                "dg_runtime_business_source": business_source,
                "dg_runtime_business_status": business_status,
                "dg_runtime_business_rejection_reason": rejection_reason,
                "site_load_energy_kwh": Decimal(str(site_load_energy_kwh)) if site_load_energy_kwh is not None else None,
                "battery_dc_energy_kwh": Decimal(str(battery_dc_energy_kwh)) if battery_dc_energy_kwh is not None else None,
                "load_kw": load_kw_dec,
                "controller_fuel_level_start": Decimal(str(fuel_level_start)) if fuel_level_start is not None else None,
                "controller_fuel_level_end": Decimal(str(fuel_level_end)) if fuel_level_end is not None else None,
                "controller_fuel_consumed": Decimal(str(fuel_consumed)) if fuel_consumed is not None else None,
            }
        return result
    finally:
        conn.close()
