# financial/services/site_scope_snowflake.py
"""
Périmètre de sites pour la sync financière (sync_financial_conso), déterminé
depuis Snowflake plutôt que depuis le catalogue local core.Site/billing.
ContractSiteLink (alimenté manuellement via /admin/sites, souvent absent en
environnement de dev local — voir échange du 2026-08).

DB_GFMS_PROD.GOLD.SITES_FILTERED_FIXED a une colonne CLIENT qui vaut
'AktivCo' pour les sites facturés par Aktivco (78 sites Sénégal vérifiés le
2026-08) — équivalent réel de Site.invoice_payment. En revanche, aucune
colonne Snowflake n'équivaut à Site.grid_fee (une redevance grid est une
décision contractuelle, pas un attribut technique du site ; POWER_SUPPLY a
été vérifié comme non fiable : 32/78 sites Aktivco l'ont à "Undefined").

Ce périmètre Snowflake est donc VOLONTAIREMENT plus large que le vrai
périmètre de production (qui recroise en plus grid_fee=True côté Django) —
à utiliser pour la visibilité en local / dev, en attendant l'import réel du
catalogue de sites. Documenté explicitement dans sync_financial_conso.py.
"""
import logging

logger = logging.getLogger(__name__)

FUEL_DATABASE = "DB_GFMS_PROD"
FUEL_SCHEMA = "GOLD"


def fetch_aktivco_site_scope(country: str = "Senegal") -> list[dict]:
    """
    Retourne [{"site_id": str, "site_name": str|None, "site_monitored": bool,
    "power_supply": str|None}, ...] pour tous les sites du pays donné dont
    CLIENT = 'AktivCo' sur Snowflake.

    site_monitored (SITE_MONITORED='1' côté Snowflake) est le prédicteur quasi
    parfait de la présence de données Grid/ACM/Solaire : vérifié le 2026-08 sur
    les 78 sites Aktivco Sénégal, les 42 sites sans AUCUNE donnée synchronisée
    étaient TOUS SITE_MONITORED='0' (aucun capteur/compteur raccordé au GFMS —
    trou d'instrumentation physique, pas un problème de sync côté EnerTrack).
    """
    from certification.services.snowflake_service import SnowflakeService

    sf = SnowflakeService()
    conn = sf._connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT SITE_ID, SITE_NAME, SITE_MONITORED, POWER_SUPPLY
            FROM {FUEL_DATABASE}.{FUEL_SCHEMA}.SITES_FILTERED_FIXED
            WHERE COUNTRY = %(country)s AND CLIENT = 'AktivCo'
        """, {"country": country})
        return [
            {
                "site_id": row[0],
                "site_name": row[1],
                "site_monitored": row[2] == "1",
                "power_supply": row[3],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()
