# scripts/seed_tariffs.py
# Exécuter via : python manage.py shell < scripts/seed_tariffs.py
# ou             python manage.py runscript seed_tariffs  (si django-extensions installé)
#
# Met à jour TOUS les tarifs selon le CDC (tableau Tarifs Energie).
# Utilise update_or_create sur (category, date_debut) → pas de doublon.

from datetime import date
from decimal import Decimal
from billing.models import TariffRate

DATE_DEBUT = date(2000, 1, 1)
DATE_FIN   = date(9999, 12, 31)

# (category, k1, k2, k3_or_None, prime_fixe_or_None)
TARIFS_CDC = [
    # ── Polices MT / HTA (pas de K3, pas de LCTR) ──────────────────────────
    ("MTLU", Decimal("91.93"),  Decimal("151.72"), None,            Decimal("9881")),
    ("MTCU", Decimal("155.50"), Decimal("248.28"), None,            Decimal("962")),
    ("MTG",  Decimal("111.91"), Decimal("184.65"), None,            Decimal("4094")),
    ("PFP",  Decimal("140.74"), Decimal("232.23"), None,            Decimal("2868")),
    ("PGP",  Decimal("140.74"), Decimal("232.23"), None,            Decimal("2868")),
    ("DGP",  Decimal("118.37"), Decimal("170.53"), None,            Decimal("956")),
    # ── Polices BT domestiques (K3 distinct, prime fixe = 0) ────────────────
    ("PMP",  Decimal("165.00"), Decimal("191.01"), Decimal("210.81"), None),
    ("PPP",  Decimal("163.80"), Decimal("189.84"), Decimal("208.63"), None),
    ("DMP",  Decimal("111.23"), Decimal("143.54"), Decimal("158.46"), None),
    ("DPP",  Decimal("91.17"),  Decimal("136.49"), Decimal("159.36"), None),
]

print("=" * 60)
print("Seed tarifs CDC — billing.TariffRate")
print("=" * 60)

for cat, k1, k2, k3, prime_fixe in TARIFS_CDC:
    obj, created = TariffRate.objects.update_or_create(
        category=cat,
        date_debut=DATE_DEBUT,
        defaults={
            "date_fin":    DATE_FIN,
            "energie_k1":  k1,
            "energie_k2":  k2,
            "energie_k3":  k3,          # None pour polices sans K3
            "prime_fixe":  prime_fixe or Decimal("0"),
        },
    )
    action = "CRÉÉ" if created else "MIS À JOUR"
    print(f"  [{action}] {cat:6s}  K1={k1:8.2f}  K2={k2:8.2f}  K3={str(k3):8s}  PF={prime_fixe}")

print()
print(f"✓ {len(TARIFS_CDC)} tarifs traités.")
print()

# Vérification finale
print("État final de la table TariffRate :")
print(f"{'Police':<8} {'K1':>8} {'K2':>8} {'K3':>8} {'Prime fixe':>12} {'Période'}")
print("-" * 65)
for tr in TariffRate.objects.order_by("category"):
    print(
        f"{tr.category:<8} "
        f"{float(tr.energie_k1 or 0):>8.2f} "
        f"{float(tr.energie_k2 or 0):>8.2f} "
        f"{str(round(float(tr.energie_k3), 2) if tr.energie_k3 else '—'):>8} "
        f"{float(tr.prime_fixe or 0):>12.2f} "
        f"  {tr.date_debut} → {tr.date_fin}"
    )