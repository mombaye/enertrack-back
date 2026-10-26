from django.db import migrations


def forwards(apps, schema_editor):
    Movement = apps.get_model("fuel_tracking", "FuelMovement")
    # Ponction : un type par sens (sortant au site source, entrant au site destinataire)
    Movement.objects.filter(movement_type="TRANSFERT_INTERSITE", movement_direction="OUT").update(movement_type="TRANSFERT_INTERSITE_SORTANT")
    Movement.objects.filter(movement_type="TRANSFERT_INTERSITE", movement_direction="IN").update(movement_type="TRANSFERT_INTERSITE_ENTRANT")
    Balance = apps.get_model("fuel_tracking", "FuelStockBalance")
    # Anciens statuts de bilan → trois résultats (les bilans sont recalculables ; la cause détaillée est conservée)
    for old, new in (("PRET_A_CERTIFIER_AVEC_RESERVE", "ESTIMATION_AVEC_RESERVE"), ("DONNEES_STOCK_INCOMPLETES", "NON_CALCULABLE"),
                     ("MOUVEMENTS_A_CONTROLER", "NON_CALCULABLE"), ("HISTORIQUE_NON_VERIFIABLE", "NON_CALCULABLE")):
        Balance.objects.filter(statut_bilan=old).update(statut_bilan=new, cause_principale=old, resultat_bilan=new)
    Balance.objects.filter(statut_bilan="PRET_A_CERTIFIER").update(resultat_bilan="PRET_A_CERTIFIER")


def backwards(apps, schema_editor):
    Movement = apps.get_model("fuel_tracking", "FuelMovement")
    Movement.objects.filter(movement_type__in=("TRANSFERT_INTERSITE_SORTANT", "TRANSFERT_INTERSITE_ENTRANT")).update(movement_type="TRANSFERT_INTERSITE")


class Migration(migrations.Migration):
    dependencies = [("fuel_tracking", "0050_mvp_estimation_scores")]
    operations = [migrations.RunPython(forwards, backwards)]
