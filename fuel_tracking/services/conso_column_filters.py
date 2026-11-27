# fuel_tracking/services/conso_column_filters.py
"""
Filtres de colonnes « comme Excel » du tableau Suivi Conso (bouton ▼ de chaque en-tête).

Chaque colonne filtrable est repérée par « bloc.champ » (ex. « estimation_theorique.type_ge ») ou un champ de la ligne
(« site_id », « site_name »). La valeur filtrée est celle AFFICHÉE dans le tableau (lignes enrichies comme à l'affichage,
cache uniquement, aucun appel Snowflake / ENOC), réduite à une clé texte :
  - case vide / absente / liste vide → "" (affichée « (Vides) » : « n.d. » n'est jamais confondu avec 0) ;
  - nombre → au plus 3 décimales, sans zéros inutiles (« 1234.5 ») ; plusieurs GE → valeurs jointes par « / » ;
  - texte → tel quel (espaces normalisés).
Filtre = {colonne: [clés cochées]} : un site est gardé si, pour CHAQUE colonne filtrée, sa clé est cochée.
"""
from __future__ import annotations

from decimal import Decimal

TOP = ("site_id", "site_name")
FILTER_KEYS = TOP + tuple(f"inventaire.{k}" for k in ("region", "indoor_outdoor", "on_grid", "typologie_cible", "typologie_facturee")) \
    + tuple(f"{b}.{k}" for b in ("stock_initial_sources", "stock_final_sources") for k in (
        "date", "terrain_l", "gardiennage_l", "fms_l", "retenue_l", "source", "compteur_terrain_h", "compteur_gardiennage_h",
        "compteur_fms_h", "compteur_retenu_h", "source_compteur")) \
    + tuple(f"ravitaillement.{k}" for k in ("date_finale_fichier", "eydon_l", "carte_total_l", "ajout_l", "retrait_l", "vole_l", "fuite_l",
                                                     "vole_closing_l", "vidange_closing_l", "vole_retenu_l", "vidange_retenu_l")) \
    + tuple(f"estimation_theorique.{k}" for k in (
        "fabricant_ge", "type_ge", "source_ge", "prp_kva", "prp_kw", "tm_dg_on_h", "tm_rectifier_h", "tm_terrain_h", "tm_index_fms_h",
        "tm_dg_on_visibilite_pct", "tm_rectifier_visibilite_pct", "tm_terrain_visibilite_pct", "tm_index_fms_visibilite_pct",
        "tm_retenu_h", "tm_source", "p_rect_kw", "p_ge_kw", "p_grid_kw", "p_rect_min_kw", "p_rect_max_kw", "p_ge_min_kw",
        "p_ge_max_kw", "p_grid_min_kw", "p_grid_max_kw", "p_load_dc_kw", "p_batt_kw", "p_retenue_kw", "p_retenue_source",
        "taux_charge", "cph_l_h", "conso_theorique_l", "conso_fuel_sensor_l", "conso_fuel_sensor_avant_l", "conso_retenue_source")) \
    + tuple(f"analyse_variation.{k}" for k in ("conso_theorique_retenue_l", "variation_l", "grande_difference", "delta_l", "alerte", "tolerance_l")) \
    + tuple(f"analyse_mensuelle.{k}" for k in ("nb_jours", "conso_moyenne_l_j", "estimation_mois_l", "equivalent_n1_l", "delta_l"))
PARAM = "filtres_colonnes"


def _one(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Oui" if v else "Non"
    if isinstance(v, (int, float, Decimal)):
        return f"{float(v):.3f}".rstrip("0").rstrip(".")
    return " ".join(str(v).split())


def vkey(v) -> str:
    """Clé texte de la valeur affichée (voir l'en-tête du module)."""
    if isinstance(v, (list, tuple)):
        parts = [_one(x) for x in v if x is not None]
        return " / ".join(p for p in parts if p)
    return _one(v)


def value(row: dict, key: str):
    if key in TOP:
        return row.get(key)
    bloc, champ = key.split(".", 1)
    return (row.get(bloc) or {}).get(champ)


def index(enriched_rows: list[dict]) -> dict:
    """site → {colonne: clé} pour toutes les colonnes filtrables."""
    return {r["site_id"]: {k: vkey(value(r, k)) for k in FILTER_KEYS} for r in enriched_rows}


def parse(raw) -> dict[str, set[str]]:
    """Paramètre `filtres_colonnes` (JSON {colonne: [clés]}) → {colonne: ensemble} ; colonnes inconnues ignorées."""
    import json

    try:
        d = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError:
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: {str(x) for x in v} for k, v in d.items() if k in FILTER_KEYS and isinstance(v, list)}


def keep(idx: dict, filters: dict[str, set[str]], site_id: str, skip: str | None = None) -> bool:
    vals = idx.get(site_id)
    if vals is None:
        return not filters
    return all(vals.get(k, "") in allowed for k, allowed in filters.items() if k != skip)


def distinct(idx: dict, filters: dict[str, set[str]], site_ids, col: str) -> list[dict]:
    """Valeurs de la colonne `col` (avec leur nombre de sites) parmi les sites retenus par les AUTRES filtres de colonnes,
    comme Excel. Tri : nombres croissants, sinon ordre alphabétique ; « (Vides) » en dernier."""
    counts: dict[str, int] = {}
    for sid in site_ids:
        if keep(idx, filters, sid, skip=col):
            k = (idx.get(sid) or {}).get(col, "")
            counts[k] = counts.get(k, 0) + 1

    def num(s):
        try:
            return float(s)
        except ValueError:
            return None
    vals = [k for k in counts if k != ""]
    if vals and all(num(k) is not None for k in vals):
        vals.sort(key=num)
    else:
        vals.sort(key=str.lower)
    return [{"valeur": k, "nb": counts[k]} for k in vals] + ([{"valeur": "", "nb": counts[""]}] if "" in counts else [])
