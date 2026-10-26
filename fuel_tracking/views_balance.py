# fuel_tracking/views_balance.py
"""
Bilan de stock par site et période exacte (services/stock_period_balance.py).

  GET  stock/bilans/?debut=…&fin=…&pays&zone&site&typologie&statut&certification&stock_initial=present|absent
                     &stock_final=present|absent&source=…&a_controler=1&page&limit   liste paginée côté serveur + totaux
  GET  stock/bilans/periodes/                                  périodes déjà calculées
  POST stock/bilans/calculer/   {debut, fin, site?}            calcule / recalcule la période exacte (admin, manager)
  GET  stock/bilans/<id>/                                      fiche : mouvements, candidats stock, sélection, historique
  POST stock/bilans/<id>/certifier/ {decision, comment}        certifier | certifier_avec_reserve (admin, manager)
  GET  stock/bilans/rapprochement/?debut=…&fin=…               conso réelle / instrumentée / théorique (onglet Consommation)
"""
from __future__ import annotations

from datetime import datetime, time

from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.views_cph import VALIDATOR_ROLES, IsCphValidator


def _f(v):
    return float(v) if v is not None else None


def parse_bound(value: str | None, end: bool = False):
    """« 2026-10-02 » (début 00:00 / fin 23:59:59) ou « 2026-10-02T06:30 » ; fuseau Africa/Dakar."""
    if not value:
        return None
    try:
        if "T" in value or " " in value:
            dt = datetime.fromisoformat(value.replace(" ", "T"))
        else:
            d = datetime.strptime(value, "%Y-%m-%d").date()
            dt = datetime.combine(d, time(23, 59, 59) if end else time.min)
    except ValueError:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def row(b) -> dict:
    return {
        "id": b.pk, "country": b.country, "site_id": b.site_id, "site_name": b.site_name, "zone": b.zone,
        "typologie": b.typologie, "profil_energetique": b.profil_energetique,
        "observation_start": b.observation_start, "observation_end": b.observation_end,
        "stock_initial_l": _f(b.stock_initial_l), "source_stock_initial": b.source_stock_initial, "date_stock_initial": b.date_stock_initial,
        "stock_final_l": _f(b.stock_final_l), "source_stock_final": b.source_stock_final, "date_stock_final": b.date_stock_final,
        "truck_l": _f(b.truck_l), "carte_total_l": _f(b.carte_total_l), "transferts_entrants_l": _f(b.transferts_entrants_l),
        "transferts_sortants_l": _f(b.transferts_sortants_l), "vols_l": _f(b.vols_l), "vidanges_l": _f(b.vidanges_l),
        "retraits_l": _f(b.retraits_l), "rajouts_l": _f(b.rajouts_l), "livraisons_externes_l": _f(b.livraisons_externes_l),
        "consommation_reelle_l": _f(b.consommation_reelle_l), "conso_journaliere_l": _f(b.conso_journaliere_l),
        "statut_bilan": b.statut_bilan, "niveau_certification": b.niveau_certification, "motifs": b.motifs, "alertes": b.alertes,
        "cause_principale": b.cause_principale, "score_confiance": b.score_confiance, "reserves": b.reserves, "estimation": b.estimation,
        "age_stock_initial_j": b.age_stock_initial_j, "age_stock_final_j": b.age_stock_final_j,
        "entrees_externes_nettes_l": _f(b.entrees_externes_nettes_l), "volume_transfere_l": _f(b.volume_transfere_l),
        "impact_ponctions_l": _f(b.impact_ponctions_l), "volume_brut_integre_l": _f(b.volume_brut_integre_l),
        "avertissement": "ESTIMATION — VALIDATION FUEL MANAGER REQUISE" if b.statut_bilan == "ESTIMATION_AVEC_RESERVE" else None,
        "nb_mouvements": len(b.mouvements or []), "nb_a_controler": sum(1 for x in (b.mouvements_exclus or []) if "contrôler" in x.get("motif", "")),
        "fraicheur_stock_final_h": round((b.calculated_at - b.date_stock_final).total_seconds() / 3600, 1) if b.date_stock_final else None,
        "calculated_at": b.calculated_at, "rule_version": b.rule_version,
    }


