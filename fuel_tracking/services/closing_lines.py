"""
Fichier Closing mensuel : conservation LIGNE À LIGNE (FuelClosingLine), target du fichier, contrôle de cohérence des totaux.

Règles :
  - toutes les lignes du fichier sont conservées (sites inconnus, sans région, identifiants en double) : rien n'est écarté en silence, les compteurs
    (lues / importées / non reconnues / en double / sans région) sont rapportés dans le lot ;
  - chaque ligne est rattachée au MOIS COMPTABLE DU FICHIER (« Dernier jour du mois de SEPTEMBRE »), pas à la date de relevé du site ;
  - contrôle : Σ importée ≠ Σ lue ≠ bloc de KPI du fichier → avertissement visible dans le rapport du lot.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal

from fuel_tracking.services import stock_import_files as F

SOURCE_TARGET_FICHIER = "CLOSING_AKT_INV"
TOL_L = 1.0                                       # écart toléré (arrondis du fichier)
SUMS = (("conso_closing_l", "Σ Qtité cons considérée (EK)"), ("stock_initial_l", "Σ stock initial (AT)"),
        ("total_livraison_l", "Σ livraisons (BF)"), ("stock_final_l", "Σ stock final (CJ)"), ("vole_l", "Σ volé (CA)"),
        ("vidange_l", "Σ vidange / fuite / écart (CB)"), ("target_mois_l", "Σ target mensuel (FI)"))
DB_FIELD = {"conso_closing_l": "conso_closing_l", "stock_initial_l": "stock_initial_l", "total_livraison_l": "total_livraison_l",
            "stock_final_l": "stock_final_l", "vole_l": "vole_l", "vidange_l": "vidange_l", "target_mois_l": "target_mois_l"}


def _dec(v):
    return None if v is None else Decimal(str(round(float(v), 4)))


def _dominant_month(data) -> str | None:
    c = Counter(f"{r['date_finale']:%Y-%m}" for r in data if r.get("date_finale"))
    return c.most_common(1)[0][0] if c else None


def file_month(data, diag) -> str | None:
    """Mois comptable du fichier : cellule « Dernier jour du mois de … », sinon mois le plus fréquent des dates de relevé finales."""
    return (diag or {}).get("mois_comptable") or _dominant_month(data)


def _sum(rows, key):
    v = [float(r[key]) for r in rows if r.get(key) is not None]
    return round(sum(v), 1) if v else None


def save_lines(batch, data, diag, resolver) -> dict:
    """Enregistre TOUTES les lignes du fichier. Retourne les compteurs et le contrôle de cohérence."""
    from fuel_tracking.models import FuelClosingLine as L

    mois = file_month(data, diag)
    if mois is None:
        return {"mois": None, "erreur": "Mois comptable du fichier introuvable (ni « Dernier jour du mois de… », ni dates de relevé)."}
    seen: set[str] = set()
    objs = []
    stats = {"mois": mois, "lignes_lues": len(data), "importees": 0, "id_reconnus": 0, "id_non_reconnus": 0, "id_en_double": 0, "sans_region": 0,
             "sans_conso": 0, "sans_date": 0, "non_reconnus_exemples": []}
    for r in data:
        sid = resolver.resolve(r["site"])
        key = (sid or r["site"]).upper()
        dup = key in seen
        seen.add(key)
        stats["id_reconnus" if sid else "id_non_reconnus"] += 1
        stats["id_en_double"] += dup
        stats["sans_region"] += not r.get("region")
        stats["sans_conso"] += r.get("conso_closing_l") is None
        stats["sans_date"] += not (r.get("date_initiale") and r.get("date_finale"))
        if not sid and len(stats["non_reconnus_exemples"]) < 15:
            stats["non_reconnus_exemples"].append(r["site"])
        objs.append(L(
            batch=batch, mois=mois, ligne=r["ligne"], site_raw=r["site"][:128], site_id=(sid or None) and sid.upper(), region=(r.get("region") or None),
            grid=(r.get("grid") or None), nb_ge=_dec(r.get("nb_ge")), date_initiale=r.get("date_initiale"), date_finale=r.get("date_finale"),
            jours=_dec(r.get("jours")), stock_initial_l=_dec(r.get("stock_initial_l")), total_livraison_l=_dec(r.get("total_livraison_l")),
            rajout_retrait_l=_dec(r.get("rajout_retrait_l")), vole_l=_dec(r.get("vole_l")), vidange_l=_dec(r.get("vidange_l")),
            stock_final_l=_dec(r.get("stock_final_l")), conso_closing_l=_dec(r.get("conso_closing_l")), conso_niveau_l=_dec(r.get("conso_niveau_l")),
            conso_capteur_l=_dec(r.get("conso_capteur_l")), estimation_mois_l=_dec(r.get("estimation_mois_l")),
            target_jour_l=_dec(r.get("target_jour_l")), target_mois_l=_dec(r.get("target_mois_l")), analyses=r.get("analyses") or {}, id_en_double=dup))
    L.objects.bulk_create(objs, batch_size=1000)
    stats["importees"] = len(objs)
    stats["controle"] = control_totals(batch, data, diag, mois)
    return stats


def _kpi_pick(kpi: dict, needle: str, nth: int = 0):
    hits = [v for k, v in kpi.items() if needle in k]
    return hits[nth] if len(hits) > nth else None


def control_totals(batch, data, diag, mois) -> dict:
    """Compare : totaux LUS dans le fichier (ligne à ligne) · totaux IMPORTÉS en base · bloc de KPI affiché en haut du fichier."""
    from django.db.models import Sum

    from fuel_tracking.models import FuelClosingLine as L

    kpi = (diag or {}).get("kpi_fichier") or {}
    agg = L.objects.filter(batch=batch).aggregate(**{k: Sum(DB_FIELD[k]) for k, _ in SUMS})
    kpi_ref = {"conso_closing_l": _kpi_pick(kpi, "consommée entre les dates de relevés"),
               "stock_initial_l": _kpi_pick(kpi, "quantité laissée dans les cuves aux dates de relevés", 0),
               "stock_final_l": _kpi_pick(kpi, "quantité laissée dans les cuves aux dates de relevés", 1),
               "vole_l": _kpi_pick(kpi, "probable quantité volée"), "target_mois_l": _kpi_pick(kpi, "target mensuel")}
    checks, ok = [], True
    for key, label in SUMS:
        lu, imp = _sum(data, key), (None if agg[key] is None else round(float(agg[key]), 1))
        ref = kpi_ref.get(key)
        ecart_import = None if lu is None or imp is None else round(imp - lu, 1)
        ecart_kpi = None if lu is None or ref is None else round(lu - float(ref), 1)
        bad = (ecart_import is not None and abs(ecart_import) > TOL_L) or (ecart_kpi is not None and abs(ecart_kpi) > TOL_L)
        ok = ok and not bad
        checks.append({"champ": key, "libelle": label, "lu_fichier_l": lu, "importe_l": imp, "kpi_fichier_l": None if ref is None else round(float(ref), 1),
                       "ecart_import_l": ecart_import, "ecart_kpi_l": ecart_kpi, "ok": not bad})
    return {"ok": ok, "tolerance_l": TOL_L, "totaux": checks,
            "avertissement": None if ok else "Écart entre les totaux du fichier et les totaux importés : vérifier le rapport d'import avant d'utiliser les chiffres."}


def targets_from_lines(batch, data, mois, resolver) -> dict:
    """Target mensuel (FI) par site, repris du fichier Closing (feuille de synthèse). Première occurrence conservée ; remplace le target actif des sites concernés."""
    from fuel_tracking.models import FuelSiteTarget as T

    year = int(mois[:4])
    targets, seen, stats = [], set(), {"lignes_avec_target": 0, "importes": 0, "doublons": 0, "sans_target": 0, "mois": mois}
    for r in data:
        fi = r.get("target_mois_l")
        if fi is None:
            stats["sans_target"] += 1
            continue
        if fi <= 0:
            stats["target_zero"] += 1           # 0 L/mois = pas de target exploitable (pas de division) : comptée à part, jamais importée comme target
            continue
        stats["lignes_avec_target"] += 1
        sid = resolver.resolve(r["site"])
        key = (sid or r["site"]).strip().upper()
        if key in seen:
            stats["doublons"] += 1
            continue
        seen.add(key)
        targets.append(T(site_id=key, site_name=None, region=r.get("region"), typologie=r.get("grid"), target_mensuel_l=Decimal(str(round(fi, 3))),
                         source_target=SOURCE_TARGET_FICHIER, statut_target="TARGET_MENSUEL_FICHIER", reference_year=year,
                         historique={"mois": mois, "target_jour_l": r.get("target_jour_l"), "libelle": "Fuel target [L/mois] du fichier Closing (Akt Inv. Senegal)"},
                         batch=batch, source_sheet="Synthese conso fuel", source_row=r["ligne"]))
    if targets:
        T.objects.filter(is_active=True, site_id__in=[t.site_id for t in targets]).update(is_active=False)
        T.objects.bulk_create(targets, batch_size=1000)
    stats["importes"] = len(targets)
    return stats


def lines_for_month(mois: str) -> list:
    """Lignes de la DERNIÈRE version importée du Closing de ce mois comptable."""
    from django.db.models import Max

    from fuel_tracking.models import FuelClosingLine as L

    last = L.objects.filter(mois=mois).aggregate(m=Max("batch_id"))["m"]
    return [] if last is None else list(L.objects.filter(mois=mois, batch_id=last).order_by("ligne"))


ANALYSES = {
    "1": "Temps de fonctionnement GE supérieur à l'indisponibilité Grid",
    "2": "Test de conformité des données de stock",
    "3": "Grande différence conso observée / conso théorique",
    "4": "Grande différence estimation capteur / conso sur la période de relevé",
    "5": "Grande différence L/jour observé / L/jour théorique du design",
    "6": "Grande différence consommation du mois précédent / mois actuel",
    "7": "Conso supérieure à la conso maximale théorique",
    "8": "Grande différence énergie Grid théorique / Energy Desk",
    "9": "Grande différence énergie théorique / Energy Desk",
}
RAS = {"", "ok", "non", "n.a", "n.a.", "na", "none", "0", "0.0"}     # valeurs du fichier qui ne signalent rien (« ok », « Non », « n.a »)


def _f(v):
    return None if v is None else float(v)


def _tot(rows, attr):
    vals = [float(getattr(r, attr)) for r in rows if getattr(r, attr) is not None]
    return {"total_l": round(sum(vals), 1) if vals else None, "sites": len(vals)}


def _par_region(rows) -> list:
    """Conso Closing par région du fichier ; les lignes sans région restent visibles (« Sans région »), jamais écartées."""
    acc: dict = {}
    for r in rows:
        if r.conso_closing_l is None:
            continue
        k = (r.region or "").strip().upper() or "Sans région"
        a = acc.setdefault(k, {"region": k, "conso_l": 0.0, "sites": 0})
        a["conso_l"] += float(r.conso_closing_l)
        a["sites"] += 1
    return sorted(({**a, "conso_l": round(a["conso_l"], 1)} for a in acc.values()), key=lambda a: -a["conso_l"])


def _top_sites(rows, n=10) -> list:
    pos = [r for r in rows if r.conso_closing_l is not None and float(r.conso_closing_l) > 0]
    pos.sort(key=lambda r: -float(r.conso_closing_l))
    return [{"site": (r.site_id or r.site_raw), "region": (r.region or None), "conso_l": round(float(r.conso_closing_l), 1),
             "jours": float(r.jours) if r.jours is not None else None} for r in pos[:n]]


def synthese(mois: str) -> dict:
    """Agrégats du Closing d'un mois comptable, tels que dans le fichier (aucune valeur calculée hors des formules du fichier)."""
    from fuel_tracking.services.fuel_target import tolerance_pct

    rows = lines_for_month(mois)
    if not rows:
        return {"mois": mois, "disponible": False,
                "motif": f"Aucun fichier Closing importé pour {mois} (feuille « Synthese conso fuel »).",
                "action": {"libelle": f"Importer le Closing de {mois}", "cible": "suivi-stock/imports"}}
    batch = rows[0].batch
    ge = [r for r in rows if r.nb_ge is not None and float(r.nb_ge) >= 1]
    conso = _tot(rows, "conso_closing_l")
    ini, liv, fin = _tot(rows, "stock_initial_l"), _tot(rows, "total_livraison_l"), _tot(rows, "stock_final_l")
    vol, vid = _tot(rows, "vole_l"), _tot(rows, "vidange_l")
    both = [r for r in rows if r.target_mois_l is not None and float(r.target_mois_l) > 0 and r.conso_closing_l is not None]
    tol = tolerance_pct()
    t_sum = sum(float(r.target_mois_l) for r in both)
    c_sum = sum(float(r.conso_closing_l) for r in both)
    over = [r for r in both if float(r.conso_closing_l) > float(r.target_mois_l) * (1 + tol / 100)]
    analyses = []
    for k, label in ANALYSES.items():
        cnt = Counter(str(r.analyses.get(k)).strip() if r.analyses.get(k) not in (None, "") else "(vide)" for r in rows)
        vals = [{"valeur": v, "nb": n, "a_verifier": v.strip().lower() not in RAS and v != "(vide)"} for v, n in cnt.most_common(8)]
        analyses.append({"code": k, "libelle": label, "valeurs": vals, "a_verifier": sum(x["nb"] for x in vals if x["a_verifier"])})
    s = (batch.summary or {})
    delta = None if ini["total_l"] is None or fin["total_l"] is None else round(ini["total_l"] - fin["total_l"], 1)
    variation = None if None in (ini["total_l"], liv["total_l"], fin["total_l"]) else round(ini["total_l"] + liv["total_l"] - fin["total_l"], 1)
    return {
        "mois": mois, "disponible": True,
        "fichier": {"lot": batch.id, "nom": batch.file_name, "importe_le": batch.received_at.isoformat() if batch.received_at else None},
        "lignes": {"lues": len(rows), "id_reconnus": sum(1 for r in rows if r.site_id), "id_non_reconnus": sum(1 for r in rows if not r.site_id),
                   "id_en_double": sum(1 for r in rows if r.id_en_double), "sans_region": sum(1 for r in rows if not r.region),
                   "sans_conso": sum(1 for r in rows if r.conso_closing_l is None)},
        "controle": (s.get("lignes_fichier") or {}).get("controle"),
        "conso_closing": {**conso, "lignes": len(rows), "avec_ge": _tot(ge, "conso_closing_l"), "sites_conso_positive": sum(1 for r in rows if (r.conso_closing_l or 0) > 0)},
        "stock": {"initial_l": ini["total_l"], "livraisons_l": liv["total_l"], "final_l": fin["total_l"], "delta_stock_l": delta, "variation_stock_l": variation,
                  "formule": "stock initial (AT) + livraisons (BF) − stock final (CJ) ; la consommation du fichier retranche en plus le volé (CA) et les vidanges/écarts (CB)",
                  "avec_ge": {"initial_l": _tot(ge, "stock_initial_l")["total_l"], "final_l": _tot(ge, "stock_final_l")["total_l"]}},
        "vols": {**vol, "sites_concernes": sum(1 for r in rows if (r.vole_l or 0) > 0)},
        "vidange_fuite_ecart": {**vid, "sites_concernes": sum(1 for r in rows if (r.vidange_l or 0) > 0)},
        "capteur": {**_tot(rows, "conso_capteur_l"), "sites_conso_positive": sum(1 for r in rows if (r.conso_capteur_l or 0) > 0)},
        "target": {"total_mois_l": round(sum(float(r.target_mois_l) for r in rows if r.target_mois_l is not None), 1), "sites": sum(1 for r in rows if r.target_mois_l is not None),
                   "sites_comparables": len(both), "conso_comparable_l": round(c_sum, 1), "target_comparable_l": round(t_sum, 1),
                   "atteinte_pct": round(c_sum / t_sum * 100, 1) if t_sum else None, "tolerance_pct": tol,
                   "sites_au_dessus": len(over), "source": "Fuel target [L/mois] du fichier Closing (Akt Inv. Senegal)"},
        "statuts": analyses,
        "par_region": _par_region(rows),
        "top_sites": _top_sites(rows),
    }
