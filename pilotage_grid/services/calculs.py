"""
Calculs Pilotage Grid côté serveur (SQL sur les tables relationnelles) — mêmes règles que l'interface d'origine.

Filtres communs : région, porteur, catégorie tarifaire, tension (BT/MT), période [de, à] en mois de consommation.
Les factures (cube) sont filtrées sur le mois COMPTABLE compris entre M[de+1] et M[à+1] (règle d'origine : la date comptable suit le mois consommé).
"""
from __future__ import annotations

from collections import defaultdict

from django.db.models import Count, Q, Sum

from pilotage_grid.models import PgInvoice, PgInvoiceFact, PgSite, PgSiteMonth

ST_LBL = ["Payée", "Annulée", "Impayée", "Hors scope / remplacée / annulée (AZ)", "Sans statut (Impayées/Rempl/Hors Scope)"]
DAYS = 30.4375


class Filters:
    def __init__(self, months: list[str], params):
        g = lambda k: (params.get(k) or "").strip()  # noqa: E731
        self.months = months
        n = len(months)
        self.reg, self.por, self.tar, self.ten = g("region"), g("porteur"), g("tarif"), g("tension")
        default_from = months.index("2023-07") if "2023-07" in months else 0
        default_to = max(0, n - 2)

        def idx(v, d):
            return months.index(v) if v in months else d

        self.a = idx(g("from"), default_from)
        self.b = idx(g("to"), default_to)
        if self.b < self.a:
            self.a, self.b = self.b, self.a
        self.ma = months[min(n - 1, self.a + 1)]
        self.mb = months[min(n - 1, self.b + 1)]

    def tarif_q(self, prefix: str = "") -> Q:
        q = Q()
        if self.tar:
            q &= Q(**{f"{prefix}tarif": self.tar})
        if self.ten == "MT":
            q &= Q(**{f"{prefix}tarif__startswith": "MT"})
        elif self.ten == "BT":
            q &= ~Q(**{f"{prefix}tarif__startswith": "MT"})
        return q

    def sites(self):
        qs = PgSite.objects.all()
        if self.reg:
            qs = qs.filter(region=self.reg)
        if self.por:
            qs = qs.filter(porteur=self.por)
        if self.tar:
            qs = qs.filter(tarif=self.tar)
        if self.ten:
            qs = qs.filter(tension=self.ten)
        return qs

    def facts(self):
        qs = PgInvoiceFact.objects.filter(comptable__gte=self.ma, comptable__lte=self.mb)
        if self.reg:
            qs = qs.filter(region=self.reg)
        if self.por:
            qs = qs.filter(porteur=self.por)
        return qs.filter(self.tarif_q())

    def invoices(self):
        qs = PgInvoice.objects.filter(date_comptable__gte=self.ma, date_comptable__lte=f"{self.mb}-99")
        if self.reg:
            qs = qs.filter(region=self.reg)
        if self.por:
            qs = qs.filter(porteur=self.por)
        return qs.filter(self.tarif_q())


def months_list() -> list[str]:
    return list(PgSiteMonth.objects.order_by("month").values_list("month", flat=True).distinct())


def facets() -> dict:
    months = months_list()
    cube = PgInvoiceFact.objects
    regs = sorted(set(cube.values_list("region", flat=True)) | set(PgSite.objects.values_list("region", flat=True)))
    pors = sorted(set(cube.values_list("porteur", flat=True)) | set(PgSite.objects.values_list("porteur", flat=True)))
    tars = sorted(set(cube.values_list("tarif", flat=True)) | set(PgSite.objects.values_list("tarif", flat=True)))
    n = len(months)
    return {"months": months, "regions": [r for r in regs if r], "porteurs": [p for p in pors if p], "tarifs": [t for t in tars if t],
            "default_from": "2023-07" if "2023-07" in months else (months[0] if months else None), "default_to": months[max(0, n - 2)] if months else None}


def label_of(site: PgSite) -> tuple[str, str]:
    k = site.site_key
    lab = "Sans Site_ID" if k.startswith("SANS_ID") else "site_sonatel" if k.startswith("SONATEL · ") else k
    dname = site.name or (f"Contrat {site.contrat}" if k.startswith("SANS_ID") else "")
    return lab, dname