def filtered(params):
    from fuel_tracking.models import FuelStockBalance as B

    start, end = parse_bound(params.get("debut")), parse_bound(params.get("fin"), end=True)
    qs = B.objects.all()
    if start and end:
        qs = qs.filter(observation_start=start, observation_end=end)
    p = params
    if p.get("pays"):
        qs = qs.filter(country__iexact=p["pays"])
    if p.get("zone"):
        qs = qs.filter(zone__iexact=p["zone"])
    if p.get("site"):
        s = p["site"].strip()
        qs = qs.filter(Q(site_id__icontains=s) | Q(site_name__icontains=s))
    if p.get("typologie"):
        qs = qs.filter(typologie__iexact=p["typologie"])
    if p.get("statut"):
        qs = qs.filter(statut_bilan=p["statut"])
    if p.get("certification"):
        qs = qs.filter(niveau_certification=p["certification"])
    for key, field in (("stock_initial", "stock_initial_l"), ("stock_final", "stock_final_l")):
        if p.get(key) == "present":
            qs = qs.filter(**{f"{field}__isnull": False})
        elif p.get(key) == "absent":
            qs = qs.filter(**{f"{field}__isnull": True})
    if p.get("source"):
        qs = qs.filter(Q(source_stock_initial=p["source"]) | Q(source_stock_final=p["source"]))
    if p.get("a_controler") in ("1", "true"):
        qs = qs.filter(cause_principale__in=("MOUVEMENTS_A_CONTROLER", "TRANSFERT_DESEQUILIBRE"))
    if p.get("cause"):
        qs = qs.filter(cause_principale=p["cause"])
    if p.get("estimation") in ("1", "true"):
        qs = qs.filter(estimation=True)
    return qs, start, end


class StockBalanceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.views_stock import _page, _pagination

        qs, start, end = filtered(request.query_params)
        if not (start and end):
            last = B.objects.order_by("-calculated_at").values("observation_start", "observation_end").first()
            if last:
                qs = qs.filter(observation_start=last["observation_start"], observation_end=last["observation_end"])
                start, end = last["observation_start"], last["observation_end"]
        page, limit = _page(request, default=50, maximum=200)
        total = qs.count()
        lo = (page - 1) * limit
        data = [row(b) for b in qs.order_by("statut_bilan", "site_id")[lo:lo + limit]]
        period = B.objects.filter(observation_start=start, observation_end=end) if start and end else B.objects.none()
        totals = {r["statut_bilan"]: r["n"] for r in period.values("statut_bilan").annotate(n=Count("id"))}
        sources = {}
        for field in ("source_stock_initial", "source_stock_final"):
            for r in period.exclude(**{f"{field}__isnull": True}).values(field).annotate(n=Count("id")):
                sources.setdefault(r[field], {"initial": 0, "final": 0})["initial" if "initial" in field else "final"] = r["n"]
        vol = period.aggregate(truck=Sum("livraisons_externes_l"), carte=Sum("carte_total_l"), ent=Sum("transferts_entrants_l"),
                               sor=Sum("transferts_sortants_l"), brut=Sum("volume_brut_integre_l"))
        z = lambda v: float(v or 0)  # noqa: E731  (somme d'agrégats : 0 = aucune ligne, jamais une donnée inconnue)
        volumes = {
            "livraisons_truck_eydon_l": z(vol["truck"]), "ravitaillements_carte_total_l": z(vol["carte"]),
            "entrees_externes_nettes_l": z(vol["truck"]) + z(vol["carte"]),
            "ponctions_entrantes_l": z(vol["ent"]), "ponctions_sortantes_l": z(vol["sor"]), "volume_transfere_entre_sites_l": z(vol["sor"]),
            "impact_global_ponctions_l": __import__("fuel_tracking.services.stock_period_balance", fromlist=["x"]).ponctions_globales(start, end)["impact_global_l"] if start and end else 0.0,
            "volume_brut_integre_l": z(vol["brut"]),
            "note": "Les Ponctions sont des transferts internes : elles ne sont ni des achats ni une entrée nette de carburant ; "
                    "leur impact global (pays) est nul.",
        }
        return Response({
            "data": data, "pagination": _pagination(page, limit, total), "periode": {"debut": start, "fin": end}, "volumes": volumes,
            "totaux": {"par_statut": totals, "calculables": period.filter(consommation_reelle_l__isnull=False).count(),
                       "incomplets": period.filter(consommation_reelle_l__isnull=True).count(), "sources_stock": sources,
                       "par_certification": {r["niveau_certification"]: r["n"] for r in period.values("niveau_certification").annotate(n=Count("id"))}},
            "facettes": {
                "statuts": [{"key": k, "label": lbl} for k, lbl in B.Statut.choices],
                "certifications": [{"key": k, "label": lbl} for k, lbl in B.Niveau.choices],
                "zones": sorted({v for v in period.values_list("zone", flat=True) if v}),
                "pays": sorted({v for v in period.values_list("country", flat=True) if v}),
                "typologies": sorted({v for v in period.values_list("typologie", flat=True) if v}),
                "sources": sorted(sources),
                "causes": sorted({v for v in period.values_list("cause_principale", flat=True) if v}),
            },
            "can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES,
            "formule": "conso_reelle = stock_initial + livraisons_truck + carte_total + transferts_entrants + rajouts "
                       "− transferts_sortants − retraits − vols − vidanges − stock_final",
            "seuils": __import__("fuel_tracking.services.stock_period_balance", fromlist=["cfg"]).cfg(),
        })


class StockBalancePeriodsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelStockBalance as B

        rows = (B.objects.values("observation_start", "observation_end")
                .annotate(sites=Count("id"), calculables=Count("id", filter=Q(consommation_reelle_l__isnull=False)))
                .order_by("-observation_end", "-observation_start")[:50])
        return Response({"data": list(rows)})


