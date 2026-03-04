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



from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Sum, Count
from decimal import Decimal

# ----------------------------
# Helpers
# ----------------------------

D0 = Decimal("0")
D15 = Decimal("1.5")
D30 = Decimal("30")


IGNORED_SITE_KEY = "site_sonatel"


def _parse_qp_date(request, name: str):
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
            return Response(
                {"detail": "Paramètre 'echeance' requis (format date)"},
                status=400
            )


        affected_keys: Set[Tuple[str, int, int]] = set()

        created_count = 0
        updated_count = 0
        monthly_total = 0
        skipped_missing_required = 0
        skipped_invalid_period = 0
        skipped_dup_in_file = 0

        issues_buf = []
        seen_in_file = set()

        # ✅ cache tarifs pour éviter N requêtes
        tariff_cache: Dict[tuple, Optional[TariffRate]] = {}

        with transaction.atomic():
            batch = ImportBatch.objects.create(source_filename=f.name)

            df = pd.read_excel(f, dtype=object)

            # renommage robuste via normalisation
            normed_cols = {_norm_header(c): c for c in df.columns}
            normed_map = {_norm_header(src): dst for src, dst in COLUMN_MAP.items()}
            rename_map = {normed_cols[src]: dst for src, dst in normed_map.items() if src in normed_cols}
            df = df.rename(columns=rename_map)

            if "numero_compte_contrat" in df.columns:
                contracts = set(df["numero_compte_contrat"].dropna().map(_to_contract_str).dropna().tolist())
            else:
                contracts = set()

            contract_to_site_id = dict(
                ContractSiteLink.objects.filter(numero_compte_contrat__in=contracts)
                .values_list("numero_compte_contrat", "site_id")
            )
            missing_contracts = set()

            required_cols = ["numero_compte_contrat", "numero_facture", "date_debut_periode", "date_fin_periode"]
            missing_cols = [c for c in required_cols if c not in df.columns]
            if missing_cols:
                issues_buf.append(
                    ImportIssue(
                        batch=batch,
                        row_number=None,
                        severity=ImportIssue.Severity.ERROR,
                        field=";".join(missing_cols),
                        message=f"Colonnes obligatoires manquantes dans le fichier: {', '.join(missing_cols)}",
                        raw_data={"columns": list(df.columns)},
                    )
                )
                ImportIssue.objects.bulk_create(issues_buf)
                return Response(
                    {
                        "batch": ImportBatchSerializer(batch).data,
                        "detail": "Fichier invalide (colonnes obligatoires manquantes).",
                        "missing_columns": missing_cols,
                    },
                    status=400,
                )

            for i, row in df.iterrows():
                excel_row = int(i) + 2
                raw_row = _row_snapshot(row.to_dict())

                data = {}

                for k in COLUMN_MAP.values():
                    if k not in df.columns:
                        continue
                    val = row.get(k, None)

                    if k in DATE_COLS:
                        parsed = _to_date_fr(val)
                        if parsed is None and not _is_blank(val):
                            issues_buf.append(
                                ImportIssue(
                                    batch=batch,
                                    row_number=excel_row,
                                    severity=ImportIssue.Severity.WARN,
                                    field=k,
                                    message=f"Date non parseable: {val!r}",
                                    raw_data=raw_row,
                                )
                            )
                        data[k] = parsed

                    elif k in INT_COLS:
                        parsed = _to_int(val)
                        if parsed is None and not _is_blank(val):
                            issues_buf.append(
                                ImportIssue(
                                    batch=batch,
                                    row_number=excel_row,
                                    severity=ImportIssue.Severity.WARN,
                                    field=k,
                                    message=f"Entier non parseable: {val!r}",
                                    raw_data=raw_row,
                                )
                            )
                        data[k] = parsed

                    elif k in DEC_COLS:
                        try:
                            parsed = parse_decimal_fr(val)
                        except Exception as e:
                            parsed = None
                            issues_buf.append(
                                ImportIssue(
                                    batch=batch,
                                    row_number=excel_row,
                                    severity=ImportIssue.Severity.WARN,
                                    field=k,
                                    message=f"Decimal parse error: {val!r} ({e})",
                                    raw_data=raw_row,
                                )
                            )
                        if parsed is None and not _is_blank(val):
                            issues_buf.append(
                                ImportIssue(
                                    batch=batch,
                                    row_number=excel_row,
                                    severity=ImportIssue.Severity.WARN,
                                    field=k,
                                    message=f"Decimal non parseable: {val!r}",
                                    raw_data=raw_row,
                                )
                            )
                        data[k] = parsed

                    else:
                        data[k] = None if _is_blank(val) else str(val).strip()
                data["echeance"] = echeance


                # Mapping site
                data["numero_compte_contrat"] = _to_contract_str(data.get("numero_compte_contrat"))
                acc = data.get("numero_compte_contrat")
                if acc:
                    site_pk = contract_to_site_id.get(acc)
                    if site_pk:
                        data["site_id"] = site_pk
                    else:
                        missing_contracts.add(acc)

                # requis minimum
                req_missing = []
                if _is_blank(data.get("numero_compte_contrat")):
                    req_missing.append("numero_compte_contrat")
                if _is_blank(data.get("numero_facture")):
                    req_missing.append("numero_facture")
                if data.get("date_debut_periode") is None:
                    req_missing.append("date_debut_periode")
                if data.get("date_fin_periode") is None:
                    req_missing.append("date_fin_periode")

                if req_missing:
                    skipped_missing_required += 1
                    issues_buf.append(
                        ImportIssue(
                            batch=batch,
                            row_number=excel_row,
                            severity=ImportIssue.Severity.ERROR,
                            field=";".join(req_missing),
                            message=f"Ligne ignorée: champs requis manquants ({', '.join(req_missing)})",
                            raw_data=raw_row,
                        )
                    )
                    continue

                # période valide
                if data["date_fin_periode"] < data["date_debut_periode"]:
                    skipped_invalid_period += 1
                    issues_buf.append(
                        ImportIssue(
                            batch=batch,
                            row_number=excel_row,
                            severity=ImportIssue.Severity.ERROR,
                            field="date_debut_periode;date_fin_periode",
                            message="Ligne ignorée: date_fin_periode < date_debut_periode",
                            raw_data=raw_row,
                        )
                    )
                    continue

                # dédup interne
                key = (
                    data["numero_compte_contrat"],
                    data["numero_facture"],
                    data["date_debut_periode"],
                    data["date_fin_periode"],
                )
                if key in seen_in_file:
                    skipped_dup_in_file += 1
                    issues_buf.append(
                        ImportIssue(
                            batch=batch,
                            row_number=excel_row,
                            severity=ImportIssue.Severity.WARN,
                            field="uniq_key",
                            message="Doublon dans le fichier (même contrat+facture+période). Ligne ignorée.",
                            raw_data=raw_row,
                        )
                    )
                    continue
                seen_in_file.add(key)

                # ✅ CALCUL DONNÉES CIBLES (avant upsert)
                _compute_target_fields(
                    data=data,
                    issues_buf=issues_buf,
                    batch=batch,
                    excel_row=excel_row,
                    raw_row=raw_row,
                    tariff_cache=tariff_cache,
                )

                # upsert
                existing = SonatelInvoice.objects.filter(
                    numero_compte_contrat=data["numero_compte_contrat"],
                    numero_facture=data["numero_facture"],
                    date_debut_periode=data["date_debut_periode"],
                    date_fin_periode=data["date_fin_periode"],
                ).first()

                if existing:
                    for k, v in data.items():
                        setattr(existing, k, v)
                    existing.batch = batch

                    if hasattr(existing, "last_seen_at"):
                        existing.last_seen_at = timezone.now()
                    if hasattr(existing, "last_seen_batch"):
                        existing.last_seen_batch = batch

                    existing.save()

                    existing.months.all().delete()
                    payloads = _build_monthly_payloads(existing)
                    MonthlySynthesis.objects.bulk_create(payloads)

                    updated_count += 1
                else:
                    create_kwargs = dict(batch=batch, **data)
                    field_names = {fld.name for fld in SonatelInvoice._meta.fields}

                    if "last_seen_at" in field_names:
                        create_kwargs["last_seen_at"] = timezone.now()
                    if "last_seen_batch" in field_names:
                        create_kwargs["last_seen_batch"] = batch

                    inv = SonatelInvoice.objects.create(**create_kwargs)
                    payloads = _build_monthly_payloads(inv)
                    MonthlySynthesis.objects.bulk_create(payloads)

                    created_count += 1

                monthly_total += len(payloads)
                for p in payloads:
                    affected_keys.add((p.numero_compte_contrat, p.year, p.month))

            if issues_buf:
                ImportIssue.objects.bulk_create(issues_buf)

            count_upserted = upsert_contract_months_for_keys(affected_keys)
            count_deleted = delete_stale_contract_months(affected_keys)

        return Response(
            {
                "batch": ImportBatchSerializer(batch).data,
                "rows_created": created_count,
                "rows_updated": updated_count,
                "monthly_rows_created": monthly_total,
                "skipped_missing_required": skipped_missing_required,
                "skipped_invalid_period": skipped_invalid_period,
                "skipped_duplicate_in_file": skipped_dup_in_file,
                "issues_logged": len(issues_buf),
                "contract_months_upserted": count_upserted,
                "contract_months_deleted": count_deleted,
                "invoices_missing_site_count": len(missing_contracts),
                "invoices_missing_site_sample": list(missing_contracts)[:20],
            },
            status=status.HTTP_201_CREATED,
        )


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
                numero_compte_contrat__in=ContractSiteLink.objects.filter(site__site_id=site_code)
                .values_list("numero_compte_contrat", flat=True)
            )

        # ✅ NEW: date range => filtre months
        if start and end:
            qs = _filter_year_month_range(qs, start, end)

        link = ContractSiteLink.objects.filter(numero_compte_contrat=OuterRef("numero_compte_contrat"))
        return qs.annotate(
            site_id=Subquery(link.values("site__site_id")[:1]),
            site_name=Subquery(link.values("site__name")[:1]),
        )

class SonatelInvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SonatelInvoice.objects.select_related("batch").all().order_by("-date_comptable_facture")
    serializer_class = SonatelInvoiceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()

        q = self.request.query_params.get("search")
        status_ = self.request.query_params.get("status")
        site_code = self.request.query_params.get("site")

        start = _parse_qp_date(self.request, "start")
        end = _parse_qp_date(self.request, "end")

        if q:
            qs = qs.filter(
                Q(numero_facture__icontains=q)
                | Q(numero_compte_contrat__icontains=q)
                | Q(numero_compteur__icontains=q)
            )
        if status_:
            qs = qs.filter(status=status_.upper())

        if site_code:
            qs = qs.filter(site__site_id=site_code)

        # ✅ overlap [date_debut_periode, date_fin_periode] avec [start,end]
        if start or end:
            if not start:
                start = date.min
            if not end:
                end = date.max
            qs = qs.exclude(date_debut_periode__isnull=True).exclude(date_fin_periode__isnull=True)
            qs = qs.filter(date_debut_periode__lte=end, date_fin_periode__gte=start)

        return qs


class MonthlySynthesisViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MonthlySynthesis.objects.select_related("source", "source__site").all().order_by("-year", "-month")
    serializer_class = MonthlySynthesisSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()

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

        # ✅ NEW: date range => filtre months
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

    @action(methods=["post"], detail=False, url_path="import")
    def import_file(self, request, *args, **kwargs):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni"}, status=400)

        df = pd.read_excel(f, dtype=object)
        df.columns = [_norm_header(c) for c in df.columns]

        # ton fichier: Code site | Name | Numéro contrat
        COLS = {
            "code_site": ["Code site", "Code_site", "Site", "Site ID", "site_id", "code"],
            "name": ["Name", "Nom", "Site name", "Libelle"],
            "numero_contrat": ["Numéro contrat", "Numero contrat", "Numero_contrat", "Contrat", "Contract"],
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
        base = _filter_year_month_range(base, start, end)
        base = base.exclude(
            Q(source__site__site_id__icontains=IGNORED_SITE_KEY) |
            Q(source__site__name__icontains=IGNORED_SITE_KEY)
        )


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

        return Response(
            {
                "range": {"start": start.isoformat(), "end": end.isoformat()},
                "top": {
                    "conso_vs_montant": top("montant_ht"),      # top sur montant_ht (graphe conso vs montant)
                    "cosphi": top("montant_cosphi"),
                    "pen_prime": top("penalite_prime"),
                    "abonnement": top("abonnement"),
                },
                "evolution": evolution,
                "distribution_ht": distribution,
            }
        )