def _statut_totals(f: Filters):
    t, n = [0] * 5, [0] * 5
    for r in f.facts().values("statut").annotate(ttc=Sum("ttc"), nb=Sum("nb")):
        t[r["statut"]] += r["ttc"] or 0
        n[r["statut"]] += r["nb"] or 0
    return t, n


def synthese(months: list[str], params) -> dict:
    f = Filters(months, params)
    tT, tN = _statut_totals(f)
    sites = {s.pk: s for s in f.sites()}
    span = months[f.a:f.b + 1]
    k1 = dict.fromkeys(span, 0)
    k2 = dict.fromkeys(span, 0)
    ht = dict.fromkeys(span, 0)
    per_site = defaultdict(lambda: [0, 0, 0, 0])      # ht, kwh, jours, imp (période)
    rows = PgSiteMonth.objects.filter(site_id__in=list(sites), month__gte=months[f.a], month__lte=months[f.b]).values_list("site_id", "month", "ht", "k1", "k2", "jours")
    for sid, m, h, a, b, j in rows:
        k1[m] += a
        k2[m] += b
        ht[m] += h
        t = per_site[sid]
        t[0] += h
        t[1] += a + b
        t[2] += j
    K1, K2, HT = sum(k1.values()), sum(k2.values()), sum(ht.values())
    K = K1 + K2
    active = sum(1 for t in per_site.values() if t[2] > 0)

    # variations : dernier mois vs moyenne des 6 mois précédents (mois complets, > 500 kWh / mois)
    b = f.b
    prev = [m for m in range(b - 6, b) if m >= 0]
    need = [months[m] for m in prev] + [months[b]]
    tot_by = defaultdict(dict)
    for sid, m, a1, a2, j in PgSiteMonth.objects.filter(site_id__in=list(sites), month__in=need).values_list("site_id", "month", "k1", "k2", "jours"):
        tot_by[sid][m] = (a1 + a2, j)
    variations = []
    for sid, ser in tot_by.items():
        lk, lj = ser.get(months[b], (0, 0))
        if lj < 25:
            continue
        sk, c = 0.0, 0
        for m in prev:
            kk, jj = ser.get(months[m], (0, 0))
            if jj >= 25:
                sk += kk / jj * DAYS
                c += 1
        if c < 4:
            continue
        avg = sk / c
        if avg < 500:
            continue
        last = lk / lj * DAYS
        s = sites[sid]
        lab, dname = label_of(s)
        variations.append({"site": s.site_key, "label": lab, "name": dname, "avg": avg, "last": last, "d": (last - avg) / avg * 100})
    variations.sort(key=lambda v: -abs(v["d"]))

    costs = []
    for sid, (h, kwh, j, _imp) in per_site.items():
        if j <= 0 or kwh <= 0:
            continue
        moy = kwh / j * DAYS
        if moy <= 1000:
            continue
        s = sites[sid]
        lab, dname = label_of(s)
        costs.append({"site": s.site_key, "label": lab, "name": dname, "tarif": s.tarif, "ps": s.puissance_kw, "moy": moy, "pu": h / kwh})
    costs.sort(key=lambda c: -c["pu"])

    total = sum(tT)
    rest, rest_n = tT[2] + tT[3] + tT[4], tN[2] + tN[3] + tN[4]
    return {
        "statuts": [{"idx": i, "label": ST_LBL[i], "ttc": tT[i], "nb": tN[i]} for i in range(5)],
        "pct_paye": (tT[0] / total * 100) if total else 0,
        "kpis": {"ttc": total, "nb_factures": sum(tN), "reste": rest, "reste_nb": rest_n, "kwh": K, "k2_pct": (K2 / K * 100) if K else 0, "ht": HT,
                 "cout_kwh": (HT / K) if K else 0, "sites_factures": active, "avec_changement_contrat": sum(1 for s in sites.values() if s.changement_contrat)},
        "mensuel": [{"month": m, "k1": k1[m], "k2": k2[m], "ht": ht[m]} for m in span],
        "variations": variations[:40],
        "variations_note": f"{months[b]} comparé à la moyenne des 6 mois précédents, en kWh par mois normalisés à 30,4 jours. Sites de plus de 500 kWh/mois, mois complets uniquement.",
        "couts": costs[:40],
        "periode": {"from": months[f.a], "to": months[f.b]},
    }


