from django.shortcuts import render

from rest_framework import viewsets, status


from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser

import pandas as pd



from .models import Site, GridTargetRule
from .serializers import SiteSerializer, GridTargetRuleSerializer


@api_view(['GET'])
def ping(request):
    return Response({"status": "OK", "message": "EnerTrack API is up"})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def protected_ping(request):
    return Response({"message": f"Bonjour {request.user.username}, accès autorisé."})


class SiteViewSet(viewsets.ModelViewSet):
    queryset = Site.objects.all()
    serializer_class = SiteSerializer

    def get_queryset(self):
        user_country = self.request.user.pays
        return Site.objects.filter(country=user_country).order_by('zone', 'name')


class SiteImportView(APIView):
    parser_classes = [MultiPartParser]
    permission_classes = [IsAuthenticated]

    def normalize_str(self, value):
        if pd.isna(value):
            return None
        value = str(value).strip()
        return value if value else None

    def normalize_bool(self, value):
        if pd.isna(value):
            return None

        value = str(value).strip().lower()

        if value in ['oui', 'yes', 'true', '1', 'o', 'y']:
            return True
        if value in ['non', 'no', 'false', '0', 'n']:
            return False

        return None

    def normalize_int(self, value):
        if pd.isna(value) or value == '':
            return None
        try:
            return int(float(value))
        except Exception:
            return None

    def normalize_site_type(self, value):
        v = self.normalize_str(value)
        if not v:
            return None

        s = v.strip().lower()
        if s in ['indoor', 'in door', 'in-door']:
            return 'INDOOR'
        if s in ['outdoor', 'out door', 'out-door']:
            return 'OUTDOOR'
        return None

    def compute_load_band(self, analysis_load):
        if analysis_load is None:
            return None
        if analysis_load <= 500:
            return "0-500"
        if analysis_load <= 1000:
            return "501-1000"
        if analysis_load <= 1500:
            return "1001-1500"
        return "1500+"

    def compute_scope_status(self, invoice_payment, grid_fee, not_yet_solarized):
        payment = (invoice_payment or "").strip().lower()

        if not_yet_solarized is True:
            return "OUT_OF_SCOPE"

        if grid_fee is False:
            return "OUT_OF_SCOPE"

        if payment == "aktivco" and grid_fee is True:
            return "IN_SCOPE"

        return "UNKNOWN"

    def classify_comment_category(self, comment):
        if not comment:
            return None

        c = comment.lower()

        if "fms" in c and ("sup" in c or "supérieure" in c or "superieure" in c):
            return "FMS_GT_TARGET"

        if "très inférieure" in c or "tres inferieure" in c or "very low" in c:
            return "VERY_LOW_VS_TARGET"

        if "adjust" in c or "ajust" in c:
            return "TO_ADJUST"

        if "review" in c or "analyse" in c or "analy" in c:
            return "TO_REVIEW"

        return None

    def build_target_mapping_key(self, configuration, site_type, load_band):
        parts = [
            (configuration or "").strip(),
            (site_type or "").strip(),
            (load_band or "").strip(),
        ]
        parts = [p for p in parts if p]
        return " | ".join(parts) if parts else None

    def post(self, request, format=None):
        file = request.FILES.get('file')
        if not file:
            return Response(
                {"error": "Aucun fichier fourni."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            df = pd.read_excel(file)
        except Exception as e:
            return Response(
                {"error": f"Impossible de lire le fichier Excel : {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        required_columns = [
            'Code site',
            'Name',
        ]

        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            return Response(
                {"error": f"Colonnes obligatoires manquantes : {missing_columns}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        created_count = 0
        updated_count = 0
        errors = []
        user_country = request.user.pays

        for index, row in df.iterrows():
            try:
                site_id = self.normalize_str(row.get('Code site'))
                name = self.normalize_str(row.get('Name'))

                if not site_id:
                    errors.append({
                        "row": index + 2,
                        "error": "Code site ou Name manquant"
                    })
                    continue

                zone = site_id.split('_')[0].upper() if '_' in site_id else None

                ordered_typology = self.normalize_str(row.get('Typologie commandée'))
                installed_typology = self.normalize_str(row.get('Typologie installée'))
                billing_typology = self.normalize_str(row.get('Typologie'))
                contract_number = self.normalize_str(row.get('Numéro contrat'))
                meter_number = self.normalize_str(row.get('Numéro compteur'))

                analysis_load = self.normalize_int(row.get('Load Analyses'))
                load_band = self.compute_load_band(analysis_load)

                raw_site_type = self.normalize_str(row.get('Type'))
                site_type = self.normalize_site_type(raw_site_type)

                indoor_billed_outdoor = self.normalize_bool(row.get('Sites indoor facturés outdoor'))
                not_yet_solarized = self.normalize_bool(row.get('Sites non encore solarisés'))
                energy_desk_comment = self.normalize_str(row.get('Commentaire Energy Desk sur Load'))
                invoice_payment = self.normalize_str(row.get('Paiement Facture'))
                grid_fee = self.normalize_bool(row.get('Redevance Grid'))
                batch_operational = self.normalize_str(row.get('Batch opérationel'))

                # Configuration: colonne directe si présente, sinon fallback
                configuration = (
                    self.normalize_str(row.get('Configuration'))
                    or installed_typology
                    or ordered_typology
                )

                # Types commandé / installé si colonnes dédiées présentes
                ordered_site_type = self.normalize_site_type(
                    row.get('Type commandé') if 'Type commandé' in df.columns else raw_site_type
                )
                installed_site_type = self.normalize_site_type(
                    row.get('Type installé') if 'Type installé' in df.columns else raw_site_type
                )

                scope_status = self.compute_scope_status(
                    invoice_payment=invoice_payment,
                    grid_fee=grid_fee,
                    not_yet_solarized=not_yet_solarized,
                )

                load_comment_category = self.classify_comment_category(energy_desk_comment)

                target_mapping_key = self.build_target_mapping_key(
                    configuration=configuration,
                    site_type=site_type,
                    load_band=load_band,
                )

                defaults = {
                    'name': name,
                    'modernized': self.normalize_bool(row.get('Modernisé (juillet à Decembre)')),
                    'ordered_typology': ordered_typology,
                    'installed_typology': installed_typology,
                    'billing_typology': billing_typology,
                    'contract_number': contract_number,
                    'meter_number': meter_number,
                    'analysis_load': analysis_load,
                    'load_band': load_band,
                    'site_type': site_type,
                    'ordered_site_type': ordered_site_type,
                    'installed_site_type': installed_site_type,
                    'configuration': configuration,
                    'target_mapping_key': target_mapping_key,
                    'transformer_capacity': self.normalize_int(row.get('Capacité transformateur')),
                    'indoor_billed_outdoor': indoor_billed_outdoor,
                    'not_yet_solarized': not_yet_solarized,
                    'energy_desk_comment': energy_desk_comment,
                    'load_comment_category': load_comment_category,
                    'invoice_payment': invoice_payment,
                    'grid_fee': grid_fee,
                    'batch_operational': batch_operational,
                    'scope_status': scope_status,
                    'zone': zone if zone in dict(Site.ZONE_CHOICES) else None,
                    'country': user_country,
                }

                obj, created = Site.objects.update_or_create(
                    site_id=site_id,
                    defaults=defaults
                )

                if created:
                    created_count += 1
                else:
                    updated_count += 1

            except Exception as e:
                errors.append({
                    "row": index + 2,
                    "site_id": str(row.get('Code site', '')),
                    "error": str(e)
                })

        return Response({
            "message": "Import terminé.",
            "created": created_count,
            "updated": updated_count,
            "errors_count": len(errors),
            "errors": errors[:50]
        }, status=status.HTTP_200_OK)







class GridTargetRuleViewSet(viewsets.ModelViewSet):
    queryset = GridTargetRule.objects.all().order_by("configuration", "site_type", "load_band")
    serializer_class = GridTargetRuleSerializer
    permission_classes = [IsAuthenticated]


class GridTargetRuleImportView(APIView):
    parser_classes = [MultiPartParser]
    permission_classes = [IsAuthenticated]

    def find_columns(self, columns, must_include_all=()):
        matches = []
        for col in columns:
            raw = str(col).strip().lower()
            base = raw.split("__")[0].strip()
            if all(token.lower() in base for token in must_include_all):
                matches.append(col)
        return matches

    def uniquify_columns(self, columns):
        seen = {}
        result = []

        for col in columns:
            base = str(col).strip()
            count = seen.get(base, 0) + 1
            seen[base] = count

            if count == 1:
                result.append(f"{base}__1")
            else:
                result.append(f"{base}__{count}")

        return result

    def normalize_str(self, value):
        if pd.isna(value):
            return None
        value = str(value).strip()
        if value in {"", "-", "nan", "None"}:
            return None
        return value

    def normalize_decimal(self, value):
        if pd.isna(value):
            return None
        s = str(value).strip()
        if s in {"", "-", "nan", "None"}:
            return None
        s = s.replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            return float(s)
        except Exception:
            return None

    def normalize_load_band(self, value):
        if pd.isna(value):
            return None
        s = str(value).strip()
        if s in {"", "-", "nan", "None"}:
            return None
        try:
            n = float(s)
            if n.is_integer():
                return str(int(n))
            return str(n)
        except Exception:
            return s

    def row_text(self, row):
        vals = [str(v).strip().lower() for v in row.tolist() if pd.notna(v) and str(v).strip()]
        return " | ".join(vals)

    def find_header_rows(self, df_raw):
        cfg_row = None
        metric_row = None
        unit_row = None
        group_row = None

        for i in range(min(len(df_raw), 20)):
            txt = self.row_text(df_raw.iloc[i])

            if cfg_row is None and (
                "codes configurations" in txt or "load bands telco" in txt or "load bands" in txt
            ):
                cfg_row = i

            if metric_row is None and (
                "redevance energie grid" in txt or "cible energie grid" in txt
            ):
                metric_row = i

            if unit_row is None and ("fcfa" in txt or "kwh" in txt):
                unit_row = i

        if metric_row is not None:
            # chercher au-dessus la ligne qui contient outdoor / indoor
            for j in range(max(0, metric_row - 3), metric_row):
                txt = self.row_text(df_raw.iloc[j])
                if "outdoor" in txt or "indoor" in txt:
                    group_row = j
                    break

        return group_row, cfg_row, metric_row, unit_row

    def build_headers_from_client_sheet(self, df_raw):
        group_row, cfg_row, metric_row, unit_row = self.find_header_rows(df_raw)

        if cfg_row is None or metric_row is None or unit_row is None:
            raise ValueError(
                f"Impossible d'identifier les lignes d'en-tête. "
                f"group_row={group_row}, cfg_row={cfg_row}, metric_row={metric_row}, unit_row={unit_row}"
            )

        header_indices = [x for x in [group_row, cfg_row, metric_row, unit_row] if x is not None]
        header_block = df_raw.iloc[header_indices].copy()

        # important pour récupérer les cellules fusionnées Excel
        header_block = header_block.ffill(axis=1).fillna("")

        headers = []
        for col_idx in range(df_raw.shape[1]):
            parts = []
            for row_idx in range(header_block.shape[0]):
                cell = str(header_block.iat[row_idx, col_idx]).strip()
                if cell and cell.lower() != "nan":
                    if cell not in parts:
                        parts.append(cell)
            headers.append(" | ".join(parts).strip())

        data_start = unit_row + 1
        df = df_raw.iloc[data_start:].copy()
        df.columns = headers
        df = df.reset_index(drop=True)

        return df

    def find_column(self, columns, must_include_all=()):
        normalized = []
        for col in columns:
            raw = str(col).strip().lower()
            base = raw.split("__")[0].strip()
            normalized.append((col, base))

        for original, low in normalized:
            if all(token.lower() in low for token in must_include_all):
                return original
        return None

    def post(self, request, format=None):
        file = request.FILES.get("file")
        if not file:
            return Response({"error": "Aucun fichier fourni."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            df_raw = pd.read_excel(file, header=None)
        except Exception as e:
            return Response(
                {"error": f"Impossible de lire le fichier Excel : {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            df = self.build_headers_from_client_sheet(df_raw)
            #df.columns = [str(c).strip() for c in df.columns]
            df.columns = self.uniquify_columns([str(c).strip() for c in df.columns])
        except Exception as e:
            return Response(
                {
                    "error": f"Impossible de reconstruire les en-têtes : {str(e)}",
                    "preview_raw_shape": list(df_raw.shape),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # debug utile temporaire
        print("GRID TARGET COLUMNS =>", list(df.columns))

        col_configuration = self.find_column(df.columns, ("codes configurations cibles",))
        if not col_configuration:
            col_configuration = self.find_column(df.columns, ("codes configurations",))

        col_load_band = self.find_column(df.columns, ("load bands",))

        fee_cols = self.find_columns(df.columns, ("redevance energie grid", "fcfa"))
        target_cols = self.find_columns(df.columns, ("cible energie grid", "kwh"))
        target_day_cols = self.find_columns(df.columns, ("cible energie grid/j", "kwh"))

        # on prend la 1ère colonne = OUTDOOR, la 2ème = INDOOR
        col_outdoor_fee = fee_cols[0] if len(fee_cols) > 0 else None
        col_indoor_fee = fee_cols[1] if len(fee_cols) > 1 else None

        # attention: exclure les colonnes /J du bloc target classique
        target_cols_no_day = [c for c in target_cols if "/j" not in str(c).lower()]
        col_outdoor_target = target_cols_no_day[0] if len(target_cols_no_day) > 0 else None
        col_indoor_target = target_cols_no_day[1] if len(target_cols_no_day) > 1 else None

        col_outdoor_target_day = target_day_cols[0] if len(target_day_cols) > 0 else None
        col_indoor_target_day = target_day_cols[1] if len(target_day_cols) > 1 else None

        missing = []
        if not col_configuration:
            missing.append("Codes Configurations")
        if not col_load_band:
            missing.append("Load bands TELCO (W)")
        if not col_outdoor_fee:
            missing.append("Outdoor Redevance Energie Grid")
        if not col_indoor_fee:
            missing.append("Indoor Redevance Energie Grid")
        if not col_outdoor_target:
            missing.append("Outdoor Cible Energie Grid")
        if not col_indoor_target:
            missing.append("Indoor Cible Energie Grid")
        if not col_outdoor_target_day:
            missing.append("Outdoor Cible Energie Grid/j")
        if not col_indoor_target_day:
            missing.append("Indoor Cible Energie Grid/j")

        if missing:
            return Response(
                {
                    "error": "Colonnes obligatoires manquantes pour le format client brut.",
                    "missing": missing,
                    "detected_columns": list(df.columns),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_count = 0
        updated_count = 0
        skipped_count = 0
        errors = []

        for index, row in df.iterrows():
            try:
                configuration = self.normalize_str(row.get(col_configuration))
                load_band = self.normalize_load_band(row.get(col_load_band))

                if not configuration and not load_band:
                    skipped_count += 1
                    continue

                if not configuration or not load_band:
                    errors.append({
                        "row": index + 1,
                        "error": "Configuration ou Load Band manquant",
                    })
                    continue

                branches = [
                    {
                        "site_type": "OUTDOOR",
                        "grid_fee_amount": self.normalize_decimal(row.get(col_outdoor_fee)),
                        "target_kwh": self.normalize_decimal(row.get(col_outdoor_target)),
                        "target_kwh_per_day": self.normalize_decimal(row.get(col_outdoor_target_day)),
                    },
                    {
                        "site_type": "INDOOR",
                        "grid_fee_amount": self.normalize_decimal(row.get(col_indoor_fee)),
                        "target_kwh": self.normalize_decimal(row.get(col_indoor_target)),
                        "target_kwh_per_day": self.normalize_decimal(row.get(col_indoor_target_day)),
                    },
                ]

                for rec in branches:
                    if (
                        rec["grid_fee_amount"] is None
                        and rec["target_kwh"] is None
                        and rec["target_kwh_per_day"] is None
                    ):
                        continue

                    obj, created = GridTargetRule.objects.update_or_create(
                        configuration=configuration,
                        site_type=rec["site_type"],
                        load_band=load_band,
                        defaults={
                            "grid_fee_amount": rec["grid_fee_amount"],
                            "target_kwh": rec["target_kwh"],
                            "target_kwh_per_day": rec["target_kwh_per_day"],
                            "active": True,
                            "source_sheet": "Redevance et cible",
                            "source_row": index + 1,
                        },
                    )

                    if created:
                        created_count += 1
                    else:
                        updated_count += 1

            except Exception as e:
                print(
                    "ROW DEBUG =>",
                    index + 1,
                    {
                        "configuration": configuration,
                        "load_band": load_band,
                        "outdoor_fee": self.normalize_decimal(row.get(col_outdoor_fee)),
                        "indoor_fee": self.normalize_decimal(row.get(col_indoor_fee)),
                        "outdoor_target": self.normalize_decimal(row.get(col_outdoor_target)),
                        "indoor_target": self.normalize_decimal(row.get(col_indoor_target)),
                        "outdoor_target_day": self.normalize_decimal(row.get(col_outdoor_target_day)),
                        "indoor_target_day": self.normalize_decimal(row.get(col_indoor_target_day)),
                    },
                )
                errors.append({
                    "row": index + 1,
                    "error": str(e),
                })


        return Response(
            {
                "message": "Import GridTargetRule terminé.",
                "created": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "errors_count": len(errors),
                "errors": errors[:50],
            },
            status=status.HTTP_200_OK,
        )