class StockBalanceComputeView(APIView):
    permission_classes = [IsCphValidator]
    parser_classes = [JSONParser]

    def post(self, request):
        from fuel_tracking.services.stock_period_balance import BalanceError, compute_balances

        start, end = parse_bound(request.data.get("debut")), parse_bound(request.data.get("fin"), end=True)
        if not (start and end):
            return Response({"detail": "Période obligatoire : debut et fin (AAAA-MM-JJ ou AAAA-MM-JJTHH:MM)."}, status=400)
        if (end - start).days > 400:
            return Response({"detail": "Période limitée à 400 jours."}, status=400)
        site = (request.data.get("site") or "").strip()
        try:
            rows = compute_balances(start, end, site_ids=[site] if site else None)
        except BalanceError as e:
            return Response({"detail": str(e)}, status=400)
        return Response({"periode": {"debut": start, "fin": end}, "sites": len(rows),
                         "calculables": sum(1 for r in rows if r["consommation_reelle_l"] is not None),
                         "incomplets": sum(1 for r in rows if r["consommation_reelle_l"] is None)})


class StockBalanceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.views_enoc import enoc_link

        b = B.objects.filter(pk=pk).first()
        if b is None:
            return Response({"detail": "Bilan introuvable."}, status=404)
        mvts = [{**m, "lien_enoc": enoc_link(m.get("reference")) if m.get("source") == "ENOC" else None} for m in b.mouvements or []]
        return Response({
            **row(b), "stock_initial_detail": b.stock_initial_detail, "stock_final_detail": b.stock_final_detail,
            "candidats": b.candidats, "mouvements": mvts, "mouvements_exclus": b.mouvements_exclus,
            "score_detail": b.score_detail, "proposition": b.proposition,
            "periode_effective": {"debut": b.periode_effective_debut, "fin": b.periode_effective_fin},
            "certifications": [{"action": c.action, "commentaire": c.comment, "utilisateur": c.user_label or (c.user.username if c.user_id else None),
                                "date": c.created_at, "valeurs_certifiees": c.snapshot, "toujours_valide": c.fingerprint == b.fingerprint}
                               for c in b.certifications.select_related("user")],
            "priorite_sources": ["1. Closing mensuel validé", "2. Relevé terrain gardiennage validé (Loliforce / SCSS)",
                                 "3. Relevé ENOC qualifié (statut éligible)", "4. Autre source identifiée (marquée)"],
            "priorite_valeurs": ["1. Litre directement déclaré", "2. cm converti, cuve validée avec plaque",
                                 "3. cm estimé, cuve géométrique sans plaque (ESTIMATION)", "4. cm estimé, cuve proposée à forte compatibilité (ESTIMATION)",
                                 "5. Aucun calcul si aucune cuve exploitable"],
            "can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES,
        })


class StockBalanceCertifyView(APIView):
    permission_classes = [IsCphValidator]
    parser_classes = [JSONParser]

    def post(self, request, pk: int):
        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.services.stock_period_balance import BalanceError, certify

        b = B.objects.filter(pk=pk).first()
        if b is None:
            return Response({"detail": "Bilan introuvable."}, status=404)
        try:
            certify(b, request.user, str(request.data.get("decision") or ""), str(request.data.get("comment") or ""))
        except BalanceError as e:
            return Response({"detail": str(e)}, status=400)
        b.refresh_from_db()
        return Response(row(b))


class StockBalanceReconciliationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.services.stock_period_balance import reconciliation
        from fuel_tracking.views_stock import _page, _pagination

        start, end = parse_bound(request.query_params.get("debut")), parse_bound(request.query_params.get("fin"), end=True)
        if not (start and end):
            return Response({"detail": "Période obligatoire (debut, fin)."}, status=400)
        if not B.objects.filter(observation_start=start, observation_end=end).exists():
            return Response({"data": [], "pagination": _pagination(1, 50, 0), "calcule": False,
                             "message": "Aucun bilan de stock calculé pour cette période exacte : le calculer dans l'onglet Stock › Bilan de stock."})
        rows = reconciliation(start, end, country=request.query_params.get("pays"), zone=request.query_params.get("zone"))
        if request.query_params.get("calculables") in ("1", "true"):
            rows = [r for r in rows if r["conso_reelle_l"] is not None]
        site = (request.query_params.get("site") or "").strip().upper()
        if site:
            rows = [r for r in rows if site in (r["site_id"] or "").upper() or site in (r["site_name"] or "").upper()]
        rows.sort(key=lambda r: (r["conso_reelle_l"] is None, r["site_id"]))
        page, limit = _page(request, default=50, maximum=200)
        lo = (page - 1) * limit
        return Response({"data": rows[lo:lo + limit], "pagination": _pagination(page, limit, len(rows)), "calcule": True,
                         "totaux": {"sites": len(rows), "avec_conso_reelle": sum(1 for r in rows if r["conso_reelle_l"] is not None)}})
