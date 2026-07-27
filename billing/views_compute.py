# billing/views_compute.py (ou billing/views.py)
from decimal import Decimal, InvalidOperation
from datetime import date

from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status, serializers

from billing.models import ContractSiteLink, TariffRate  # adapte si chemins diff
from core.models import Site  # adapte si besoin

def parse_decimal_fr(v) -> Decimal:
    """
    Accepte:  "1 234,56" / "1234.56" / 1234 / None
    """
    if v is None:
        return Decimal("0")
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return Decimal("0")
    s = s.replace(" ", "").replace("\u00A0", "").replace(",", ".")
    return Decimal(s)

def _to_contract_str(v):
    if v is None:
        return None
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s or None

class ComputeBillInSerializer(serializers.Serializer):
    contract = serializers.CharField()
    date = serializers.DateField()
    k1 = serializers.CharField(required=False, allow_blank=True)  # string pour FR
    k2 = serializers.CharField(required=False, allow_blank=True)
    category = serializers.CharField(required=False, allow_blank=True)

    def validate_contract(self, v):
        c = _to_contract_str(v)
        if not c:
            raise serializers.ValidationError("contract requis.")
        # si tu veux forcer numérique:
        if not c.isdigit():
            raise serializers.ValidationError("contract doit être numérique.")
        if len(c) > 32:
            raise serializers.ValidationError("contract trop long (max 32).")
        return c

    def validate(self, attrs):
        # parse decimals FR
        try:
            attrs["k1_dec"] = parse_decimal_fr(attrs.get("k1"))
            attrs["k2_dec"] = parse_decimal_fr(attrs.get("k2"))
        except (InvalidOperation, ValueError):
            raise serializers.ValidationError("k1/k2 invalide (ex: 1234,56).")
        return attrs

class SonatelBillingComputeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = ComputeBillInSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        contract = data["contract"]
        dt: date = data["date"]
        k1 = data["k1_dec"]
        k2 = data["k2_dec"]

        link = ContractSiteLink.objects.select_related("site").filter(
            numero_compte_contrat=contract
        ).first()
        if not link:
            return Response({"detail": "Contrat non mappé."}, status=404)

        site = link.site

        # ✅ catégorie: soit envoyée par UI, soit depuis Site.billing_typology/contratual_typology
        category = (data.get("category") or "").strip() or (getattr(site, "billing_typology", "") or "").strip() \
                   or (getattr(site, "contratual_typology", "") or "").strip()

        if not category:
            return Response(
                {"detail": "Catégorie tarifaire manquante. Fournis 'category' ou renseigne Site.billing_typology."},
                status=400,
            )

        tr = (
            TariffRate.objects.filter(category__iexact=category, date_debut__lte=dt, date_fin__gte=dt)
            .order_by("-date_debut")
            .first()
        )
        if not tr:
            return Response({"detail": "Aucun tarif trouvé pour cette catégorie/période."}, status=404)

        # ✅ calcul
        unit_k1 = Decimal(tr.energie_k1)
        unit_k2 = Decimal(tr.energie_k2)
        pf = Decimal(tr.prime_fixe)

        amt_k1 = (k1 * unit_k1)
        amt_k2 = (k2 * unit_k2)
        total = amt_k1 + amt_k2 + pf

        return Response(
            {
                "contract": contract,
                "date": dt.isoformat(),
                "site": {"id": site.id, "site_id": site.site_id, "name": site.name},
                "category": category,
                "tariff": {
                    "id": tr.id,
                    "energie_k1": str(tr.energie_k1),
                    "energie_k2": str(tr.energie_k2),
                    "prime_fixe": str(tr.prime_fixe),
                    "date_debut": tr.date_debut.isoformat(),
                    "date_fin": tr.date_fin.isoformat(),
                },
                "inputs": {"k1": str(k1), "k2": str(k2)},
                "breakdown": {
                    "k1_amount": str(amt_k1),
                    "k2_amount": str(amt_k2),
                    "prime_fixe": str(pf),
                    "total": str(total),
                },
            },
            status=200,
        )