def paiements(months: list[str], params) -> dict:
    f = Filters(months, params)
    metric = (params.get("metric") or "ttc")
    val = (lambda r: r["ttc"] or 0) if metric == "ttc" else (lambda r: r["nb"] or 0)
    rows = list(f.facts().values("statut", "porteur", "echeance", "comptable", "region").annotate(ttc=Sum("ttc"), nb=Sum("nb")))
    tT, tN = [0] * 5, [0] * 5
    for r in rows:
        tT[r["statut"]] += r["ttc"] or 0
        tN[r["statut"]] += r["nb"] or 0
    excl = {"Sans porteur", "SONATEL / SONATEL & AKTIVCO"}
    por_all = sorted({r["porteur"] for r in rows if r["porteur"] not in excl})
    m_ech = defaultdict(float)
    for r in rows:
        if r["statut"] == 0 and r["echeance"]:
            m_ech[(r["echeance"], r["porteur"])] += val(r)
    ech = sorted({e for (e, p), v in m_ech.items() if p not in excl and v})
    m_cpt = defaultdict(float)
    for r in rows:
        m_cpt[(r["comptable"], r["statut"])] += val(r)
    cpt = sorted({c for (c, s) in m_cpt})
    m_reg, r_tot = defaultdict(float), defaultdict(float)
    for r in rows:
        m_reg[(r["region"], r["statut"])] += val(r)
        r_tot[r["region"]] += val(r)
    regs = sorted([k for k, v in r_tot.items() if v], key=lambda k: (m_reg[(k, 0)] / r_tot[k]) if r_tot[k] else 0)
    m_age = defaultdict(float)
    for r in rows:
        if r["statut"] >= 2:
            m_age[(r["comptable"][:4], r["statut"])] += val(r)
    yrs = sorted({y for (y, s) in m_age})
    return {
        "metric": metric,
        "kpis": {"nb": sum(tN), "ttc": sum(tT), "statuts": [{"idx": i, "label": ST_LBL[i], "ttc": tT[i], "nb": tN[i]} for i in range(5)]},
        "echeance": {"labels": ech, "series": [{"porteur": p, "data": [m_ech.get((e, p), 0) for e in ech]} for p in por_all]},
        "comptable": {"labels": cpt, "series": [{"statut": s, "label": ST_LBL[s], "data": [m_cpt.get((c, s), 0) for c in cpt]} for s in range(5)]},
        "regions": {"labels": regs, "series": [{"statut": s, "label": ST_LBL[s], "data": [(m_reg.get((r, s), 0) / r_tot[r] * 100) if r_tot[r] else 0 for r in regs]} for s in (1, 2, 3, 4)]},
        "anciennete": {"labels": yrs, "series": [{"statut": s, "label": ST_LBL[s], "data": [m_age.get((y, s), 0) for y in yrs]} for s in (2, 3, 4)]},
        "periode": {"from": months[f.a], "to": months[f.b]},
    }


def factures(months: list[str], params) -> dict:
    f = Filters(months, params)
    raw = (params.get("statuts") or "1,2,3,4")
    sel = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    qs = f.invoices().filter(statut__in=sel)
    q = (params.get("q") or "").strip()
    if q:
        from django.db.models import CharField
        from django.db.models.functions import Cast

        qs = qs.annotate(_n=Cast("numero", CharField()), _c=Cast("contrat", CharField())).filter(
            Q(_n__icontains=q) | Q(_c__icontains=q) | Q(site_id__icontains=q) | Q(site_name__icontains=q) | Q(localisation__icontains=q))
    agg = qs.aggregate(n=Count("id"), s=Sum("ttc"))
    try:
        size = max(1, min(int(params.get("page_size", 50)), 200))
        page = max(1, int(params.get("page", 1)))
    except ValueError:
        size, page = 50, 1
    rows = qs.order_by("-date_comptable", "numero")[(page - 1) * size: page * size]
    return {"total": agg["n"] or 0, "ttc": agg["s"] or 0, "page": page, "page_size": size,
            "rows": [{"numero": r.numero, "contrat": r.contrat, "site_id": r.site_id, "site_name": r.site_name, "date": r.date_comptable, "ttc": r.ttc,
                      "statut": r.statut, "porteur": r.porteur, "localisation": r.localisation} for r in rows]}
