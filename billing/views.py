# views.py / services.py ✅ PATCH COMPLET — calcul données cibles + prorata + agrégat
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, date as _date, datetime as _dt
from typing import Set, Tuple, Optional, Dict

from datetime import date
from rest_framework.exceptions import ValidationError
from django.db.models import Q

import pandas as pd
from django.db import transaction
from django.db.models import Q, Sum, Count, Min, Max, Avg
from django.utils import timezone
from rest_framework import viewsets, status, mixins
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from core.models import Site
from .models import (
    ImportBatch,
    ImportIssue,
    SonatelInvoice,
    MonthlySynthesis,
    ContractMonth,
    ContractSiteLink,
    TariffRate,   # ✅ NEW
)
from .serializers import (
    ImportBatchSerializer,
    ImportIssueSerializer,
    SonatelInvoiceSerializer,
    MonthlySynthesisSerializer,
    ContractMonthSerializer,
    TariffRateSerializer,  # ✅ NEW
    ContractSiteLinkSerializer
)
from .utils import parse_decimal_fr, iter_month_slices
import re

from rest_framework.permissions import IsAuthenticated
from rest_framework import status as http_status

import unicodedata

from django.utils import timezone

import math
from decimal import Decimal, InvalidOperation
from django.db.models import OuterRef, Subquery
from rest_framework.permissions import IsAuthenticated
import tempfile, os
from certification.models import CertificationResult


from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Sum, Count
from decimal import Decimal
from django.db.models.functions import ExtractYear, ExtractMonth, Coalesce

# ----------------------------
# Helpers
# ----------------------------

D0 = Decimal("0")
D15 = Decimal("1.5")
D30 = Decimal("30")


IGNORED_SITE_KEY = "site_sonatel"



 
 
def _parse_qp_date(request, name: str):
    from datetime import datetime
    v = request.query_params.get(name)
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except Exception:
        raise ValidationError({name: "format attendu YYYY-MM-DD"})
 
 
def _filter_year_month_range(qs, start: date, end: date):
    sy, sm = start.year, start.month
    ey, em = end.year, end.month
    return qs.filter(
        (Q(year__gt=sy) | Q(year=sy, month__gte=sm)),
        (Q(year__lt=ey) | Q(year=ey, month__lte=em)),
    )


def _apply_scope_to_invoice_qs(qs, scope: str):
    scope = (scope or "ALL").upper()

    if scope == "PAID":
        return qs.filter(payment_status=SonatelInvoice.PaymentStatus.PAID)

    if scope == "UNPAID":
        return qs.filter(payment_status=SonatelInvoice.PaymentStatus.UNPAID)

    if scope == "OUT_OF_SCOPE":
        return qs.filter(payment_status=SonatelInvoice.PaymentStatus.OUT_OF_SCOPE)

    if scope == "UNDEFINED":
        return qs.filter(Q(payment_status__isnull=True) | Q(payment_status=""))

    # règle métier : Payée = Certifiée
    if scope == "CERTIFIED":
        return qs.filter(
            Q(payment_status=SonatelInvoice.PaymentStatus.PAID)
            | Q(status=SonatelInvoice.Status.VALIDATED)
        )

    if scope == "CONTESTED":
        return qs.filter(status=SonatelInvoice.Status.CONTESTED).exclude(
            payment_status=SonatelInvoice.PaymentStatus.PAID
        )

    if scope == "CREATED":
        return qs.filter(status=SonatelInvoice.Status.CREATED).exclude(
            payment_status=SonatelInvoice.PaymentStatus.PAID
        )

    return qs


def _apply_scope_to_monthly_qs(qs, scope: str):
    scope = (scope or "ALL").upper()

    if scope == "PAID":
        return qs.filter(source__payment_status=SonatelInvoice.PaymentStatus.PAID)

    if scope == "UNPAID":
        return qs.filter(source__payment_status=SonatelInvoice.PaymentStatus.UNPAID)

    if scope == "OUT_OF_SCOPE":
        return qs.filter(source__payment_status=SonatelInvoice.PaymentStatus.OUT_OF_SCOPE)

    if scope == "UNDEFINED":
        return qs.filter(Q(source__payment_status__isnull=True) | Q(source__payment_status=""))

    # règle métier : Payée = Certifiée
    if scope == "CERTIFIED":
        return qs.filter(
            Q(source__payment_status=SonatelInvoice.PaymentStatus.PAID)
            | Q(source__status=SonatelInvoice.Status.VALIDATED)
        )

    if scope == "CONTESTED":
        return qs.filter(source__status=SonatelInvoice.Status.CONTESTED).exclude(
            source__payment_status=SonatelInvoice.PaymentStatus.PAID
        )

    if scope == "CREATED":
        return qs.filter(source__status=SonatelInvoice.Status.CREATED).exclude(
            source__payment_status=SonatelInvoice.PaymentStatus.PAID
        )

    return qs

def _norm_txt(x: str) -> str:
    s = (x or "").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s

STATUS_MAP = {
    "creee": "CREATED", "cree": "CREATED", "created": "CREATED", "createe": "CREATED",
    "validee": "VALIDATED", "valide": "VALIDATED", "validated": "VALIDATED",
    "contestee": "CONTESTED", "conteste": "CONTESTED", "contested": "CONTESTED",
    "created": "CREATED", "validated": "VALIDATED", "contested": "CONTESTED",
}

def _parse_status(v):
    if v is None:
        return None
    s = _norm_txt(str(v))
    return STATUS_MAP.get(s)


def _d0(x: Optional[Decimal]) -> Decimal:
    return x if x is not None else D0

def _q3(x: Decimal) -> Decimal:
    # arrondi cohérent avec decimal_places=3
    return x.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

def _norm_header(s: str) -> str:
    s = str(s or "").replace("\u00a0", " ").strip()
    s = " ".join(s.split())
    return s

def _is_blank(x) -> bool:
    if x is None:
        return True
    if isinstance(x, float) and pd.isna(x):
        return True
    s = str(x).strip()
    if s == "":
        return True
    if s.lower() in {"nan", "none", "#n/a", "n/a"}:
        return True
    return False

def _json_safe(v):
    try:
        import numpy as np
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return None if pd.isna(v) else float(v)
        if isinstance(v, (np.bool_,)):
            return bool(v)
    except Exception:
        pass

    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.isoformat()
    if isinstance(v, (_dt,)):
        return v.isoformat()
    if isinstance(v, (_date,)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, float) and pd.isna(v):
        return None
    return v

def _row_snapshot(row: dict) -> dict:
    return {k: _json_safe(v) for k, v in row.items()}

def _to_int(x):
    if _is_blank(x):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return int(x)
    s = str(x).strip().replace("\u00a0", " ").replace(" ", "")
    try:
        return int(float(s))
    except Exception:
        return None

