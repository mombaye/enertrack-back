# scripts/diag_ravitaillement.py — LECTURE SEULE (aucune écriture ; aucun secret affiché)
"""
Contrôle du bloc « DONNÉES DE RAVITAILLEMENT » du Suivi Conso : les 4 totaux ENOC du tableau (EYDON, Carte Total, Ajout,
Retrait) sont recomptés indépendamment, directement dans les mouvements ENOC, entre la date de relevé initiale et la date
de relevé finale de chaque site ; volé et fuite / écart sont comparés à la ligne du fichier de clôture.

Usage (prod) :
  docker compose exec -T web python manage.py shell < scripts/diag_ravitaillement.py
Mois contrôlé : DIAG_MOIS=AAAA-MM (défaut : 2026-10).
"""
import os
from collections import defaultdict
from datetime import date, timedelta

from fuel_tracking.models import FuelEnocQualifiedMovement as EQ, FuelMovement as MV, FuelStockBalance as B
from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.stock_initial_sources import ravitaillement

y, m = map(int, os.environ.get("DIAG_MOIS", "2026-10").split("-"))
d_from, d_to = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
rows = C.build(d_from, d_to, {}, with_cph_quality=False)["rows"]
ravitaillement(rows)
bounds = {pk: (t0, t1) for pk, t0, t1 in B.objects.filter(pk__in=[r["stock"]["bilan_id"] for r in rows if r["stock"]["bilan_id"]])
          .values_list("pk", "date_stock_initial", "date_stock_final")}

# Recomptage indépendant, directement dans ENOC (même règle : t0 < date ≤ t1)
indep = defaultdict(lambda: defaultdict(float))
sites = {r["site_id"]: bounds.get(r["stock"]["bilan_id"]) for r in rows if r["stock"]["date_initial"] and r["stock"]["date_final"]}
for q in EQ.objects.filter(site_id_resolved__in=list(sites), integration_status="INTEGRE").exclude(type_family="PONCTION"):
    t0, t1 = sites.get(q.site_id_resolved) or (None, None)
    when = q.accounting_at or q.actual_operation_at
    if t0 and t1 and when and t0 < when <= t1 and q.integrated_quantity_l is not None:
        indep[q.site_id_resolved]["carte_total_l" if q.type_family == "TOTAL_CARD" else "eydon_l"] += float(q.integrated_quantity_l)
for mv in MV.objects.filter(site_id__in=list(sites), source_type="ENOC", retained_in_stock=True,
                            movement_type__in=("TRANSFERT_INTERSITE_SORTANT", "TRANSFERT_INTERSITE_ENTRANT")):
    t0, t1 = sites.get(mv.site_id) or (None, None)
    if t0 and t1 and mv.movement_datetime and t0 < mv.movement_datetime <= t1:
        indep[mv.site_id]["ajout_l" if mv.movement_type.endswith("ENTRANT") else "retrait_l"] += abs(float(mv.impact_stock_l or 0))

keys = ("eydon_l", "carte_total_l", "ajout_l", "retrait_l")
tot = {k: round(sum(r["ravitaillement"][k] or 0 for r in rows)) for k in keys + ("vole_l", "fuite_l")}
print(f"== Mois {d_from:%Y-%m} : {len(rows)} sites ; {len(sites)} avec une période (date initiale et finale)")
print("Totaux affichés :", tot)
print("Totaux recomptés dans ENOC :", {k: round(sum(v[k] for v in indep.values())) for k in keys})
diff = [(r["site_id"], k, r["ravitaillement"][k], round(indep[r["site_id"]][k], 3)) for r in rows if r["site_id"] in sites for k in keys
        if abs((r["ravitaillement"][k] or 0) - indep[r["site_id"]][k]) > 0.01]
print("Écarts site par site (tableau ≠ ENOC) :", len(diff), diff[:20])
print("Sites avec au moins un mouvement ENOC :", sum(1 for r in rows if r["ravitaillement"]["nb_mouvements_enoc"]))
print("Volé / fuite — source :", {s: sum(1 for r in rows if r["ravitaillement"]["source_vol_fuite"] == s) for s in {r["ravitaillement"]["source_vol_fuite"] for r in rows}})
for r in [r for r in rows if r["ravitaillement"]["nb_mouvements_enoc"]][:5]:
    print("  ex.", r["site_id"], r["stock"]["date_initial"], "→", r["stock"]["date_final"], {k: r["ravitaillement"][k] for k in keys + ("vole_l", "fuite_l")})
