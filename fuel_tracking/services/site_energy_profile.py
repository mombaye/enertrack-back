# fuel_tracking/services/site_energy_profile.py
"""
Profil énergétique d'un site — attributs SÉPARÉS de la typologie site (colonne TYPOLOGIE, inchangée).

Chaque attribut n'est renseigné qu'à partir d'un champ source explicite (aucune déduction depuis le code
de typologie) ; sinon « Inconnu ». Règles de normalisation :

  Environnement  core.Site.installed_site_type, sinon core.Site.site_type      INDOOR → Indoor, OUTDOOR → Outdoor
  Raccordement   FuelSiteInventory.grid_supply (Snowflake SITE_ESCO_CURRENT.GRID_SUPPLY_MODIFIED)
                   valeur contenant « OFF » → Off-grid ; « ON » / « GRID » / « SECTEUR » → Grid
  GE             FuelSiteInventory.dg_count (> 0 → Oui, = 0 → Non), sinon FuelSiteScope.has_genset
  Solaire        core.Site.not_yet_solarized (« Sites non encore solarisés ») : Non → solarisé (Oui), Oui → Non

Profil énergétique calculé : Grid / Off-grid + GE (+ solaire) quand les trois attributs sont connus,
sinon « Incomplet / à confirmer ». audit_energy_fields() mesure la couverture réelle de chaque source.
"""
from __future__ import annotations

from collections import Counter

UNKNOWN = "Inconnu"


def _env(site: dict) -> str:
    v = (site.get("installed_site_type") or site.get("site_type") or "").upper()
    return {"INDOOR": "Indoor", "OUTDOOR": "Outdoor"}.get(v, UNKNOWN)


def _grid(raw) -> str:
    s = str(raw or "").upper().replace("-", "").replace(" ", "")
    if not s:
        return UNKNOWN
    if "OFF" in s:
        return "Off-grid"
    if "ON" in s or "GRID" in s or "SECTEUR" in s:
        return "Grid"
    return UNKNOWN


def _yes_no(v) -> str:
    return UNKNOWN if v is None else ("Oui" if v else "Non")


def profile_label(grid: str, ge: str, solar: str) -> str:
    if UNKNOWN in (grid, ge, solar):
        return "Incomplet / à confirmer"
    if ge == "Non":
        return f"{grid} sans GE" + (" + solaire" if solar == "Oui" else "")
    return f"{grid} + GE" + (" + solaire" if solar == "Oui" else "")


def energy_profiles(site_ids=None) -> dict[str, dict]:
    """{site_id: {environnement, raccordement, ge, solaire, profil, sources}} pour les sites du référentiel."""
    from core.models import Site
    from fuel_tracking.models import FuelSiteInventory, FuelSiteScope

    qs = Site.objects.all()
    if site_ids is not None:
        qs = qs.filter(site_id__in=list(site_ids))
    sites = list(qs.values("site_id", "site_type", "installed_site_type", "not_yet_solarized"))
    inv = {}
    for r in FuelSiteInventory.objects.values("site_id", "grid_supply", "dg_count"):
        inv.setdefault((r["site_id"] or "").upper(), r)
    scope = {(s or "").upper(): g for s, g in FuelSiteScope.objects.values_list("site_id", "has_genset")}
    out = {}
    for s in sites:
        key = s["site_id"].upper()
        i = inv.get(key) or {}
        dg = i.get("dg_count")
        if dg is not None:
            ge, ge_src = ("Oui" if dg > 0 else "Non"), "Snowflake SITE_ESCO_CURRENT.DG_COUNT"
        elif key in scope:
            ge, ge_src = _yes_no(scope[key]), "Périmètre fuel (FuelSiteScope.has_genset)"
        else:
            ge, ge_src = UNKNOWN, None
        grid = _grid(i.get("grid_supply"))
        solar = UNKNOWN if s["not_yet_solarized"] is None else ("Non" if s["not_yet_solarized"] else "Oui")
        out[s["site_id"]] = {
            "environnement": _env(s), "raccordement": grid, "ge": ge, "solaire": solar,
            "profil": profile_label(grid, ge, solar),
            "sources": {"environnement": "Gestion des sites (type installé / type)", "raccordement": "Snowflake GRID_SUPPLY_MODIFIED" if i.get("grid_supply") else None,
                        "ge": ge_src, "solaire": "Gestion des sites (sites non encore solarisés)" if solar != UNKNOWN else None},
        }
    return out


