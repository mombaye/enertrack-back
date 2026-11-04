"""Données SYNTHÉTIQUES pour la démo locale de l'interface (aucune donnée de production). Lancé par scripts/demo_ui.sh."""
import os, django
os.environ["DJANGO_SETTINGS_MODULE"] = "demo_settings"
django.setup()
from datetime import datetime, timezone as tz
from django.contrib.auth import get_user_model
from core.models import Site
from fuel_tracking.tests_stock_period_balance import MvpImportTests
from fuel_tracking.tests_stock_registry import xlsx, tank
from fuel_tracking.services import stock_import_pipeline as P, stock_period_balance as SB, enoc_canonical as C, stock_enoc as E

t = MvpImportTests("test_closing_stocks_import_idempotent_and_rules")
t.setUp()
for sid, name in [("VRD_0001", "VERT-PRET"), ("VRD_0002", "ORANGE-COMPLEMENT"), ("VRD_0003", "ROUGE-CONFLIT"), ("VRD_0004", "GRIS-SANS-STOCK"),
                  ("VRD_0005", "CLOSING-INCOHERENT"), ("KLD_2035", "CUVE-PROPOSEE"), ("KLD_1259", "CUVE-SANS-PLAQUE")]:
    Site.objects.get_or_create(site_id=sid, defaults=dict(name=name, zone="DKR", country="sen", installed_typology="A_Ax_GG GE"))
head = ["Id du site", "Nombre de GE installé [ex: 1,2,3 …]", "Forme géométrique cuve", "Date de relevé initiale", "Qtité laissée à la date du relevé", "Qtité totale livrée",
        "Qté rajoutée ou retirée de la cuve par les techniciens d' un site à l' autre", "Qté volée", "Qté vidangée (fuel de mauvaise qualité)", "Date de relevé finale",
        "Qtité laissée à la date du relevé", "Qtité cons considéréé (l)", "Test date"]
d1, d2 = datetime(2026, 8, 1), datetime(2026, 8, 31)
rows = [["Synthèse"], head,
        ["VRD_0001", 1, None, d1, 500, 200, 0, 0, 0, d2, 300, 400, None],
        ["VRD_0002", 1, None, d1, 800, 300, 0, 0, 0, d2, 500, 600, None],
        ["VRD_0003", 1, None, d1, 600, 0, 0, 0, 0, d2, 100, 500, None],
        ["VRD_0005", 1, None, d1, 800, 0, 0, 0, 0, d2, 600, 500, None],
        ["XXX_9999", 1, None, d1, 100, 0, 0, 0, 0, d2, 50, 50, None]]
P.process_bytes(xlsx({"Synthese conso fuel": rows}), "Closing_Synthese_Conso_Fuel_Aout_2026_SYNTHETIQUE.xlsx", "CLOSING", origin="UPLOAD")
HEAD = ["Site ID", "Site Name", "Catégorie", "Zone", "Ville", "MARQUE GE", "TYPE DE GE", "PUISSANCE GE", "Contact agent", "Relevé en CM", "Relevé en litres",
        "Compteur horaire (h)", "Date de relevé", "Commentaires", "Site gardé par"]
# Loliforce synthétique avec rejets (site inconnu, niveau manquant, date impossible)
from fuel_tracking.tests_stock_registry import HEAD as GH, guard_row
P.process_bytes(xlsx({"Remonte carburant 20 septembre ": [["SEMAINE 40"], [], GH] + [guard_row("VRD_0004", cm=40, d=datetime(2026, 10, 3)), guard_row("XXX_7777", cm=12, d=datetime(2026, 10, 3)),
                 guard_row("VRD_0004", cm=None, l=None, d=datetime(2026, 10, 4)), guard_row("VRD_0004", cm=15, d=datetime(2027, 6, 14))]}),
                "REMONTE CARBURANT OCTOBRE 2026 SYNTHETIQUE.xlsx", "SCSS", origin="UPLOAD")
# ENOC : VRD_0001 concordant (200 L), VRD_0003 en conflit (700 L, Closing 0) ; Ponction sans site source
for code, site, qty, day in (("TR-V1", "VRD_0001", 200, 15), ("TR-V3", "VRD_0003", 700, 20)):
    m = t.mk(code, typ="TRUCK", site=site, qty=qty, req_extra={"level_before": 100, "level_before_unit": "L"},
             op_extra={"level_after": 300, "level_after_unit": "L", "operation_date": datetime(2026, 8, day, tzinfo=tz.utc).isoformat()})
    C.sync_qualified([m])
m = t.mk("PO-V1", typ="PONCTION", status="validated", operation=False, site="VRD_0004",
         req_extra={"approved_quantity_liters": 300, "action": {"validated_at": datetime(2026, 8, 12, tzinfo=tz.utc).isoformat(), "validated_by": "FM"}})
C.sync_qualified([m]); E.sync_enoc_to_registry()
tank("VRD_0004", plate=None)
tank("KLD_2035", status="PROPOSEE", plate=None, compat_score=85)
tank("KLD_1259", plate=None)
SB.compute_balances(datetime(2026, 8, 1, tzinfo=tz.utc), datetime(2026, 8, 31, 23, 59, 59, tzinfo=tz.utc))
U = get_user_model()
u, _ = U.objects.get_or_create(username="demo", defaults=dict(email="demo@example.sn", role="admin", is_staff=True, is_superuser=True))
u.set_password("demo-egrid-123"); u.save()
from fuel_tracking.models import FuelStockBalance as B
print({b.site_id: (b.statut_bilan, b.cause_principale, b.score_confiance) for b in B.objects.all()})
