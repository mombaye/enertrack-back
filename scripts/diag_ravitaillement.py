# scripts/diag_ravitaillement.py — LECTURE SEULE (aucune écriture ; aucun secret affiché)
"""
Contrôle du bloc « DONNÉES DE RAVITAILLEMENT » du Suivi Conso : les 6 totaux du tableau (EYDON, Carte Total, Ajout, Retrait,
Volé, Vidangé / Fuite / Écart) sont recomptés indépendamment, directement dans ENOC, sur la plage « Date de Relevé
Initiale » → « Date de Relevé Finale » de la ligne du fichier de chaque site (contexte de la ligne, relu ici séparément).

Usage (prod) :
  docker compose exec -T web python manage.py shell < scripts/diag_ravitaillement.py
Mois contrôlé : DIAG_MOIS=AAAA-MM (défaut : 2026-10).
"""
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

from django.utils import timezone

from fuel_tracking.models import FuelEnocQualifiedMovement as EQ, FuelMovement as MV, FuelStockBalance as B
from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.stock_enoc import check_transfer_balance
from fuel_tracking.services.stock_initial_sources import ravitaillement

y, m = map(int, os.environ.get("DIAG_MOIS", "2026-10").split("-"))
d_from, d_to = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
rows = C.build(d_from, d_to, {}, with_cph_quality=False)["rows"]
ravitaillement(rows)

# Dates de la ligne du fichier (relues dans le détail du bilan, indépendamment du code de l'écran)
tz = timezone.get_current_timezone()
at = lambda s: timezone.make_aware(datetime.combine(date.fromisoformat(str(s)[:10]), datetime.min.time()), tz)  # noqa: E731
sites = {}
for r in rows:
    b = B.objects.filter(pk=r["stock"]["bilan_id"]).values("stock_initial_detail", "stock_final_detail").first() if r["stock"]["bilan_id"] else None
    ctx = next((x.get("contexte_source") for x in ((b or {}).get("stock_final_detail"), (b or {}).get("stock_initial_detail"))
                if isinstance(x, dict) and x.get("source") == "CLOSING" and isinstance(x.get("contexte_source"), dict)), None)
    per = (ctx or {}).get("periode") or [r["stock"]["date_initial"], r["stock"]["date_final"]]
    if per[0] and per[1]:
        sites[r["site_id"]] = (at(per[0]), at(per[1]))

indep = defaultdict(lambda: defaultdict(float))
for q in EQ.objects.filter(site_id_resolved__in=list(sites), integration_status="INTEGRE").exclude(type_family="PONCTION"):
    t0, t1 = sites[q.site_id_resolved]
    when = q.accounting_at or q.actual_operation_at
    if when and t0 < when <= t1 and q.integrated_quantity_l is not None:
        indep[q.site_id_resolved]["carte_total_l" if q.type_family == "TOTAL_CARD" else "eydon_l"] += float(q.integrated_quantity_l)
unbalanced = {b["transfer_id"] for b in check_transfer_balance()}
key = {"TRANSFERT_INTERSITE_ENTRANT": "ajout_l", "TRANSFERT_INTERSITE_SORTANT": "retrait_l", "THEFT": "vole_l", "DRAIN": "fuite_l"}
for mv in MV.objects.filter(site_id__in=list(sites), source_type="ENOC", retained_in_stock=True, movement_type__in=list(key)).exclude(status="REJECTED"):
    t0, t1 = sites[mv.site_id]
    if mv.movement_datetime and t0 < mv.movement_datetime <= t1 and not (mv.movement_type.startswith("TRANSFERT") and mv.transfer_id in unbalanced):
        q = mv.impact_stock_l if mv.movement_type.startswith("TRANSFERT") and mv.impact_stock_l is not None else mv.quantity_l
        indep[mv.site_id][key[mv.movement_type]] += abs(float(q or 0))

keys = ("eydon_l", "carte_total_l", "ajout_l", "retrait_l", "vole_l", "fuite_l")
print(f"== Mois {d_from:%Y-%m} : {len(rows)} sites ; {len(sites)} avec les dates de la ligne du fichier")
print("Totaux affichés :", {k: round(sum(r["ravitaillement"][k] or 0 for r in rows)) for k in keys})
print("Totaux recomptés dans ENOC :", {k: round(sum(v[k] for v in indep.values())) for k in keys})
diff = [(r["site_id"], k, r["ravitaillement"][k], round(indep[r["site_id"]][k], 3)) for r in rows if r["site_id"] in sites for k in keys
        if abs((r["ravitaillement"][k] or 0) - indep[r["site_id"]][k]) > 0.01]
print("Écarts site par site (tableau ≠ ENOC) :", len(diff), diff[:20])
print("Sites avec au moins un mouvement ENOC :", sum(1 for r in rows if r["ravitaillement"]["nb_mouvements_enoc"]))
print("Sites sans dates de fichier (cases vides) :", [r["site_id"] for r in rows if r["ravitaillement"]["debut"] is None][:20])
for r in [r for r in rows if r["ravitaillement"]["nb_mouvements_enoc"]][:5]:
    print("  ex.", r["site_id"], r["ravitaillement"]["debut"], "→", r["ravitaillement"]["fin"], {k: r["ravitaillement"][k] for k in keys})
