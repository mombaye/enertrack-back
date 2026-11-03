"""Alimente les tables relationnelles Pilotage Grid depuis les jeux de données actifs (rejoué à chaque import)."""
from __future__ import annotations

from django.db import transaction

from pilotage_grid.models import PgInvoice, PgInvoiceFact, PgSite, PgSiteMonth
from pilotage_grid.services import store as S


def tension_of(tarif: str) -> str:
    return "MT" if str(tarif).startswith("MT") else "BT"


@transaction.atomic
def rebuild_paiements() -> dict:
    ds = S.active("paiements")
    if ds is None:
        return {"invoice_fact": 0, "invoice": 0}
    P, L = ds.data, ds.data["lists"]
    PgInvoiceFact.objects.all().delete()
    PgInvoice.objects.all().delete()
    if not all(k in L for k in ("por", "ech", "cpt", "reg", "tar")):
        return {"invoice_fact": 0, "invoice": 0, "avertissement": "listes de référence incomplètes : tables non alimentées"}
    PgInvoiceFact.objects.bulk_create([
        PgInvoiceFact(statut=r[0], porteur=L["por"][r[1]], echeance=L["ech"][r[2]], comptable=L["cpt"][r[3]], region=L["reg"][r[4]], tarif=L["tar"][r[5]],
                      nb=r[6], ttc=r[7], ht=r[8] if len(r) > 8 else 0)
        for r in P["cube"]], batch_size=2000)
    PgInvoice.objects.bulk_create([
        PgInvoice(numero=r[0], contrat=r[1], site_id=str(r[2]), site_name=r[3] or "", date_comptable=r[4], ttc=r[5], statut=r[6], porteur=r[7], localisation=r[8] or "",
                  region=r[9], tarif=r[10])
        for r in P["det"]], batch_size=2000)
    return {"invoice_fact": len(P["cube"]), "invoice": len(P["det"])}


@transaction.atomic
def rebuild_sites() -> dict:
    ds = S.active("sites")
    if ds is None:
        return {"site": 0, "site_month": 0}
    Q = ds.data
    if not Q.get("sites") or not isinstance(Q.get("ser"), list):
        PgSiteMonth.objects.all().delete()
        PgSite.objects.all().delete()
        return {"site": 0, "site_month": 0}
    months, reps = Q["months"], {r[0] for r in Q["reps"]}
    PgSiteMonth.objects.all().delete()
    PgSite.objects.all().delete()
    sites = []
    for s in Q["sites"]:
        sites.append(PgSite(site_key=str(s[0]), name=s[1] or "", region=s[2] or "", tarif=s[3] or "", tension=s[4] or tension_of(s[3]), puissance_kw=s[5], porteur=s[10] or "",
                            contrat=s[11], derniere_facture=s[16] or "", changement_contrat=s[0] in reps))
    PgSite.objects.bulk_create(sites, batch_size=2000)
    by_key = {p.site_key: p.pk for p in PgSite.objects.only("id", "site_key")}
    rows = []
    for i, s in enumerate(Q["sites"]):
        x = Q["ser"][i]
        if not x:
            continue
        start, ht, ttc, k1, k2, jours, imp = x[0], x[1], x[2], x[3], x[4], x[5], x[6]
        pk = by_key[str(s[0])]
        for j in range(len(ht)):
            m = start + j
            if 0 <= m < len(months):
                rows.append(PgSiteMonth(site_id=pk, month=months[m], ht=ht[j], ttc=ttc[j], k1=k1[j], k2=k2[j], jours=jours[j], imp=imp[j]))
    PgSiteMonth.objects.bulk_create(rows, batch_size=5000)
    return {"site": len(sites), "site_month": len(rows)}


def rebuild_all() -> dict:
    return {**rebuild_paiements(), **rebuild_sites()}
