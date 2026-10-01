# Données : correspondance automatique plaque → courbe sur les mappages existants.
# Séparée de 0038 (schéma) : sous PostgreSQL, créer un index dans la même transaction
# que des UPDATE touchant une clé étrangère échoue (« pending trigger events »).

from django.db import migrations
from django.utils import timezone


def run_auto_matching(apps, schema_editor):
    """Validations humaines existantes → VALIDE_MANUELLEMENT ; les autres mappages sont évalués automatiquement."""
    from fuel_tracking.services.cph_matching import METHOD_MANUAL, VALIDE_MANUELLEMENT, apply_auto_matching

    Mapping = apps.get_model("fuel_tracking", "CphInventoryMapping")
    Curve = apps.get_model("fuel_tracking", "CphCurve")
    now = timezone.now()
    # Seule une validation portant un auteur est humaine ; une courbe sans auteur vient de
    # l'automatique (cas d'un retour arrière puis ré-application) et est réévaluée.
    Mapping.objects.exclude(validated_curve=None).exclude(validated_by=None).update(
        match_status=VALIDE_MANUELLEMENT, match_method=METHOD_MANUAL, matched_at=now)
    apply_auto_matching(Mapping.objects.all(), list(Curve.objects.all()), now)


def backwards(apps, schema_editor):
    """Retour arrière : retirer les courbes posées par l'automatique (aucun auteur) ;
    les validations humaines sont conservées."""
    Mapping = apps.get_model("fuel_tracking", "CphInventoryMapping")
    Mapping.objects.exclude(validated_curve=None).filter(validated_by=None).update(
        validated_curve=None, validated_at=None, validation_comment="")


class Migration(migrations.Migration):

    dependencies = [
        ("fuel_tracking", "0038_cph_auto_matching_measured_conso"),
    ]

    operations = [
        migrations.RunPython(run_auto_matching, backwards),
    ]
