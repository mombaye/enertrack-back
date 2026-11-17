"""
Tâches automatiques après chaque déploiement (lancées par l'entrypoint, idempotentes, sans effet si rien n'est à faire) :

  1. Pilotage Grid : reconstruit les tables relationnelles si les jeux de données existent mais que les tables sont vides.
  2. Suivi Carburant : recalcule les bilans de stock des périodes récentes qui datent de l'ancienne règle (sans « variation de stock »),
     pour que les variations négatives et les nouveaux champs soient disponibles sans commande manuelle.
  3. Suivi Carburant : applique les règles de bilan en vigueur dès le déploiement (requalification ENOC, recalcul des bilans
     calculés avec une autre version de règle, 2 derniers mois complets).

    python manage.py fuel_post_deploy [--periodes 3] [--dry-run]
Aucune certification n'est jamais créée ni modifiée ici ; ENOC et Snowflake ne sont ni lus en écriture ni modifiés.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Tâches post-déploiement idempotentes (tables Pilotage Grid, recalcul des bilans obsolètes)."

    def add_arguments(self, parser):
        parser.add_argument("--periodes", type=int, default=3, help="Nombre de périodes récentes à examiner.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, periodes, dry_run, **opts):
        w = self.stdout.write
        self._pilotage(w, dry_run)
        self._bilans(w, periodes, dry_run)
        self._regles_mvp(w, periodes, dry_run)
        self._ponctions_output(w, dry_run)
        self._sans_ge_output(w, dry_run)
        self._kpis_closing(w, dry_run)

    def _kpis_closing(self, w, dry_run):
        """6. Closing importés avant le 17/11/2026 : enregistre le bloc de KPI de l'en-tête (relu dans la copie d'archive)."""
        if dry_run:
            return
        try:
            from fuel_tracking.services.closing_kpis import backfill_file_kpis

            w(f"Closing : KPI de l'en-tête enregistrés pour {backfill_file_kpis()} lot(s).")
        except Exception as e:  # ne jamais empêcher le démarrage du service
            w(f"Closing : KPI de l'en-tête impossibles ({e}).")

    def _sans_ge_output(self, w, dry_run):
        """5. Lots Output App Fuel importés avant le 17/11/2026 : enregistre la liste des sites « sans GE et sans cuve » (relue
           dans la copie d'archive) pour les exclure de la santé des données et des totaux du mois."""
        if dry_run:
            return
        try:
            from fuel_tracking.services.output_app_fuel import backfill_sites_without_genset

            w(f"Output App Fuel : liste des sites sans GE complétée pour {backfill_sites_without_genset()} lot(s).")
        except Exception as e:  # ne jamais empêcher le démarrage du service
            w(f"Output App Fuel : liste des sites sans GE impossible ({e}).")

    def _ponctions_output(self, w, dry_run):
        """
        4. Correctif du 17/11/2026 : les relevés importés d'un fichier Output App Fuel avant ce correctif comptaient deux fois les
           ponctions (« Total Ponction » ajouté à « Total Ajout » − « Total Retrait », qui l'incluent déjà). Corrige leur contexte
           (rajout_retrait_l −= ponction_l, une seule fois : marqueur « ponction_corrigee ») puis recalcule les bilans des mois couverts.
        """
        try:
            from fuel_tracking.models import FuelImportBatch, FuelStockObservation as Obs
            from fuel_tracking.services.closing_upload import months_covered, recompute_months

            todo = [o for o in Obs.objects.filter(source_type="CLOSING", source_context__fichier="Output App Fuel").only("id", "batch_id", "source_context")
                    if not (o.source_context or {}).get("ponction_corrigee")]
            w(f"Output App Fuel : {len(todo)} relevé(s) au contexte de ponction à corriger.")
            if dry_run or not todo:
                return
            batches = set()
            for o in todo:
                ctx = dict(o.source_context or {})
                if ctx.get("rajout_retrait_l") is not None and ctx.get("ponction_l"):
                    ctx["rajout_retrait_l"] = ctx["rajout_retrait_l"] - ctx["ponction_l"]
                ctx["ponction_corrigee"] = True
                Obs.objects.filter(pk=o.pk).update(source_context=ctx)
                batches.add(o.batch_id)
            for b in FuelImportBatch.objects.filter(pk__in=[x for x in batches if x]):
                res = recompute_months(months_covered(b), track=[b])
                w(f"  Lot #{b.pk} ({b.file_name}) : bilans recalculés {[(m['debut'][:7], m['calculables'], m['sites']) for m in res]}")
        except Exception as e:  # ne jamais empêcher le démarrage du service
            w(f"Output App Fuel : correctif des ponctions impossible ({e}).")

    def _regles_mvp(self, w, periodes, dry_run):
        """
        3. Règles en vigueur (ex. MVP du 16/11/2026) : dès le déploiement, sans attendre la synchro de 5 min ni le calcul de 3 h 11 :
           a. requalifie les mouvements ENOC avec les règles de statut courantes (ex. demandes en attente désormais éligibles) ;
           b. recalcule, du plus ancien au plus récent, les bilans des périodes récentes calculés avec une autre version de règle
              (le stock initial d'une période reprend le stock final de la précédente) ;
           c. calcule les 2 derniers mois complets s'ils manquent.
        """
        try:
            from fuel_tracking.models import FuelStockBalance as B
            from fuel_tracking.services import stock_period_balance as SB
            from fuel_tracking.services.auto_balances import compute_recent_months
            from fuel_tracking.services.stock_enoc import sync_enoc_to_registry

            version = SB.current_rule_version()
            if version != SB.MVP_RULE_VERSION:
                w(f"Règles des bilans ({version}) : règles MVP inactives, rien à appliquer.")
                return
            periods =list(B.objects.values_list("observation_start", "observation_end").distinct().order_by("-observation_end")[:periodes])
            stale = [(s, e) for s, e in periods if B.objects.filter(observation_start=s, observation_end=e).exclude(rule_version=version).exists()]
            w(f"Règles des bilans ({version}) : {len(stale)} période(s) récente(s) à recalculer.")
            if dry_run:
                return
            st = sync_enoc_to_registry()
            w(f"  ENOC requalifié : {st.get('qualification', {})}")
            for s, e in sorted(stale, key=lambda p: p[1]):
                rows = SB.compute_balances(s, e)
                w(f"  Bilans {s:%d/%m/%Y} → {e:%d/%m/%Y} recalculés : {len(rows)} site(s).")
            for r in compute_recent_months(2):
                w(f"  Mois {r['debut']} → {r['fin']} : {r['sites']} site(s), {r['calculables']} calculable(s).")
        except Exception as e:  # ne jamais empêcher le démarrage du service
            w(f"Règles des bilans : recalcul impossible ({e}).")

    def _pilotage(self, w, dry_run):
        try:
            from pathlib import Path

            from django.conf import settings
            from django.core.management import call_command

            from pilotage_grid.models import PgInvoiceFact, PgSite, PilotageDataset
            from pilotage_grid.services import tables

            initial = Path(settings.BASE_DIR) / "data_imports" / "pilotage_grid" / "pilotage_grid_initial.html"
            if initial.is_file() and not dry_run:   # jeux initiaux versionnés dans le dépôt : chargés seulement s'ils manquent en base
                call_command("pilotage_seed_from_html", only_missing=True, stdout=self.stdout)

            has_data = PilotageDataset.objects.filter(is_active=True, key__in=("paiements", "sites")).exists()
            empty = not PgInvoiceFact.objects.exists() or not PgSite.objects.exists()
            if has_data and empty:
                w("Pilotage Grid : tables vides alors que les jeux de données existent — reconstruction." if not dry_run else "Pilotage Grid : reconstruction nécessaire (dry-run).")
                if not dry_run:
                    w(f"  {tables.rebuild_all()}")
            else:
                w("Pilotage Grid : tables à jour." if has_data else "Pilotage Grid : aucun jeu de données en base (import initial à faire).")
        except Exception as e:  # ne jamais empêcher le démarrage du service
            w(f"Pilotage Grid : vérification impossible ({e}).")

    def _bilans(self, w, periodes, dry_run):
        try:
            from django.db.models import Q

            from fuel_tracking.models import FuelStockBalance as B
            from fuel_tracking.services import stock_period_balance as SB

            periods = list(B.objects.values_list("observation_start", "observation_end").distinct().order_by("-observation_end")[:periodes])
            for start, end in periods:
                stale = B.objects.filter(observation_start=start, observation_end=end, variation_stock_l__isnull=True).filter(
                    Q(consommation_reelle_l__isnull=False) | Q(cause_principale="CONSO_NEGATIVE")).count()
                if not stale:
                    w(f"Bilans {start:%d/%m/%Y} → {end:%d/%m/%Y} : à jour.")
                    continue
                w(f"Bilans {start:%d/%m/%Y} → {end:%d/%m/%Y} : {stale} bilan(s) sans variation de stock — recalcul.")
                if not dry_run:
                    SB.compute_balances(start, end)
        except Exception as e:
            w(f"Bilans : recalcul impossible ({e}).")