def _to_date_fr(x):
    if _is_blank(x):
        return None

    if isinstance(x, pd.Timestamp):
        return None if pd.isna(x) else x.date()
    if isinstance(x, _date) and not isinstance(x, _dt):
        return x
    if isinstance(x, _dt):
        return x.date()

    s = str(x).strip().replace("\u00a0", " ")

    for fmt in (
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%y",
        "%d/%m/%y %H:%M:%S",
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass

    try:
        sn = s.replace(",", ".")
        if sn.replace(".", "", 1).lstrip("-").isdigit():
            val = float(sn)
            if 20000 <= val <= 80000:
                ts = pd.to_datetime(val, unit="D", origin="1899-12-30", errors="coerce")
                if ts is not None and not pd.isna(ts):
                    return ts.date()
    except Exception:
        pass

    ts = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return None if (ts is None or pd.isna(ts)) else ts.date()

"""def _to_contract_str(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    if isinstance(x, (int,)):
        return str(x)
    if isinstance(x, (float,)):
        return str(int(x)) if x.is_integer() else str(x).strip()
    s = str(x).strip().replace("\u00a0", " ")
    s = s.replace(" ", "")
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s or None"""


# ----------------------------
# Mapping
# ----------------------------
COLUMN_MAP = {
    "Numero Compte Contrat": "numero_compte_contrat",
    "Partenaire": "partenaire",
    "Localite": "localite",
    "Arrondissement": "arrondissement",
    "Rue": "rue",
    "Numero Facture": "numero_facture",
    "Date comptable Facture": "date_comptable_facture",
    "Montant Total Energie": "montant_total_energie",
    "Montant Redevance": "montant_redevance",
    "Montant TCO": "montant_tco",
    "Montant Hors TVA": "montant_hors_tva",
    "Montant TVA": "montant_tva",
    "Montant Facture TTC": "montant_ttc",
    "Date Debut Periode Facturation": "date_debut_periode",
    "Date Fin Periode Facturation": "date_fin_periode",
    "AI_CG": "ai_cg",
    "NI_CG": "ni_cg",
    "Ancien index K1": "ancien_index_k1",
    "Ancien Index K2": "ancien_index_k2",
    "Nouvel index K1": "nouvel_index_k1",
    "Nouvel Index K2": "nouvel_index_k2",
    "Montant Energie K1": "montant_energie_k1",
    "Montant Energie K2": "montant_energie_k2",
    "Consommation Facturée": "conso_facturee",
    "Rappel K1": "rappel_k1",
    "Rappel K2": "rappel_k2",
    "Majoration K1": "majoration_k1",
    "Majoration K2": "majoration_k2",
    "Nb Jour Facturation": "nb_jour_facturation",
    "Puissance Transfo": "puissance_transfo",
    "Puissance Souscrite": "puissance_souscrite",       # PS
    "Puissance MAX Relevee": "puissance_max_relevee",   # Prel
    "Montant Prime Fixe": "montant_prime_fixe",
    "Montant cosinus phi": "montant_cosinus_phi",
    "Valeur cosinus phi": "valeur_cosinus_phi",
    "Type de Tarif": "type_de_tarif",  # catégorie tarifaire
    "Type de Client": "type_de_client",
    "CCG": "ccg",
    "Type Compte de Contrat": "type_compte_de_contrat",
    "Anc Cote": "anc_cote",
    "Unite de releve": "unite_de_releve",
    "Ancien index réactif": "ancien_index_reactif",
    "Nouvel index réactif": "nouvel_index_reactif",
    "Consommation réactive": "conso_reactive",
    "Majo réactif": "majo_reactif",
    "Ancien index H1": "ancien_index_h1",
    "Nouvel index H1": "nouvel_index_h1",
    "Consommation H1": "conso_h1",
    "AGENCE": "agence",
    "N° Compteur": "numero_compteur",
    "N°  Compteur": "numero_compteur",
    "Nº Compteur": "numero_compteur",
}

DATE_COLS = {"date_comptable_facture", "date_debut_periode", "date_fin_periode"}
INT_COLS = {"nb_jour_facturation"}

DEC_COLS = {
    "montant_total_energie", "montant_redevance", "montant_tco", "montant_hors_tva", "montant_tva", "montant_ttc",
    "ai_cg", "ni_cg",
    "ancien_index_k1", "ancien_index_k2", "nouvel_index_k1", "nouvel_index_k2",
    "montant_energie_k1", "montant_energie_k2", "conso_facturee",
    "rappel_k1", "rappel_k2", "majoration_k1", "majoration_k2",
    "puissance_transfo", "puissance_souscrite", "puissance_max_relevee",
    "montant_prime_fixe", "montant_cosinus_phi", "valeur_cosinus_phi",
    "ancien_index_reactif", "nouvel_index_reactif", "conso_reactive", "majo_reactif",
    "ancien_index_h1", "nouvel_index_h1", "conso_h1",
}


# ----------------------------
# Tarifs lookup + calcul "données cibles"
# ----------------------------

def _pick_tariff_rate(
    cache: Dict[tuple, Optional[TariffRate]],
    categorie: Optional[str],
    ref_date: Optional[_date],
) -> Optional[TariffRate]:
    if not categorie or not ref_date:
        return None
    key = (categorie.strip().upper(), ref_date)
    if key in cache:
        return cache[key]
 

    tr = (
        TariffRate.objects.filter(
            category__iexact=categorie.strip(),
            date_debut__lte=ref_date,
            date_fin__gte=ref_date,
        )
        .order_by("-date_debut")
        .first()
    )

    cache[key] = tr
    return tr



def eligible_site_q():
    return Q(site__invoice_payment__iexact="Aktivco", site__grid_fee=True)

def eligible_source_site_q():
    return Q(source__site__invoice_payment__iexact="Aktivco", source__site__grid_fee=True)

def eligible_contract_link_q():
    return Q(site__invoice_payment__iexact="Aktivco", site__grid_fee=True)


def normalize_tariff_category(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().upper()
    m = re.search(r"\(([^)]+)\)", s)   # prend DGP dans (... )
    if m:
        return m.group(1).strip().upper()
    return s



def _compute_target_fields(
    data: dict,
    issues_buf: list,
    batch: ImportBatch,
    excel_row: int,
    raw_row: dict,
    tariff_cache: Dict[tuple, Optional[TariffRate]],
):
    """
    Ajoute dans data:
      - abonnement_calcule
      - penalite_abonnement_calculee
      - energie_calculee

    Hypothèse (alignée sur votre structure actuelle):
      Abonnement = Montant Prime Fixe (source) + Montant Redevance + Montant TCO
    """
    # 1) Abonnement
    mpf = data.get("montant_prime_fixe")
    redev = data.get("montant_redevance")
    tco = data.get("montant_tco")
    abonnement = _d0(mpf) + _d0(redev) + _d0(tco)
    data["abonnement_calcule"] = _q3(abonnement)

    # 2) Pénalité abonnement (PenPrime)
    # PenPrime = 1.5 * PrimeFixe(tarif) * max(Prel-PS,0) * nbjrs/30
    ref_date = data.get("date_debut_periode") or data.get("date_comptable_facture")
   
    categorie = normalize_tariff_category(data.get("type_de_tarif"))


    prel = data.get("puissance_max_relevee")
    ps = data.get("puissance_souscrite")
    nbjrs = data.get("nb_jour_facturation")

    pen = D0
    tr = _pick_tariff_rate(tariff_cache, categorie, ref_date)

    if tr is None and categorie:
        issues_buf.append(
            ImportIssue(
                batch=batch,
                row_number=excel_row,
                severity=ImportIssue.Severity.WARN,
                field="type_de_tarif",
                message=f"Tarif introuvable pour categorie={categorie!r} à la date {ref_date}. Pénalité abonnement=0.",
                raw_data=raw_row,
            )
        )

    if tr and prel is not None and ps is not None and nbjrs is not None:
        delta = _d0(prel) - _d0(ps)
        if delta < 0:
            delta = D0
        pen = D15 * _d0(tr.prime_fixe) * delta * (Decimal(nbjrs) / D30)
    else:
        # si données manquantes, pénalité=0 mais on log si on avait une catégorie
        if categorie and (prel is None or ps is None or nbjrs is None):
            issues_buf.append(
                ImportIssue(
                    batch=batch,
                    row_number=excel_row,
                    severity=ImportIssue.Severity.WARN,
                    field="penalite_abonnement_calculee",
                    message="Données manquantes pour calcul PenPrime (Prel/PS/nbjrs). Pénalité abonnement=0.",
                    raw_data=raw_row,
                )
            )

    data["penalite_abonnement_calculee"] = _q3(pen)

    # 3) Énergie (NRJ)
    # NRJ = Montant HT – abonnement – Pénalités abonnement – montant cosphi
    ht = data.get("montant_hors_tva")
    cosphi_pen = data.get("montant_cosinus_phi")
    if ht is None:
        data["energie_calculee"] = None
        issues_buf.append(
            ImportIssue(
                batch=batch,
                row_number=excel_row,
                severity=ImportIssue.Severity.WARN,
                field="montant_hors_tva",
                message="Montant HT manquant: énergie_calculee non calculée.",
                raw_data=raw_row,
            )
        )
    else:
        nrj = _d0(ht) - _d0(data["abonnement_calcule"]) - _d0(data["penalite_abonnement_calculee"]) - _d0(cosphi_pen)
        data["energie_calculee"] = _q3(nrj)


# ----------------------------
# Monthly synthesis builder ✅ (avec données cibles proratisées)
# ----------------------------

def _prorate(val, ratio: Decimal):
    return (val * ratio) if val is not None else None

def _build_monthly_payloads(inv: SonatelInvoice):
    start, end = inv.date_debut_periode, inv.date_fin_periode
    
    if not start or not end or end < start:
        return []

    total_days = (end - start).days + 1
    if total_days <= 0:
        return []

    payloads = []
    for y, m, _seg_start, _seg_end, _days_in_month, days_covered in iter_month_slices(start, end):
        ratio = Decimal(days_covered) / Decimal(total_days)

        payloads.append(
            MonthlySynthesis(
                source=inv,
                year=y,
                month=m,
                period_start=start,
                period_end=end,
                period_total_days=total_days,
                days_covered=days_covered,

                conso=_prorate(inv.conso_facturee, ratio),
                montant_energie=_prorate(inv.montant_total_energie, ratio),
                montant_ttc=_prorate(inv.montant_ttc, ratio),
                montant_hors_tva=_prorate(inv.montant_hors_tva, ratio),

                montant_redevance=_prorate(inv.montant_redevance, ratio),
                montant_tco=_prorate(inv.montant_tco, ratio),
                montant_tva=_prorate(inv.montant_tva, ratio),

                montant_energie_k1=_prorate(inv.montant_energie_k1, ratio),
                montant_energie_k2=_prorate(inv.montant_energie_k2, ratio),
                rappel_k1=_prorate(inv.rappel_k1, ratio),
                rappel_k2=_prorate(inv.rappel_k2, ratio),
                majoration_k1=_prorate(inv.majoration_k1, ratio),
                majoration_k2=_prorate(inv.majoration_k2, ratio),

                montant_prime_fixe=_prorate(inv.montant_prime_fixe, ratio),
                montant_cosinus_phi=_prorate(inv.montant_cosinus_phi, ratio),

                conso_reactive=_prorate(inv.conso_reactive, ratio),
                majo_reactif=_prorate(inv.majo_reactif, ratio),

                conso_h1=_prorate(inv.conso_h1, ratio),

                # ✅ DONNÉES CIBLES proratisées
                abonnement_calcule=_prorate(inv.abonnement_calcule, ratio),
                penalite_abonnement_calculee=_prorate(inv.penalite_abonnement_calculee, ratio),
                energie_calculee=_prorate(inv.energie_calculee, ratio),

                valeur_cosinus_phi=inv.valeur_cosinus_phi,

                numero_compte_contrat=inv.numero_compte_contrat,
                numero_facture=inv.numero_facture,
                status=inv.status,
            )
        )
    return payloads


# ----------------------------
# ImportBatchViewSet.import_file ✅ (calcul données cibles avant upsert)
# ----------------------------

class ImportBatchViewSet(viewsets.GenericViewSet, mixins.ListModelMixin):
    queryset = ImportBatch.objects.all().order_by("-imported_at")
    serializer_class = ImportBatchSerializer
    parser_classes = (MultiPartParser, FormParser)
    

    @action(methods=["get"], detail=True, url_path="issues")
    def issues(self, request, pk=None):
        batch = self.get_object()
        qs = batch.issues.all()

        severity = request.query_params.get("severity")
        if severity:
            qs = qs.filter(severity=severity.upper())

        return Response(ImportIssueSerializer(qs, many=True).data)

   
    @action(methods=["post"], detail=False, url_path="import")
    def import_file(self, request, *args, **kwargs):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni"}, status=400)
    
        echeance_raw = request.data.get("echeance") or request.data.get("date_echeance")
        echeance = _to_date_fr(echeance_raw)
        if not echeance:
            return Response({"detail": "Paramètre echeance requis (YYYY-MM-DD)"}, status=400)
    
        # ✅ Sauvegarder dans default_storage → MEDIA_ROOT (volume partagé web+celery)
        import uuid as _uuid
        from django.core.files.storage import default_storage
        from django.core.files.base import ContentFile
    
        storage_key = f"billing_imports/{_uuid.uuid4().hex}_{f.name}"
        default_storage.save(storage_key, ContentFile(f.read()))
    
        batch = ImportBatch.objects.create(
            source_filename=f.name,
            task_status=ImportBatch.TaskStatus.PENDING,
            task_message="En file d'attente…",
            task_meta={"storage_key": storage_key},
        )
    
        from .tasks import import_invoices_task
        result = import_invoices_task.delay(batch.id, storage_key, str(echeance))
        batch.task_id = result.id
        batch.save(update_fields=["task_id"])
    
        return Response(
            {
                "batch": ImportBatchSerializer(batch).data,
                "task_id": result.id,
                "detail": "Import lancé. Suivre via /batches/{id}/task-status/",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    # billing/views.py — AJOUT dans ImportBatchViewSet
    # ─────────────────────────────────────────────────────────────────────────────
    # Ajouter cette action DANS la classe ImportBatchViewSet, après import_file().
    # ─────────────────────────────────────────────────────────────────────────────

    @action(methods=["post"], detail=False, url_path="import-status-update")
    def import_status_update(self, request, *args, **kwargs):
        """
        Import asynchrone de mise à jour des statuts.

        Accepte le fichier facturation Sonatel standard (même format que l'import normal).
        Chaque ligne identifiée (contrat + facture + dates) voit son statut mis à jour.

        Body (multipart/form-data) :
          - file          : fichier Excel (.xlsx / .xls)
          - status        : statut cible (CREATED | VALIDATED | CONTESTED) — défaut VALIDATED
          - echeance      : date d'échéance YYYY-MM-DD (pour la traçabilité du batch)

        Retour : batch JSON + task_id  (poller via GET /batches/{id}/task-status/)
        """
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni"}, status=400)

        # Statut cible (facultatif, défaut VALIDATED)
        from .models import SonatelInvoice
        target_status = (request.data.get("status") or "VALIDATED").strip().upper()
        valid_statuses = {s.value for s in SonatelInvoice.Status}
        if target_status not in valid_statuses:
            return Response(
                {
                    "detail": f"Statut invalide: {target_status!r}.",
                    "accepted": sorted(valid_statuses),
                },
                status=400,
            )

        # Sauvegarde dans default_storage (volume partagé web+celery)
        import uuid as _uuid
        from django.core.files.storage import default_storage
        from django.core.files.base import ContentFile

        storage_key = f"billing_status_updates/{_uuid.uuid4().hex}_{f.name}"
        default_storage.save(storage_key, ContentFile(f.read()))

        batch = ImportBatch.objects.create(
            kind=ImportBatch.Kind.STATUS_UPDATE,
            source_filename=f.name,
            imported_by=request.user if request.user.is_authenticated else None,
            task_status=ImportBatch.TaskStatus.PENDING,
            task_message="En file d'attente…",
            task_meta={
                "storage_key": storage_key,
                "target_status": target_status,
            },
        )

        from .tasks import import_status_update_task
        result = import_status_update_task.delay(batch.id, storage_key, target_status)
        batch.task_id = result.id
        batch.save(update_fields=["task_id"])

        return Response(
            {
                "batch": ImportBatchSerializer(batch).data,
                "task_id": result.id,
                "detail": (
                    f"Mise à jour statuts lancée (→ {target_status}). "
                    f"Suivre via GET /batches/{batch.id}/task-status/"
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )


    @action(methods=["get"], detail=True, url_path="task-status")
    def task_status(self, request, pk=None):
        """
        Polling endpoint pour suivre la progression d'un import async.
        GET /sonatel-billing/batches/{id}/task-status/
        """
        batch = self.get_object()
        from .serializers import ImportBatchSerializer
        return Response({
            "id":            batch.id,
            "task_id":       batch.task_id,
            "task_status":   batch.task_status,
            "task_progress": batch.task_progress,
            "task_message":  batch.task_message,
            "task_meta":     batch.task_meta,
            "task_updated_at": batch.task_updated_at.isoformat() if batch.task_updated_at else None,
            "source_filename": batch.source_filename,
            "imported_at":   batch.imported_at.isoformat() if batch.imported_at else None,
        })
 
 

# ----------------------------
# ContractMonth upsert / cleanup ✅ (avec sommes données cibles)
# ----------------------------

def _q_or_from_keys(keys: Set[Tuple[str, int, int]]) -> Q:
    q = Q()
    first = True
    for acc, y, m in keys:
        part = Q(numero_compte_contrat=acc, year=y, month=m)
        q = part if first else (q | part)
        first = False
    return q if not first else Q(pk__in=[])

def delete_stale_contract_months(keys: Set[Tuple[str, int, int]]) -> int:
    if not keys:
        return 0

    alive = set(
        MonthlySynthesis.objects.filter(_q_or_from_keys(keys))
        .values_list("numero_compte_contrat", "year", "month")
        .distinct()
    )

    stale = keys - alive
    if not stale:
        return 0

    return ContractMonth.objects.filter(_q_or_from_keys(stale)).delete()[0]



class ContractMonthViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ContractMonth.objects.all().order_by("-year", "-month")
    serializer_class = ContractMonthSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()

        eligible_accounts = ContractSiteLink.objects.filter(
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=True,
        ).values_list("numero_compte_contrat", flat=True)

        qs = qs.filter(numero_compte_contrat__in=eligible_accounts)

        year = self.request.query_params.get("year")
        month = self.request.query_params.get("month")
        account = self.request.query_params.get("account")
        site_code = self.request.query_params.get("site")

        start = _parse_qp_date(self.request, "start")
        end = _parse_qp_date(self.request, "end")
        if start and end and end < start:
            raise ValidationError({"end": "end < start"})

        if year:
            qs = qs.filter(year=int(year))
        if month:
            qs = qs.filter(month=int(month))
        if account:
            qs = qs.filter(numero_compte_contrat=account)

        if site_code:
            qs = qs.filter(
                numero_compte_contrat__in=ContractSiteLink.objects.filter(
                    site__site_id=site_code,
                    site__invoice_payment__iexact="Aktivco",
                    site__grid_fee=True,
                ).values_list("numero_compte_contrat", flat=True)
            )

        if start and end:
            qs = _filter_year_month_range(qs, start, end)

        link = ContractSiteLink.objects.filter(
            numero_compte_contrat=OuterRef("numero_compte_contrat"),
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=True,
        )

        return qs.annotate(
            site_id=Subquery(link.values("site__site_id")[:1]),
            site_name=Subquery(link.values("site__name")[:1]),
        )


class SonatelBillingStatsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        start = _parse_qp_date(request, "start")
        end = _parse_qp_date(request, "end")

        if not start or not end:
            today = timezone.localdate()
            start = start or date(today.year, 1, 1)
            end = end or today

        if end < start:
            raise ValidationError({"end": "end < start"})

        site_code = request.query_params.get("site")
        scope = (request.query_params.get("scope") or "ALL").upper()

        def s(v):
            return str(v or Decimal("0"))

        # ------------------------------------------------------------------
        # Base globale (non filtrée par scope) -> pour cartes de statuts
        # ------------------------------------------------------------------
        monthly_global = MonthlySynthesis.objects.select_related("source__site").filter(
            source__site__isnull=False,
            source__site__invoice_payment__iexact="Aktivco",
            source__site__grid_fee=True,
        )
        monthly_global = _filter_year_month_range(monthly_global, start, end)
        monthly_global = monthly_global.exclude(
            Q(source__site__site_id__icontains=IGNORED_SITE_KEY) |
            Q(source__site__name__icontains=IGNORED_SITE_KEY)
        )

        if site_code:
            monthly_global = monthly_global.filter(source__site__site_id=site_code)

        invoice_global = SonatelInvoice.objects.select_related("site").filter(
            site__isnull=False,
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=True,
        ).exclude(
            Q(site__site_id__icontains=IGNORED_SITE_KEY) |
            Q(site__name__icontains=IGNORED_SITE_KEY)
        )

        if site_code:
            invoice_global = invoice_global.filter(site__site_id=site_code)

        invoice_global = (
            invoice_global
            .exclude(date_debut_periode__isnull=True)
            .exclude(date_fin_periode__isnull=True)
            .filter(date_debut_periode__lte=end, date_fin_periode__gte=start)
        )

        # ------------------------------------------------------------------
        # Base filtrée globalement par scope -> pour toutes les autres stats
        # ------------------------------------------------------------------
        monthly_filtered = _apply_scope_to_monthly_qs(monthly_global, scope)

        # ------------------------------------------------------------------
        # Évolution mensuelle (filtrée par scope)
        # ------------------------------------------------------------------
        evo = (
            monthly_filtered.values("year", "month")
            .annotate(
                invoices=Count("source_id", distinct=True),
                montant_ht=Sum("montant_hors_tva"),
                montant_ttc=Sum("montant_ttc"),
                nrj=Sum("energie_calculee"),
                abonnement=Sum("abonnement_calcule"),
                penalite_prime=Sum("penalite_abonnement_calculee"),
                cosphi=Sum("montant_cosinus_phi"),
            )
            .order_by("year", "month")
        )

        evolution = [
            {
                "period": f"{r['year']}-{str(r['month']).zfill(2)}",
                "invoices": r["invoices"] or 0,
                "montant_ht": s(r["montant_ht"]),
                "montant_ttc": s(r["montant_ttc"]),
                "nrj": s(r["nrj"]),
                "abonnement": s(r["abonnement"]),
                "penalite_prime": s(r["penalite_prime"]),
                "cosphi": s(r["cosphi"]),
            }
            for r in evo
        ]

        # ------------------------------------------------------------------
        # Top sites (filtrés par scope)
        # ------------------------------------------------------------------
        by_site = (
            monthly_filtered.values("source__site__site_id", "source__site__name")
            .annotate(
                conso=Sum("conso"),
                montant_ht=Sum("montant_hors_tva"),
                montant_cosphi=Sum("montant_cosinus_phi"),
                penalite_prime=Sum("penalite_abonnement_calculee"),
                abonnement=Sum("abonnement_calcule"),
            )
        )

        def top(order_field: str):
            rows = by_site.order_by(f"-{order_field}")[:20]
            return [
                {
                    "site_id": r["source__site__site_id"],
                    "site_name": r["source__site__name"],
                    "conso": float(r["conso"] or 0),
                    "montant_ht": s(r["montant_ht"]),
                    "montant_cosphi": s(r["montant_cosphi"]),
                    "penalite_prime": s(r["penalite_prime"]),
                    "abonnement": s(r["abonnement"]),
                }
                for r in rows
            ]

        # ------------------------------------------------------------------
        # Distribution HT (filtrée par scope)
        # ------------------------------------------------------------------
        agg = monthly_filtered.aggregate(
            total_ht=Sum("montant_hors_tva"),
            nrj=Sum("energie_calculee"),
            cosphi=Sum("montant_cosinus_phi"),
            pen_prime=Sum("penalite_abonnement_calculee"),
            abonnement=Sum("abonnement_calcule"),
        )
        total_ht = agg["total_ht"] or Decimal("0")

        def pct(v):
            v = v or Decimal("0")
            return float((v / total_ht * 100) if total_ht else Decimal("0"))

        distribution = {
            "total_ht": s(total_ht),
            "parts": [
                {"key": "NRJ", "label": "NRJ", "value": s(agg["nrj"]), "percent": pct(agg["nrj"])},
                {"key": "COSPHI", "label": "Montant Cos Phi", "value": s(agg["cosphi"]), "percent": pct(agg["cosphi"])},
                {"key": "PEN_PRIME", "label": "Pénalité Prime", "value": s(agg["pen_prime"]), "percent": pct(agg["pen_prime"])},
                {"key": "ABONNEMENT", "label": "Abonnement", "value": s(agg["abonnement"]), "percent": pct(agg["abonnement"])},
            ],
        }

        # ------------------------------------------------------------------
        # Statuts paiement (globaux sur la période, non filtrés par scope)
        # ------------------------------------------------------------------
        payment_summary = invoice_global.aggregate(
            total=Count("id"),
            paid=Count("id", filter=Q(payment_status=SonatelInvoice.PaymentStatus.PAID)),
            unpaid=Count("id", filter=Q(payment_status=SonatelInvoice.PaymentStatus.UNPAID)),
            out_of_scope=Count("id", filter=Q(payment_status=SonatelInvoice.PaymentStatus.OUT_OF_SCOPE)),
            undefined=Count("id", filter=Q(payment_status__isnull=True) | Q(payment_status="")),
        )

        payment_total = payment_summary["total"] or 0

        payment_evo_qs = (
            invoice_global
            .annotate(ref_date=Coalesce("date_comptable_facture", "date_fin_periode"))
            .exclude(ref_date__isnull=True)
            .annotate(year=ExtractYear("ref_date"), month=ExtractMonth("ref_date"))
            .values("year", "month")
            .annotate(
                total=Count("id"),
                paid=Count("id", filter=Q(payment_status=SonatelInvoice.PaymentStatus.PAID)),
                unpaid=Count("id", filter=Q(payment_status=SonatelInvoice.PaymentStatus.UNPAID)),
                out_of_scope=Count("id", filter=Q(payment_status=SonatelInvoice.PaymentStatus.OUT_OF_SCOPE)),
                undefined=Count("id", filter=Q(payment_status__isnull=True) | Q(payment_status="")),
            )
            .order_by("year", "month")
        )

        payment_evolution = [
            {
                "period": f"{r['year']}-{str(r['month']).zfill(2)}",
                "total": r["total"] or 0,
                "paid": r["paid"] or 0,
                "unpaid": r["unpaid"] or 0,
                "out_of_scope": r["out_of_scope"] or 0,
                "undefined": r["undefined"] or 0,
            }
            for r in payment_evo_qs
        ]

        # ------------------------------------------------------------------
        # Certification billing (globale sur la période, non filtrée par scope)
        # règle : Payée = Certifiée
        # ------------------------------------------------------------------
        certified_filter = (
            Q(payment_status=SonatelInvoice.PaymentStatus.PAID)
            | Q(status=SonatelInvoice.Status.VALIDATED)
        )
        contested_filter = (
            Q(status=SonatelInvoice.Status.CONTESTED)
            & ~Q(payment_status=SonatelInvoice.PaymentStatus.PAID)
        )
        created_filter = (
            Q(status=SonatelInvoice.Status.CREATED)
            & ~Q(payment_status=SonatelInvoice.PaymentStatus.PAID)
        )

        invoice_cert_summary = invoice_global.aggregate(
            total=Count("id"),
            certified=Count("id", filter=certified_filter),
            contested=Count("id", filter=contested_filter),
            created=Count("id", filter=created_filter),
        )

        invoice_cert_total = invoice_cert_summary["total"] or 0
        invoice_certified = invoice_cert_summary["certified"] or 0

        invoice_cert_evo_qs = (
            invoice_global
            .annotate(ref_date=Coalesce("date_comptable_facture", "date_fin_periode"))
            .exclude(ref_date__isnull=True)
            .annotate(year=ExtractYear("ref_date"), month=ExtractMonth("ref_date"))
            .values("year", "month")
            .annotate(
                total=Count("id"),
                certified=Count("id", filter=certified_filter),
                contested=Count("id", filter=contested_filter),
                created=Count("id", filter=created_filter),
            )
            .order_by("year", "month")
        )

        invoice_cert_evolution = [
            {
                "period": f"{r['year']}-{str(r['month']).zfill(2)}",
                "total": r["total"] or 0,
                "certified": r["certified"] or 0,
                "contested": r["contested"] or 0,
                "created": r["created"] or 0,
            }
            for r in invoice_cert_evo_qs
        ]

        # ------------------------------------------------------------------
        # Certification technique eFMS/Sénélec (inchangée)
        # ------------------------------------------------------------------
        cert_base = CertificationResult.objects.select_related("cert_batch", "site").filter(
            site__isnull=False,
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=True,
        ).filter(
            (Q(cert_batch__echeance_year__gt=start.year) |
             Q(cert_batch__echeance_year=start.year, cert_batch__echeance_month__gte=start.month)),
            (Q(cert_batch__echeance_year__lt=end.year) |
             Q(cert_batch__echeance_year=end.year, cert_batch__echeance_month__lte=end.month)),
        )

        if site_code:
            cert_base = cert_base.filter(site__site_id=site_code)

        cert_summary = cert_base.aggregate(
            total=Count("id"),
            certified_fms=Count("id", filter=Q(status=CertificationResult.Status.CERTIFIED_FMS)),
            certified_senelec=Count("id", filter=Q(status=CertificationResult.Status.CERTIFIED_SENELEC)),
            needs_review=Count("id", filter=Q(status=CertificationResult.Status.NEEDS_REVIEW)),
            unknown_contract=Count("id", filter=Q(status=CertificationResult.Status.UNKNOWN_CONTRACT)),
            fms_unavailable=Count("id", filter=Q(status=CertificationResult.Status.FMS_UNAVAILABLE)),
            mesure_alert=Count("id", filter=Q(status=CertificationResult.Status.MESURE_A_VERIFIER)),
        )

        certified_total = (cert_summary["certified_fms"] or 0) + (cert_summary["certified_senelec"] or 0)
        other_total = (
            (cert_summary["needs_review"] or 0)
            + (cert_summary["unknown_contract"] or 0)
            + (cert_summary["fms_unavailable"] or 0)
            + (cert_summary["mesure_alert"] or 0)
        )
        total_cert = cert_summary["total"] or 0

        cert_evo_qs = (
            cert_base.values("cert_batch__echeance_year", "cert_batch__echeance_month")
            .annotate(
                total=Count("id"),
                certified_fms=Count("id", filter=Q(status=CertificationResult.Status.CERTIFIED_FMS)),
                certified_senelec=Count("id", filter=Q(status=CertificationResult.Status.CERTIFIED_SENELEC)),
                needs_review=Count("id", filter=Q(status=CertificationResult.Status.NEEDS_REVIEW)),
                unknown_contract=Count("id", filter=Q(status=CertificationResult.Status.UNKNOWN_CONTRACT)),
                fms_unavailable=Count("id", filter=Q(status=CertificationResult.Status.FMS_UNAVAILABLE)),
                mesure_alert=Count("id", filter=Q(status=CertificationResult.Status.MESURE_A_VERIFIER)),
            )
            .order_by("cert_batch__echeance_year", "cert_batch__echeance_month")
        )

        cert_evolution = []
        for r in cert_evo_qs:
            other = (
                (r["needs_review"] or 0)
                + (r["unknown_contract"] or 0)
                + (r["fms_unavailable"] or 0)
                + (r["mesure_alert"] or 0)
            )
            cert_evolution.append({
                "period": f"{r['cert_batch__echeance_year']}-{str(r['cert_batch__echeance_month']).zfill(2)}",
                "total": r["total"] or 0,
                "certified_total": (r["certified_fms"] or 0) + (r["certified_senelec"] or 0),
                "certified_fms": r["certified_fms"] or 0,
                "certified_senelec": r["certified_senelec"] or 0,
                "needs_review": r["needs_review"] or 0,
                "unknown_contract": r["unknown_contract"] or 0,
                "fms_unavailable": r["fms_unavailable"] or 0,
                "mesure_alert": r["mesure_alert"] or 0,
                "other": other,
            })

        return Response({
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "scope": scope,
            "top": {
                "conso_vs_montant": top("montant_ht"),
                "cosphi": top("montant_cosphi"),
                "pen_prime": top("penalite_prime"),
                "abonnement": top("abonnement"),
            },
            "evolution": evolution,
            "distribution_ht": distribution,
            "payment_statuses": {
                "summary": {
                    "total": payment_total,
                    "paid": payment_summary["paid"] or 0,
                    "unpaid": payment_summary["unpaid"] or 0,
                    "out_of_scope": payment_summary["out_of_scope"] or 0,
                    "undefined": payment_summary["undefined"] or 0,
                    "paid_pct": round(((payment_summary["paid"] or 0) / payment_total) * 100, 2) if payment_total else 0,
                },
                "evolution": payment_evolution,
            },
            "invoice_certification": {
                "summary": {
                    "total": invoice_cert_total,
                    "certified": invoice_certified,
                    "contested": invoice_cert_summary["contested"] or 0,
                    "created": invoice_cert_summary["created"] or 0,
                    "taux_certification": round((invoice_certified / invoice_cert_total) * 100, 2) if invoice_cert_total else 0,
                },
                "evolution": invoice_cert_evolution,
            },
            "certification": {
                "summary": {
                    "total": total_cert,
                    "certified_total": certified_total,
                    "certified_fms": cert_summary["certified_fms"] or 0,
                    "certified_senelec": cert_summary["certified_senelec"] or 0,
                    "needs_review": cert_summary["needs_review"] or 0,
                    "unknown_contract": cert_summary["unknown_contract"] or 0,
                    "fms_unavailable": cert_summary["fms_unavailable"] or 0,
                    "mesure_alert": cert_summary["mesure_alert"] or 0,
                    "other": other_total,
                    "taux_certification": round((certified_total / total_cert) * 100, 2) if total_cert else 0,
                },
                "evolution": cert_evolution,
            },
        })


        
class SonatelInvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SonatelInvoice.objects.select_related("batch", "site").all().order_by("-date_comptable_facture")
    serializer_class = SonatelInvoiceSerializer
    permission_classes = [IsAuthenticated]
 
    def get_queryset(self):
        qs = super().get_queryset()
 
        qs = qs.filter(site__isnull=False)
        qs = qs.filter(site__invoice_payment__iexact="Aktivco", site__grid_fee=True)
 
        q          = self.request.query_params.get("search")
        status_    = self.request.query_params.get("status")
        pay_status = self.request.query_params.get("payment_status")   # ✅ nouveau
        site_code  = self.request.query_params.get("site")
 
        start = _parse_qp_date(self.request, "start")
        end   = _parse_qp_date(self.request, "end")
 
        if q:
            qs = qs.filter(
                Q(numero_facture__icontains=q)
                | Q(numero_compte_contrat__icontains=q)
                | Q(numero_compteur__icontains=q)
            )
 
        if status_:
            qs = qs.filter(status=status_.upper())
 
        # ✅ Filtre paiement — ?payment_status=PAID|UNPAID|OUT_OF_SCOPE
        if pay_status:
            qs = qs.filter(payment_status=pay_status.upper())
 
        if site_code:
            qs = qs.filter(site__site_id=site_code)
 
        if start or end:
            if not start:
                from datetime import date as _date_cls
                start = _date_cls.min
            if not end:
                from datetime import date as _date_cls
                end = _date_cls.max
            qs = (
                qs
                .exclude(date_debut_periode__isnull=True)
                .exclude(date_fin_periode__isnull=True)
                .filter(date_debut_periode__lte=end, date_fin_periode__gte=start)
            )
 
        return qs


    @action(methods=["patch"], detail=True, url_path="update-status")
    def update_status(self, request, pk=None):
        invoice = self.get_object()

        status_ = request.data.get("status")
        payment_status = request.data.get("payment_status")

        update_fields = ["updated_at"]

        if status_ is not None:
            valid = {s.value for s in SonatelInvoice.Status}
            if status_.upper() not in valid:
                return Response({"detail": f"Statut invalide: {status_!r}. Acceptés: {sorted(valid)}"}, status=400)
            invoice.status = status_.upper()
            invoice.status_updated_at = timezone.now()
            update_fields += ["status", "status_updated_at"]

        if payment_status is not None:
            valid = {s.value for s in SonatelInvoice.PaymentStatus}
            if payment_status.upper() not in valid:
                return Response({"detail": f"Payment status invalide: {payment_status!r}. Acceptés: {sorted(valid)}"}, status=400)
            invoice.payment_status = payment_status.upper()
            invoice.payment_status_updated_at = timezone.now()
            update_fields += ["payment_status", "payment_status_updated_at"]

        if len(update_fields) <= 1:
            return Response({"detail": "Aucun champ à mettre à jour (status ou payment_status requis)"}, status=400)

        invoice.save(update_fields=update_fields)

        # Propager le statut certif sur les MonthlySynthesis liés
        if "status" in update_fields:
            invoice.months.update(status=invoice.status)

        return Response(SonatelInvoiceSerializer(invoice).data)

class MonthlySynthesisViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MonthlySynthesis.objects.select_related("source", "source__site").all().order_by("-year", "-month")
    serializer_class = MonthlySynthesisSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()

        qs = qs.filter(source__site__isnull=False)
        qs = qs.filter(source__site__invoice_payment__iexact="Aktivco", source__site__grid_fee=True)

        year = self.request.query_params.get("year")
        month = self.request.query_params.get("month")
        account = self.request.query_params.get("account")
        facture = self.request.query_params.get("facture")
        status_ = self.request.query_params.get("status")
        site_code = self.request.query_params.get("site")

        start = _parse_qp_date(self.request, "start")
        end = _parse_qp_date(self.request, "end")
        if start and end and end < start:
            raise ValidationError({"end": "end < start"})

        if year:
            qs = qs.filter(year=int(year))
        if month:
            qs = qs.filter(month=int(month))
        if account:
            qs = qs.filter(numero_compte_contrat=account)
        if facture:
            qs = qs.filter(numero_facture=facture)
        if status_:
            qs = qs.filter(status=status_.upper())
        if site_code:
            qs = qs.filter(source__site__site_id=site_code)

        if start and end:
            qs = _filter_year_month_range(qs, start, end)

        return qs



def upsert_contract_months_for_keys(keys: Set[Tuple[str, int, int]]) -> int:
    if not keys:
        return 0

    filters = None
    for (acc, y, m) in keys:
        q = Q(numero_compte_contrat=acc, year=y, month=m)
        filters = (filters | q) if filters is not None else q

    qs = (
        MonthlySynthesis.objects.filter(filters)
        .filter(
            source__site__isnull=False,
            source__site__invoice_payment__iexact="Aktivco",
            source__site__grid_fee=True,
        )
        .values("numero_compte_contrat", "year", "month")
        .annotate(
            conso=Sum("conso"),
            montant_energie=Sum("montant_energie"),
            montant_ttc=Sum("montant_ttc"),
            montant_hors_tva=Sum("montant_hors_tva"),

            montant_redevance=Sum("montant_redevance"),
            montant_tco=Sum("montant_tco"),
            montant_tva=Sum("montant_tva"),

            montant_energie_k1=Sum("montant_energie_k1"),
            montant_energie_k2=Sum("montant_energie_k2"),
            rappel_k1=Sum("rappel_k1"),
            rappel_k2=Sum("rappel_k2"),
            majoration_k1=Sum("majoration_k1"),
            majoration_k2=Sum("majoration_k2"),

            montant_prime_fixe=Sum("montant_prime_fixe"),
            montant_cosinus_phi=Sum("montant_cosinus_phi"),

            conso_reactive=Sum("conso_reactive"),
            majo_reactif=Sum("majo_reactif"),
            conso_h1=Sum("conso_h1"),

            # ✅ DONNÉES CIBLES
            abonnement_calcule=Sum("abonnement_calcule"),
            penalite_abonnement_calculee=Sum("penalite_abonnement_calculee"),
            energie_calculee=Sum("energie_calculee"),

            valeur_cosinus_phi=Avg("valeur_cosinus_phi"),

            invoices_count=Count("id"),
            first_period_start=Min("period_start"),
            last_period_end=Max("period_end"),
        )
    )

    objs = [
        ContractMonth(
            numero_compte_contrat=r["numero_compte_contrat"],
            year=r["year"],
            month=r["month"],

            conso=r["conso"],
            montant_energie=r["montant_energie"],
            montant_ttc=r["montant_ttc"],
            montant_hors_tva=r["montant_hors_tva"],

            montant_redevance=r["montant_redevance"],
            montant_tco=r["montant_tco"],
            montant_tva=r["montant_tva"],

            montant_energie_k1=r["montant_energie_k1"],
            montant_energie_k2=r["montant_energie_k2"],
            rappel_k1=r["rappel_k1"],
            rappel_k2=r["rappel_k2"],
            majoration_k1=r["majoration_k1"],
            majoration_k2=r["majoration_k2"],

            montant_prime_fixe=r["montant_prime_fixe"],
            montant_cosinus_phi=r["montant_cosinus_phi"],

            conso_reactive=r["conso_reactive"],
            majo_reactif=r["majo_reactif"],
            conso_h1=r["conso_h1"],

            # ✅ DONNÉES CIBLES
            abonnement_calcule=r["abonnement_calcule"],
            penalite_abonnement_calculee=r["penalite_abonnement_calculee"],
            energie_calculee=r["energie_calculee"],

            valeur_cosinus_phi=r["valeur_cosinus_phi"],

            invoices_count=r["invoices_count"],
            first_period_start=r["first_period_start"],
            last_period_end=r["last_period_end"],
        )
        for r in qs
    ]

    try:
        ContractMonth.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["numero_compte_contrat", "year", "month"],
            update_fields=[
                "conso", "montant_energie", "montant_ttc", "montant_hors_tva",
                "montant_redevance", "montant_tco", "montant_tva",
                "montant_energie_k1", "montant_energie_k2",
                "rappel_k1", "rappel_k2", "majoration_k1", "majoration_k2",
                "montant_prime_fixe", "montant_cosinus_phi",
                "conso_reactive", "majo_reactif", "conso_h1",
                # ✅ DONNÉES CIBLES
                "abonnement_calcule", "penalite_abonnement_calculee", "energie_calculee",
                "valeur_cosinus_phi",
                "invoices_count", "first_period_start", "last_period_end",
            ],
        )
        return len(objs)
    except TypeError:
        with transaction.atomic():
            for o in objs:
                ContractMonth.objects.update_or_create(
                    numero_compte_contrat=o.numero_compte_contrat,
                    year=o.year,
                    month=o.month,
                    defaults={f.name: getattr(o, f.name) for f in ContractMonth._meta.fields if f.name != "id"},
                )
        return len(objs)







class TariffRateViewSet(viewsets.ModelViewSet):
    queryset = TariffRate.objects.select_related("last_seen_batch").all().order_by("-date_debut", "category")
    serializer_class = TariffRateSerializer
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get("search")
        if q:
            qs = qs.filter(category__icontains=q)
        cat = self.request.query_params.get("category")
        if cat:
            qs = qs.filter(category__iexact=cat.strip())
        return qs

    @action(methods=["post"], detail=False, url_path="import")
    def import_file(self, request, *args, **kwargs):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni"}, status=400)

        df = pd.read_excel(f, dtype=object)
        df.columns = [_norm_header(c) for c in df.columns]
        print(df.columns)

        # colonnes attendues (avec variantes tolérées)
        COLS = {
            "Catégorie Tarifaire": ["Catégorie Tarifaire", "Categorie Tarifaire", "Categorie", "Catégorie", "Category"],
            "Energie K1": ["Energie K1", "Énergie K1", "Energy K1", "K1"],
            "Energie K2": ["Energie K2", "Énergie K2", "Energy K2", "K2"],
            "Prime Fixe": ["Prime Fixe", "Prime fixe", "Prime", "Fixed Prime"],
            "Date Début": ["Date Début", "Date Debut", "Début", "Debut", "Start Date"],
            "Date Fin": ["Date Fin", "Fin", "End Date"],
        }

        def pick(colnames):
            for c in colnames:
                if c in df.columns:
                    return c
            return None

        c_cat = pick(COLS["Catégorie Tarifaire"])
        c_k1 = pick(COLS["Energie K1"])
        c_k2 = pick(COLS["Energie K2"])
        c_pf = pick(COLS["Prime Fixe"])
        c_sd = pick(COLS["Date Début"])
        c_ed = pick(COLS["Date Fin"])

        missing = [k for k, v in [
            ("Catégorie Tarifaire", c_cat),
            ("Energie K1", c_k1),
            ("Energie K2", c_k2),
            ("Prime Fixe", c_pf),
            ("Date Début", c_sd),
            ("Date Fin", c_ed),
        ] if not v]
        if missing:
            return Response({"detail": f"Colonnes manquantes: {missing}", "found": list(df.columns)}, status=400)

        created = updated = skipped = 0
        issues = []

        with transaction.atomic():
            batch = ImportBatch.objects.create(
                kind=ImportBatch.Kind.TARIFF_TABLE,
                source_filename=f.name,
                imported_by=request.user,
            )

            for i, row in df.iterrows():
                excel_row = int(i) + 2

                cat = row.get(c_cat, None)
                cat = None if _is_blank(cat) else str(cat).strip()

                sd = _to_date_fr(row.get(c_sd, None))
                ed = _to_date_fr(row.get(c_ed, None))

                print(cat, sd, ed)

                try:
                    k1 = parse_decimal_fr(row.get(c_k1, None))
                    k2 = parse_decimal_fr(row.get(c_k2, None))
                    pf = parse_decimal_fr(row.get(c_pf, None))
                except Exception as e:
                    issues.append({"row": excel_row, "error": f"Decimal parse error: {e}"})
                    skipped += 1
                    continue

                if not cat or sd is None or ed is None or ed < sd:
                    issues.append({"row": excel_row, "error": "cat/date invalides"})
                    skipped += 1
                    continue

                obj, was_created = TariffRate.objects.update_or_create(
                    category=cat,
                    date_debut=sd,
                    date_fin=ed,
                    defaults={
                        "energie_k1": k1,
                        "energie_k2": k2,
                        "prime_fixe": pf,
                        "last_seen_at": timezone.now(),
                        "last_seen_batch": batch,
                    },
                )
                created += 1 if was_created else 0
                updated += 0 if was_created else 1

        return Response(
            {
                "file": f.name,
                "batch_id": batch.id,
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "issues_sample": issues[:30],
            },
            status=status.HTTP_201_CREATED,
        )

    @action(methods=["get"], detail=False, url_path="resolve")
    def resolve(self, request, *args, **kwargs):
        """
        Lookup utile aux calculs :
        /tariff-rates/resolve/?category=DGP&date=2026-01-15
        """
        cat = (request.query_params.get("category") or "").strip()
        d = (request.query_params.get("date") or "").strip()
        if not cat or not d:
            return Response({"detail": "category et date requis"}, status=400)

        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return Response({"detail": "date invalide (format YYYY-MM-DD)"}, status=400)

        tr = (
            TariffRate.objects.filter(category__iexact=cat, date_debut__lte=dt, date_fin__gte=dt)
            .order_by("-date_debut")
            .first()
        )
        if not tr:
            return Response({"detail": "Aucun tarif trouvé pour cette catégorie/période"}, status=404)

        return Response(TariffRateSerializer(tr).data, status=200)





def _to_contract_str(v):
    """Convertit proprement le numero contrat Excel -> string (sans .0, sans exponent)."""
    if _is_blank(v):
        return None

    # int direct
    if isinstance(v, (int,)):
        return str(v).strip()

    # float (souvent Excel met 2200...0)
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return str(int(v))

    s = str(v).strip()

    # 22001513021.0 -> 22001513021
    if s.endswith(".0"):
        s = s[:-2]

    # scientific notation (rare mais possible)
    if "e" in s.lower():
        try:
            s = format(Decimal(s), "f")
            if "." in s:
                s = s.split(".")[0]
        except (InvalidOperation, ValueError):
            pass

    s = s.strip()
    return s or None


class ContractSiteLinkViewSet(viewsets.GenericViewSet, mixins.ListModelMixin):
    queryset = ContractSiteLink.objects.select_related("site").all().order_by("-last_seen_at")
    serializer_class = ContractSiteLinkSerializer
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
 
        # ✅ Filtre de recherche : site_id ou nom du site
        q = self.request.query_params.get("search")
        if q:
            qs = qs.filter(
                Q(site__site_id__icontains=q) | Q(site__name__icontains=q)
            )
 
        return qs

    @action(methods=["post"], detail=False, url_path="import")
    def import_file(self, request, *args, **kwargs):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni"}, status=400)

        df = pd.read_excel(f, dtype=object)
        df.columns = [_norm_header(c) for c in df.columns]

        # ton fichier: Code site | Name | Numéro contrat
        COLS = {
            "code_site":               ["Site ID"],
            "name":                    ["Site Name"],
            "numero_contrat":          ["Numéro contrat", "Numero contrat"],
            "puissance_contractuelle": ["Puissance contractuelle"],     # col K → W
            "load_activation":         ["Load activation"],             # à chercher plus à droite
            "typologie_contractuelle": ["Typologie contractuelle"],
        }

        def pick(colnames):
            for c in colnames:
                c_norm = _norm_header(c)
                if c_norm in df.columns:
                    return c_norm
            return None

        c_site = pick(COLS["code_site"])
        c_name = pick(COLS["name"])
        c_ctr  = pick(COLS["numero_contrat"])

        missing = [k for k, v in [("code_site", c_site), ("numero_contrat", c_ctr)] if not v]
        if missing:
            return Response({"detail": f"Colonnes manquantes: {missing}", "found": list(df.columns)}, status=400)

        created = updated = skipped = 0
        issues = []

        with transaction.atomic():
            batch = ImportBatch.objects.create(
                kind=ImportBatch.Kind.CONTRACT_SITE_LINK,
                source_filename=f.name,
                imported_by=request.user,
            )

            for i, row in df.iterrows():
                excel_row = int(i) + 2

                site_code = None if _is_blank(row.get(c_site)) else str(row.get(c_site)).strip()
                contract = _to_contract_str(row.get(c_ctr))
                name = None
                if c_name:
                    name = None if _is_blank(row.get(c_name)) else str(row.get(c_name)).strip()

                if not site_code or not contract:
                    skipped += 1
                    issues.append({"row": excel_row, "error": "site_code/contract vide"})
                    continue

                # ✅ mappe/cree le site
                site, _ = Site.objects.get_or_create(
                    site_id=site_code,
                    defaults={"name": name or site_code},
                )
                # si tu veux updater le name si vide en base
                if name and (not getattr(site, "name", None)):
                    site.name = name
                    site.save(update_fields=["name"])

                obj, was_created = ContractSiteLink.objects.update_or_create(
                    numero_compte_contrat=contract,
                    defaults={
                        "site": site,
                        "last_seen_at": timezone.now(),
                        
                        # "source_filename": f.name, # si tu as ce champ
                        # "imported_by": request.user, # si tu as ce champ
                    },
                )
                created += 1 if was_created else 0
                updated += 0 if was_created else 1

        return Response(
            {
                "file": f.name,
                "batch_id": batch.id,
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "issues_sample": issues[:30],
            },
            status=status.HTTP_201_CREATED,
        )



"""
class SonatelBillingStatsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        start = _parse_qp_date(request, "start")
        end = _parse_qp_date(request, "end")

        # ✅ default = current year
        if not start or not end:
            today = timezone.localdate()
            start = start or date(today.year, 1, 1)
            end = end or today

        if end < start:
            raise ValidationError({"end": "end < start"})

        base = MonthlySynthesis.objects.select_related("source__site")
        base = base.filter(source__site__isnull=False)
        base = base.filter(
            source__site__invoice_payment__iexact="Aktivco",
            source__site__grid_fee=True,
        )
        base = _filter_year_month_range(base, start, end)
        base = base.exclude(
            Q(source__site__site_id__icontains=IGNORED_SITE_KEY) |
            Q(source__site__name__icontains=IGNORED_SITE_KEY)
        )
 
        # ✅ Filtre optionnel par site (recherche frontend)
        site_code = request.query_params.get("site")
        if site_code:
            base = base.filter(source__site__site_id=site_code)


        def s(v):  # json safe decimal
            return str(v or Decimal("0"))

        # --- evolution (par mois)
        evo = (
            base.values("year", "month")
            .annotate(
                invoices=Count("source_id", distinct=True),
                montant_ht=Sum("montant_hors_tva"),
                montant_ttc=Sum("montant_ttc"),
                nrj=Sum("energie_calculee"),
                abonnement=Sum("abonnement_calcule"),
                penalite_prime=Sum("penalite_abonnement_calculee"),
                cosphi=Sum("montant_cosinus_phi"),
            )
            .order_by("year", "month")
        )
        evolution = [
            {
                "period": f"{r['year']}-{str(r['month']).zfill(2)}",
                "invoices": r["invoices"] or 0,
                "montant_ht": s(r["montant_ht"]),
                "montant_ttc": s(r["montant_ttc"]),
                "nrj": s(r["nrj"]),
                "abonnement": s(r["abonnement"]),
                "penalite_prime": s(r["penalite_prime"]),
                "cosphi": s(r["cosphi"]),
            }
            for r in evo
        ]

        # --- top 20 (group by site)
        by_site = (
            base.values("source__site__site_id", "source__site__name")
            .annotate(
                conso=Sum("conso"),
                montant_ht=Sum("montant_hors_tva"),
                montant_cosphi=Sum("montant_cosinus_phi"),
                penalite_prime=Sum("penalite_abonnement_calculee"),
                abonnement=Sum("abonnement_calcule"),
            )
        )

        def top(order_field: str):
            rows = by_site.order_by(f"-{order_field}")[:20]
            return [
                {
                    "site_id": r["source__site__site_id"],
                    "site_name": r["source__site__name"],
                    "conso": float(r["conso"] or 0),
                    "montant_ht": s(r["montant_ht"]),
                    "montant_cosphi": s(r["montant_cosphi"]),
                    "penalite_prime": s(r["penalite_prime"]),
                    "abonnement": s(r["abonnement"]),
                }
                for r in rows
            ]

        # --- distribution HT
        agg = base.aggregate(
            total_ht=Sum("montant_hors_tva"),
            nrj=Sum("energie_calculee"),
            cosphi=Sum("montant_cosinus_phi"),
            pen_prime=Sum("penalite_abonnement_calculee"),
            abonnement=Sum("abonnement_calcule"),
        )
        total_ht = agg["total_ht"] or Decimal("0")

        def pct(v):
            v = v or Decimal("0")
            return float((v / total_ht * 100) if total_ht else Decimal("0"))

        distribution = {
            "total_ht": s(total_ht),
            "parts": [
                {"key": "NRJ", "label": "NRJ", "value": s(agg["nrj"]), "percent": pct(agg["nrj"])},
                {"key": "COSPHI", "label": "Montant Cos Phi", "value": s(agg["cosphi"]), "percent": pct(agg["cosphi"])},
                {"key": "PEN_PRIME", "label": "Pénalité Prime", "value": s(agg["pen_prime"]), "percent": pct(agg["pen_prime"])},
                {"key": "ABONNEMENT", "label": "Abonnement", "value": s(agg["abonnement"]), "percent": pct(agg["abonnement"])},
            ],
        }

        # ── Certification stats ───────────────────────────────────────────────
        cert_base = CertificationResult.objects.select_related("cert_batch", "site").filter(
            site__isnull=False,
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=True,
        ).filter(
            (Q(cert_batch__echeance_year__gt=start.year) |
             Q(cert_batch__echeance_year=start.year, cert_batch__echeance_month__gte=start.month)),
            (Q(cert_batch__echeance_year__lt=end.year) |
             Q(cert_batch__echeance_year=end.year, cert_batch__echeance_month__lte=end.month)),
        )

        if site_code:
            cert_base = cert_base.filter(site__site_id=site_code)

        cert_summary = cert_base.aggregate(
            total=Count("id"),
            certified_fms=Count("id", filter=Q(status=CertificationResult.Status.CERTIFIED_FMS)),
            certified_senelec=Count("id", filter=Q(status=CertificationResult.Status.CERTIFIED_SENELEC)),
            needs_review=Count("id", filter=Q(status=CertificationResult.Status.NEEDS_REVIEW)),
            unknown_contract=Count("id", filter=Q(status=CertificationResult.Status.UNKNOWN_CONTRACT)),
            fms_unavailable=Count("id", filter=Q(status=CertificationResult.Status.FMS_UNAVAILABLE)),
        )

        certified_total = (cert_summary["certified_fms"] or 0) + (cert_summary["certified_senelec"] or 0)
        other_total = (
            (cert_summary["needs_review"] or 0)
            + (cert_summary["unknown_contract"] or 0)
            + (cert_summary["fms_unavailable"] or 0)
        )
        total_cert = cert_summary["total"] or 0

        cert_evo_qs = (
            cert_base.values("cert_batch__echeance_year", "cert_batch__echeance_month")
            .annotate(
                total=Count("id"),
                certified_fms=Count("id", filter=Q(status=CertificationResult.Status.CERTIFIED_FMS)),
                certified_senelec=Count("id", filter=Q(status=CertificationResult.Status.CERTIFIED_SENELEC)),
                needs_review=Count("id", filter=Q(status=CertificationResult.Status.NEEDS_REVIEW)),
                unknown_contract=Count("id", filter=Q(status=CertificationResult.Status.UNKNOWN_CONTRACT)),
                fms_unavailable=Count("id", filter=Q(status=CertificationResult.Status.FMS_UNAVAILABLE)),
            )
            .order_by("cert_batch__echeance_year", "cert_batch__echeance_month")
        )

        cert_evolution = []
        for r in cert_evo_qs:
            other = (r["needs_review"] or 0) + (r["unknown_contract"] or 0) + (r["fms_unavailable"] or 0)
            cert_evolution.append({
                "period": f"{r['cert_batch__echeance_year']}-{str(r['cert_batch__echeance_month']).zfill(2)}",
                "total": r["total"] or 0,
                "certified_total": (r["certified_fms"] or 0) + (r["certified_senelec"] or 0),
                "certified_fms": r["certified_fms"] or 0,
                "certified_senelec": r["certified_senelec"] or 0,
                "needs_review": r["needs_review"] or 0,
                "unknown_contract": r["unknown_contract"] or 0,
                "fms_unavailable": r["fms_unavailable"] or 0,
                "other": other,
            })

        return Response(
            {
                "range": {"start": start.isoformat(), "end": end.isoformat()},
                "top": {
                    "conso_vs_montant": top("montant_ht"),
                    "cosphi": top("montant_cosphi"),
                    "pen_prime": top("penalite_prime"),
                    "abonnement": top("abonnement"),
                },
                "evolution": evolution,
                "distribution_ht": distribution,
                "certification": {
                    "summary": {
                        "total": total_cert,
                        "certified_total": certified_total,
                        "certified_fms": cert_summary["certified_fms"] or 0,
                        "certified_senelec": cert_summary["certified_senelec"] or 0,
                        "needs_review": cert_summary["needs_review"] or 0,
                        "unknown_contract": cert_summary["unknown_contract"] or 0,
                        "fms_unavailable": cert_summary["fms_unavailable"] or 0,
                        "other": other_total,
                        "taux_certification": round((certified_total / total_cert) * 100, 2) if total_cert else 0,
                    },
                    "evolution": cert_evolution,
                },
            }
        )

"""





class ImpactedSitesAPIView(APIView):
    """
    GET /billing/impacted-sites/
    
    Retourne les sites impactés par le cos phi et/ou la pénalité prime fixe,
    mois par mois, sur une période donnée.

    Paramètres :
      - start  (YYYY-MM-DD) : début de période  [défaut: 1er janvier année courante]
      - end    (YYYY-MM-DD) : fin de période     [défaut: aujourd'hui]
      - filter : "cosphi" | "penalty" | "both"   [défaut: "both"]
      - min_amount : montant minimum pour être inclus dans les résultats [défaut: 0]

    Réponse :
    {
      "range": {"start": "...", "end": "..."},
      "summary": {
        "total_cosphi":  "...",   // montant cosphi total sur la période
        "total_penalty": "...",   // montant pénalité total sur la période
        "sites_cosphi_count":  N, // nb sites distincts avec cosphi
        "sites_penalty_count": N, // nb sites distincts avec pénalité
      },
      "by_month": [
        {
          "period": "2025-01",
          "sites": [
            {
              "site_id": "...",
              "site_name": "...",
              "numero_compte_contrat": "...",
              "valeur_cosinus_phi": 0.72,
              "montant_cosphi": "15420.000",
              "penalite_prime": "8200.000",
              "montant_hors_tva": "245000.000",
              // poids relatif dans la facture
              "pct_cosphi_sur_ht": 6.29,
              "pct_penalty_sur_ht": 3.35,
            },
            ...
          ],
          "totaux": {
            "montant_cosphi": "...",
            "penalite_prime": "...",
            "sites_count": N,
          }
        },
        ...
      ]
    }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # ── Paramètres ────────────────────────────────────────────────────────
        start = _parse_qp_date(request, "start")
        end   = _parse_qp_date(request, "end")

        if not start or not end:
            today = timezone.localdate()
            start = start or date(today.year, 1, 1)
            end   = end   or today

        if end < start:
            raise ValidationError({"end": "end < start"})

        filter_mode = request.query_params.get("filter", "both").lower()
        if filter_mode not in ("cosphi", "penalty", "both"):
            raise ValidationError({"filter": "valeurs acceptées : cosphi | penalty | both"})

        try:
            min_amount = Decimal(request.query_params.get("min_amount", "0"))
        except Exception:
            min_amount = Decimal("0")

        # ── Base queryset ─────────────────────────────────────────────────────
        base = (
            MonthlySynthesis.objects
            .select_related("source__site")
            .filter(
                source__site__isnull=False,
                source__site__invoice_payment__iexact="Aktivco",
                source__site__grid_fee=True,
            )
        )
        base = _filter_year_month_range(base, start, end)

        # Filtre selon le mode
        if filter_mode == "cosphi":
            base = base.filter(montant_cosinus_phi__isnull=False).exclude(montant_cosinus_phi=D0)
        elif filter_mode == "penalty":
            base = base.filter(penalite_abonnement_calculee__isnull=False).exclude(penalite_abonnement_calculee=D0)
        else:  # both — au moins l'un des deux non nul
            base = base.filter(
                Q(montant_cosinus_phi__isnull=False, montant_cosinus_phi__gt=D0)
                | Q(montant_cosinus_phi__lt=D0)  # cosphi peut être négatif (minoration)
                | Q(penalite_abonnement_calculee__isnull=False, penalite_abonnement_calculee__gt=D0)
            )

        # ── Agrégat par mois + site ───────────────────────────────────────────
        rows = (
            base
            .values(
                "year",
                "month",
                "source__site__site_id",
                "source__site__name",
                "numero_compte_contrat",
            )
            .annotate(
                montant_cosphi=Sum("montant_cosinus_phi"),
                penalite_prime=Sum("penalite_abonnement_calculee"),
                montant_hors_tva=Sum("montant_hors_tva"),
                valeur_cosinus_phi=Avg("valeur_cosinus_phi"),
            )
            .order_by("year", "month", "-montant_cosphi")
        )

        # ── Filtrage min_amount ───────────────────────────────────────────────
        def _passes_min(r):
            cosphi  = abs(r["montant_cosphi"]  or D0)
            penalty = abs(r["penalite_prime"]  or D0)
            if filter_mode == "cosphi":
                return cosphi >= min_amount
            if filter_mode == "penalty":
                return penalty >= min_amount
            return (cosphi + penalty) >= min_amount

        # ── Construction de la réponse par mois ──────────────────────────────
        months_map: dict[str, dict] = {}

        total_cosphi  = Decimal("0")
        total_penalty = Decimal("0")
        sites_cosphi:  set[str] = set()
        sites_penalty: set[str] = set()

        for r in rows:
            if not _passes_min(r):
                continue

            period = f"{r['year']}-{str(r['month']).zfill(2)}"
            if period not in months_map:
                months_map[period] = {
                    "period": period,
                    "sites": [],
                    "totaux": {
                        "montant_cosphi": Decimal("0"),
                        "penalite_prime": Decimal("0"),
                        "sites_count": 0,
                    }
                }

            cosphi_val  = r["montant_cosphi"]  or Decimal("0")
            penalty_val = r["penalite_prime"]  or Decimal("0")
            ht_val      = r["montant_hors_tva"] or Decimal("0")
            site_id     = r["source__site__site_id"]

            # Pourcentage dans la facture HT
            pct_cosphi  = float(cosphi_val  / ht_val * 100) if ht_val else 0
            pct_penalty = float(penalty_val / ht_val * 100) if ht_val else 0

            months_map[period]["sites"].append({
                "site_id":                site_id,
                "site_name":              r["source__site__name"],
                "numero_compte_contrat":  r["numero_compte_contrat"],
                "valeur_cosinus_phi":     float(r["valeur_cosinus_phi"]) if r["valeur_cosinus_phi"] else None,
                "montant_cosphi":         str(cosphi_val.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "penalite_prime":         str(penalty_val.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "montant_hors_tva":       str(ht_val.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "pct_cosphi_sur_ht":      round(pct_cosphi, 2),
                "pct_penalty_sur_ht":     round(pct_penalty, 2),
            })

            months_map[period]["totaux"]["montant_cosphi"]  += cosphi_val
            months_map[period]["totaux"]["penalite_prime"]  += penalty_val
            months_map[period]["totaux"]["sites_count"]     += 1

            total_cosphi  += cosphi_val
            total_penalty += penalty_val

            if cosphi_val != D0:
                sites_cosphi.add(site_id)
            if penalty_val > D0:
                sites_penalty.add(site_id)

        # Sérialiser les totaux Decimal → str
        by_month = []
        for period_data in months_map.values():
            t = period_data["totaux"]
            period_data["totaux"] = {
                "montant_cosphi": str(t["montant_cosphi"].quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "penalite_prime": str(t["penalite_prime"].quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "sites_count":    t["sites_count"],
            }
            by_month.append(period_data)

        return Response({
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "filter": filter_mode,
            "summary": {
                "total_cosphi":        str(total_cosphi.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "total_penalty":       str(total_penalty.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "sites_cosphi_count":  len(sites_cosphi),
                "sites_penalty_count": len(sites_penalty),
            },
            "by_month": by_month,
        })



class FNPSitesAPIView(APIView):
    """
    GET /sonatel-billing/fnp/
    Retourne les sites sans facture (Factures Non Parvenues) sur la période,
    avec estimation basée sur la moyenne glissante des N derniers mois connus.

    Params :
      - start   YYYY-MM-DD
      - end     YYYY-MM-DD
      - site    filtre optionnel sur site_id
      - horizon nb de mois d'historique pour l'estimation (défaut 3, max 12)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from collections import defaultdict

        start = _parse_qp_date(request, "start")
        end   = _parse_qp_date(request, "end")

        if not start or not end:
            today = timezone.localdate()
            start = start or date(today.year, 1, 1)
            end   = end   or today

        if end < start:
            raise ValidationError({"end": "end < start"})

        site_code = request.query_params.get("site")
        try:
            horizon = max(1, min(int(request.query_params.get("horizon", 3)), 12))
        except Exception:
            horizon = 3

        # ── 1) Contrats éligibles ─────────────────────────────────────────────
        link_qs = (
            ContractSiteLink.objects
            .filter(site__invoice_payment__iexact="Aktivco", site__grid_fee=True)
            .select_related("site")
        )
        if site_code:
            link_qs = link_qs.filter(site__site_id=site_code)

        contracts = {lnk.numero_compte_contrat: lnk for lnk in link_qs}
        if not contracts:
            return Response({
                "range": {"start": start.isoformat(), "end": end.isoformat()},
                "horizon": horizon,
                "summary": {
                    "fnp_count": 0, "sites_count": 0,
                    "estimated_total_ht": "0.000",
                    "months_covered": 0, "months_with_fnp": 0,
                },
                "rows": [],
            })

        # ── 2) Énumération des mois dans la plage ─────────────────────────────
        months_in_range: list[tuple[int, int]] = []
        cur = date(start.year, start.month, 1)
        end_anchor = date(end.year, end.month, 1)
        while cur <= end_anchor:
            months_in_range.append((cur.year, cur.month))
            # avance d'un mois
            cur = date(
                cur.year + (1 if cur.month == 12 else 0),
                (cur.month % 12) + 1,
                1,
            )

        # ── 3) Synthèses existantes sur la plage ──────────────────────────────
        existing: set[tuple[str, int, int]] = set(
            _filter_year_month_range(
                MonthlySynthesis.objects.filter(
                    numero_compte_contrat__in=contracts.keys(),
                    source__site__invoice_payment__iexact="Aktivco",
                    source__site__grid_fee=True,
                ),
                start, end,
            ).values_list("numero_compte_contrat", "year", "month")
        )

        # ── 4) Clés FNP = attendues − existantes ─────────────────────────────
        fnp_keys: list[tuple[str, int, int]] = [
            (contract, y, m)
            for contract in contracts
            for y, m in months_in_range
            if (contract, y, m) not in existing
        ]

        if not fnp_keys:
            return Response({
                "range": {"start": start.isoformat(), "end": end.isoformat()},
                "horizon": horizon,
                "summary": {
                    "fnp_count": 0, "sites_count": 0,
                    "estimated_total_ht": "0.000",
                    "months_covered": len(months_in_range), "months_with_fnp": 0,
                },
                "rows": [],
            })

        fnp_contracts = list({c for c, _, _ in fnp_keys})

        # ── 5) Historique en masse pour tous les contrats FNP ─────────────────
        # On récupère TOUT l'historique (pas filtré sur la plage) pour pouvoir
        # calculer les moyennes avant chaque mois manquant.
        history_qs = (
            MonthlySynthesis.objects
            .filter(
                numero_compte_contrat__in=fnp_contracts,
                source__site__invoice_payment__iexact="Aktivco",
                source__site__grid_fee=True,
            )
            .order_by("numero_compte_contrat", "-year", "-month")
            .values(
                "numero_compte_contrat", "year", "month",
                "conso", "montant_hors_tva", "montant_ttc",
                "abonnement_calcule", "penalite_abonnement_calculee", "energie_calculee",
            )
        )

        history_by_contract: dict[str, list] = defaultdict(list)
        for row in history_qs:
            history_by_contract[row["numero_compte_contrat"]].append(row)

        # ── Helpers ───────────────────────────────────────────────────────────
        def _pk(y, m):
            return y * 100 + m

        def _avg(rows, field):
            vals = [Decimal(str(r[field])) for r in rows if r.get(field) is not None]
            return (sum(vals) / len(vals)) if vals else None

        def _fmt(v):
            if v is None:
                return None
            return str(v.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))

        # ── 6) Construction des lignes résultantes ────────────────────────────
        result_rows = []
        for contract, y, m in fnp_keys:
            link = contracts[contract]
            target_pk = _pk(y, m)

            # Les N derniers mois strictement antérieurs à (y, m)
            hist = [
                h for h in history_by_contract[contract]
                if _pk(h["year"], h["month"]) < target_pk
            ][:horizon]

            last = hist[0] if hist else None
            last_period = (
                f"{last['year']}-{str(last['month']).zfill(2)}" if last else None
            )

            result_rows.append({
                "site_id":               link.site.site_id,
                "site_name":             link.site.name,
                "numero_compte_contrat": contract,
                "year":   y,
                "month":  m,
                "period": f"{y}-{str(m).zfill(2)}",
                "est_conso":       _fmt(_avg(hist, "conso")),
                "est_montant_ht":  _fmt(_avg(hist, "montant_hors_tva")),
                "est_montant_ttc": _fmt(_avg(hist, "montant_ttc")),
                "est_abonnement":  _fmt(_avg(hist, "abonnement_calcule")),
                "est_penalite":    _fmt(_avg(hist, "penalite_abonnement_calculee")),
                "est_nrj":         _fmt(_avg(hist, "energie_calculee")),
                "history_months":  len(hist),
                "last_invoice_period": last_period,
                "typology": link.site.typology if hasattr(link.site, 'typology') else None,
            })

        # ── 7) Résumé ─────────────────────────────────────────────────────────
        total_est_ht = sum(
            Decimal(r["est_montant_ht"]) for r in result_rows if r["est_montant_ht"]
        )
        total_est_ttc = sum(
            Decimal(r["est_montant_ttc"]) for r in result_rows if r["est_montant_ttc"]
        )
        sites_count   = len({r["site_id"] for r in result_rows})
        months_w_fnp  = len({(r["year"], r["month"]) for r in result_rows})
        no_hist_count = sum(1 for r in result_rows if r["history_months"] == 0)

        result_rows.sort(key=lambda r: (r["year"], r["month"], r["site_id"]))

        return Response({
            "range":   {"start": start.isoformat(), "end": end.isoformat()},
            "horizon": horizon,
            "summary": {
                "fnp_count":          len(result_rows),
                "sites_count":        sites_count,
                "estimated_total_ht": str(total_est_ht.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "estimated_total_ttc":str(total_est_ttc.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
                "months_covered":     len(months_in_range),
                "months_with_fnp":    months_w_fnp,
                "no_history_count":   no_hist_count,   # FNP sans historique (nouveau site)
            },
            "rows": result_rows,
        })