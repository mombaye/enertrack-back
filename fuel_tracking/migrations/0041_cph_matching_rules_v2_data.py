# Données (séparées du schéma 0040, cf. 0039 : PostgreSQL refuse un CREATE INDEX après des
# UPDATE de clé étrangère dans la même transaction).
#  - les retraits manuels deviennent REJETE ;
#  - la correspondance automatique est réévaluée avec la règle v2 (tolérance puissance
#    configurable, 15 % par défaut, sur le kVA de la courbe) ; chaque changement est historisé ;
#  - un état initial est historisé pour les mappages sans historique.

from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    from fuel_tracking.services.cph_matching import METHOD_MANUAL_REMOVAL, REJETE, apply_auto_matching, record_history

    Mapping = apps.get_model("fuel_tracking", "CphInventoryMapping")
    Curve = apps.get_model("fuel_tracking", "CphCurve")
    History = apps.get_model("fuel_tracking", "CphMappingHistory")
    now = timezone.now()
    Mapping.objects.filter(match_method=METHOD_MANUAL_REMOVAL).update(match_status=REJETE)
    apply_auto_matching(Mapping.objects.all(), list(Curve.objects.all()), now, history_model=History)
    curve_ids = dict(Curve.objects.values_list("pk", "curve_id"))
    for m in Mapping.objects.exclude(pk__in=History.objects.values("mapping_id")):
        cid = curve_ids.get(m.validated_curve_id)
        record_history(History, m, "", None, cid, f"état initial ({m.match_method or 'import abaque'})", now,
                       comment="; ".join(m.match_reasons or [])[:1000])


class Migration(migrations.Migration):

    dependencies = [
        ("fuel_tracking", "0040_cph_mapping_history"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
