"""
Priorités de fiabilisation et cuves à traiter, en ligne de commande (mêmes règles que l'écran Stock › Priorités et Cuves à traiter).
LECTURE SEULE.

    python manage.py fuel_priorites --debut 2026-08-01 --fin 2026-08-31 --top 10
"""
import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from rest_framework.test import APIRequestFactory, force_authenticate

from fuel_tracking.views_mvp import PrioritiesView, TanksToProcessView


class Command(BaseCommand):
    help = "Affiche les priorités de fiabilisation et les cuves à traiter (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--debut", default=None)
        parser.add_argument("--fin", default=None)
        parser.add_argument("--top", type=int, default=10, help="Lignes affichées par catégorie.")

    def handle(self, *args, debut, fin, top, **opts):
        user = get_user_model().objects.filter(is_active=True).order_by("-is_superuser").first()
        if user is None:
            raise CommandError("Aucun utilisateur actif.")
        f, w = APIRequestFactory(), self.stdout.write
        params = {k: v for k, v in (("debut", debut), ("fin", fin), ("limit", 200)) if v}
        req = f.get("/", params)
        force_authenticate(req, user=user)
        pr = PrioritiesView.as_view()(req).data
        w(f"Priorités de fiabilisation — période {pr['periode']['debut']} → {pr['periode']['fin']}")
        w(json.dumps(pr["totaux"], ensure_ascii=False))
        by_cat: dict[str, list] = {}
        for it in pr["data"]:
            by_cat.setdefault(it["categorie"], []).append(it)
        for cat, items in by_cat.items():
            w(f"\n— {pr['libelles'].get(cat, cat)} ({pr['totaux'].get(cat)})")
            for it in items[:top]:
                w(f"   #{it['rang']} {it['site_id']}{' cuve ' + it['cuve'] if it.get('cuve') else ''} · impact {it.get('impact_l')} {it.get('impact_unite', 'L')} · {it['motif'][:110]} → {it['action']}")
        req = f.get("/", {"limit": top})
        force_authenticate(req, user=user)
        tk = TanksToProcessView.as_view()(req).data
        w("\nCuves à traiter")
        w(f"  catégories : {json.dumps(tk['totaux_categories'], ensure_ascii=False)}")
        w(f"  utilisation : {json.dumps(tk['totaux_usage'], ensure_ascii=False)}")
        for r in tk["data"][:top]:
            w(f"   {r['site_id']} cuve {r['cuve']} · {','.join(r['categories'])} · plaque {r['plaque_l']} L · géométrie {r['geometrie_l']} L · "
              f"bilans impactés {r['bilans_impactes']} · relevés bloqués {r['releves_bloques']}, estimés {r['releves_estimes']} → {r['action']}")