def audit_energy_fields() -> list[dict]:
    """Audit des champs réellement disponibles : source, table, colonne, valeurs, couverture, règle, non reconnus."""
    from core.models import Site
    from fuel_tracking.models import FuelSiteInventory, FuelSiteScope, FuelStockSnapshot

    total = Site.objects.count() or 1
    site_ids = {s.upper() for s in Site.objects.values_list("site_id", flat=True)}
    rows = []

    def add(attr, source, table, column, values: Counter, covered: int, rule, unrecognized):
        rows.append({"attribut": attr, "source": source, "table": table, "colonne": column,
                     "valeurs": dict(values.most_common(15)), "couverture": f"{covered}/{total} ({100 * covered / total:.0f} %)",
                     "regle": rule, "non_reconnues": dict(unrecognized.most_common(10))})

    for col in ("installed_site_type", "site_type"):
        vals = Counter(Site.objects.values_list(col, flat=True))
        add("Environnement", "Gestion des sites", "core_site", col, vals, total - vals.get(None, 0) - vals.get("", 0),
            "INDOOR → Indoor, OUTDOOR → Outdoor, vide → Inconnu",
            Counter({k: v for k, v in vals.items() if k and k.upper() not in ("INDOOR", "OUTDOOR")}))
    inv = {(r["site_id"] or "").upper(): r for r in FuelSiteInventory.objects.values("site_id", "grid_supply", "dg_count")}
    gvals = Counter(inv[k]["grid_supply"] for k in site_ids if k in inv)
    add("Raccordement", "Snowflake (lecture seule, synchronisé)", "fuel_tracking_fuelsiteinventory", "grid_supply", gvals,
        sum(v for k, v in gvals.items() if k), "contient OFF → Off-grid ; ON / GRID / SECTEUR → Grid ; sinon Inconnu",
        Counter({k: v for k, v in gvals.items() if k and _grid(k) == UNKNOWN}))
    pvals = Counter(FuelStockSnapshot.objects.filter(snapshot_year__isnull=True).values_list("power_supply", flat=True))
    add("Raccordement (non retenu)", "Snapshot stock (Snowflake)", "fuel_tracking_fuelstocksnapshot", "power_supply", pvals,
        sum(v for k, v in pvals.items() if k), "information seulement (doublon de la source Snowflake)", Counter())
    dvals = Counter(inv[k]["dg_count"] for k in site_ids if k in inv)
    add("GE", "Snowflake (lecture seule, synchronisé)", "fuel_tracking_fuelsiteinventory", "dg_count", dvals,
        sum(v for k, v in dvals.items() if k is not None), "> 0 → Oui ; 0 → Non ; vide → source suivante", Counter())
    svals = Counter(g for s, g in FuelSiteScope.objects.values_list("site_id", "has_genset") if (s or "").upper() in site_ids)
    add("GE (repli)", "Périmètre fuel importé", "fuel_tracking_fuelsitescope", "has_genset", svals, sum(svals.values()),
        "True → Oui ; False → Non", Counter())
    nvals = Counter(Site.objects.values_list("not_yet_solarized", flat=True))
    add("Solaire", "Gestion des sites", "core_site", "not_yet_solarized", nvals, total - nvals.get(None, 0),
        "False (déjà solarisé) → Oui ; True (non encore solarisé) → Non ; vide → Inconnu", Counter())
    return rows
