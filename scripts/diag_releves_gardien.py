# scripts/diag_releves_gardien.py — LECTURE SEULE (ENOC et base EnerTrack jamais modifiés ; aucun secret affiché)
"""
Vérifie la colonne « Qtité laissée à la date initiale (Société de gardiennage) » du Suivi Conso :
pour chaque site du mois, existe-t-il dans ENOC (partie « Relevé de gardien », collection fuel_level_readings)
un relevé de CE site fait EXACTEMENT à la date de relevé initiale du fichier ?

Usage (prod) :
  docker compose exec -T web python manage.py shell < scripts/diag_releves_gardien.py
Mois contrôlé : variable d'environnement DIAG_MOIS=AAAA-MM (défaut : 2026-10).
"""
import os
from collections import Counter
from datetime import date, datetime, timedelta

from django.conf import settings

from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.enoc_mongo_service import _connect
from fuel_tracking.services.stock_initial_sources import _enoc_day, _parse_guard_doc

y, m = map(int, os.environ.get("DIAG_MOIS", "2026-10").split("-"))
d_from = date(y, m, 1)
d_to = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
rows = C.build(d_from, d_to, {}, with_cph_quality=False)["rows"]
pairs = {(r["site_id"], date.fromisoformat(str(r["stock"]["date_initial"])[:10])) for r in rows if r["stock"].get("date_initial")}
print(f"== Mois {d_from:%Y-%m} : {len(rows)} sites, {len(pairs)} avec une date de relevé initiale")
print("Dates de relevé initiale (top 5) :", Counter(d for _, d in pairs).most_common(5))

client = _connect()
try:
    db = client[settings.ENOC_MONGO_DB_NAME]
    names = sorted(db.list_collection_names())
    print("\n== Collections ENOC liées aux relevés :", [n for n in names if any(k in n.lower() for k in ("releve", "reading", "gardien", "guard", "level"))])
    col = db.fuel_level_readings
    print("fuel_level_readings : total", col.estimated_document_count())
    last = col.find({}, {"date_releve": 1}).sort("date_releve", -1).limit(1)
    print("Relevé de gardien le plus récent dans ENOC :", next((d.get("date_releve") for d in last), None))
    days = sorted({d for _, d in pairs})
    if days:
        lo, hi = days[0] - timedelta(days=3), days[-1] + timedelta(days=4)
        docs = list(col.find({"$or": [{"date_releve": {"$gte": lo.isoformat(), "$lt": hi.isoformat()}},
                                     {"date_releve": {"$gte": datetime.combine(lo, datetime.min.time()), "$lt": datetime.combine(hi, datetime.min.time())}}]}))
        print(f"Relevés ENOC entre {lo} et {hi} (tous sites) : {len(docs)} ; par jour :", sorted(Counter(_enoc_day(d.get("date_releve")) for d in docs).items()))
        if docs:
            print("Champs d'un relevé (noms seulement) :", sorted(docs[0].keys()), "| cuves :", sorted((docs[0].get("tanks") or [{}])[0].keys()))
        exact = {}
        for d in docs:
            k = (d.get("site_id"), _enoc_day(d.get("date_releve")))
            if k in pairs:
                exact.setdefault(k, []).append(d)
        print(f"\n== Sites avec un relevé de gardien ENOC le JOUR EXACT de la date initiale : {len(exact)} / {len(pairs)}")
        for (sid, day), lst in sorted(exact.items())[:30]:
            lst.sort(key=lambda d: str(d.get("date_releve")))
            v = _parse_guard_doc(lst[-1])
            print(f"  {sid} {day} : {len(lst)} relevé(s), retenu {lst[-1].get('date_releve')} → {v['litres']} L, compteur {v['compteur']}")
finally:
    client.close()

# Pour comparaison : ce que l'ancienne version affichait (fichiers Loliforce/SCSS, PAS ENOC)
from fuel_tracking.models import FuelStockObservation as Obs  # noqa: E402
old = Obs.objects.filter(source_type__in=("LOLIFORCE", "SCSS"), site_id__in={s for s, _ in pairs}).values_list("site_id", "observation_datetime")
print("\n== Ancienne source (fichiers Loliforce/SCSS, jour exact) :", sum(1 for s, dt in old if (s, dt.date()) in pairs), "site(s) — n'est plus utilisée")
