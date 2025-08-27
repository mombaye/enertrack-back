from venv import logger
from celery import shared_task
import pandas as pd
from io import BytesIO
from .models import Facture
from core.models import Site
from invoices.utils.parsers import safe_date, safe_decimal, safe_float, safe_int, safe_str
from django.db import transaction

@shared_task
def import_factures_task(file_bytes):
    df = pd.read_excel(BytesIO(file_bytes))
    
    sites = {s.name: s for s in Site.objects.all()}
    factures_existantes = {
        f.facture_number: f for f in Facture.objects.filter(facture_number__in=df['FACTURE'].dropna().unique())
    }

    to_create, to_update, errors = [], [], []
    created, updated, skipped = 0, 0, 0

    for idx, row in df.iterrows():
        try:
            site = sites.get(row['SITE'])
            if not site:
                skipped += 1
                errors.append(f"Ligne {idx+2}: site inconnu '{row['SITE']}'")
                continue

            facture_number = safe_str(row['FACTURE'])
            if not facture_number:
                skipped += 1
                errors.append(f"Ligne {idx+2}: numéro de facture vide")
                continue
        
            print(row.get('CONS FACTURÉE'))

            data = {
                'site': site,
                'police_number': safe_str(row.get('N° POLICE')),
                'contrat_number': safe_str(row.get('N°COMPTE CONTRAT')),
                "typologie": safe_str(row.get('TYPOLOGIE')),
                "categorie": safe_str(row.get('CATEGORIE')),
                "societe": safe_str(row.get('SOCIÉTÉ')),
                "type_police": safe_str(row.get('TYPE POLICE')),
                'date_facture': safe_date(row.get('DATE FACTURE')),
                'date_echeance': safe_date(row.get('ÉCHÉANCE')),
                'montant_ht': safe_decimal(row.get('MONTANT HT')),
                'montant_tco': safe_decimal(row.get('MONTANT TCO')),
                'montant_redevance': safe_decimal(row.get('MONTANT REDEVANCE')),
                'montant_tva': safe_decimal(row.get('MONTANT TVA')),
                'montant_ttc': safe_decimal(row.get('MONTANT TTC')),
                'montant_htva': safe_decimal(row.get('MONTANT HTVA')),
                'montant_energie': safe_decimal(row.get('MONTANT ENERGIE')),
                'montant_cosphi': safe_decimal(row.get('MONTANT COSPHI')),
                'date_ai': safe_date(row.get('DATE AI')),
                'date_ni': safe_date(row.get('DATE NI')),
                'index_ai_k1': safe_int(row.get('INDEX AI K1')),
                'index_ai_k2': safe_int(row.get('INDEX AI K2')),
                'index_ni_k1': safe_int(row.get('INDEX NI K1')),
                'index_ni_k2': safe_int(row.get('INDEX NI K2')),
                'consommation_kwh': safe_decimal(row.get('CONS FACTURÉE')),
                'rappel_majoration': safe_decimal(row.get('RAPPEL MAJORATION')),
                'nb_jours': safe_int(row.get('NOMBRE DE JOURS')),
                'ps': safe_float(row.get('PS')),
                'max_relevee': safe_float(row.get('MAX RELEVEE')),
                'statut': safe_str(row.get('STATUT')),
                'observation': safe_str(row.get('OBSERVATION')),
                'prime_fixe': safe_decimal(row.get('PRIME FIXE')),
                'conso_reactif': safe_decimal(row.get('CONSO REACTIF')),
                'cos_phi': safe_float(row.get('COS PHI')),
                'mois_echeance': safe_str(row.get('MOIS ECHEANCE')),
                'annee_echeance': safe_int(row.get('ANNEE ECHEANCE')),
                'mois_business': safe_str(row.get('MOIS BUSINESS')),
                'annee_business': safe_int(row.get('ANNÉE')),
                'type_tarif': safe_str(row.get('TYPE DE TARIF')),
                'type_compte': safe_str(row.get('TYPE COMPTE')),
                'numero_compteur': safe_str(row.get('N° COMPTEUR')),
            }

            if facture_number in factures_existantes:
                f = factures_existantes[facture_number]
                for k, v in data.items():
                    setattr(f, k, v)
                to_update.append(f)
                updated += 1
            else:
                to_create.append(Facture(facture_number=facture_number, **data))
                created += 1

        except Exception as e:
            print(e)
            skipped += 1
            errors.append(f"Ligne {idx+2}: {str(e)}")

    logger.warning(f"Avant transaction: créer {len(to_create)}, MAJ {len(to_update)}")
    with transaction.atomic():
        Facture.objects.bulk_create(to_create, batch_size=1000)
        Facture.objects.bulk_update(to_update, fields=data.keys(), batch_size=1000)
    logger.warning(f"Avant transaction: créer {len(to_create)}, MAJ {len(to_update)}")

    return {
        "message": f"{created} créées, {updated} modifiées",
        "skipped": skipped,
        "errors": errors
    }
