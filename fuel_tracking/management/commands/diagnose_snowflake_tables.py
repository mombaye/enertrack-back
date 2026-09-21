# fuel_tracking/management/commands/diagnose_snowflake_tables.py
"""
Diagnostic Snowflake : vérifie l'accessibilité des bases/tables utilisées par
le pipeline fuel-tracking et affiche les métriques de couverture August/Sept 2026.

Usage (dans le conteneur Docker ou localement) :
    python manage.py diagnose_snowflake_tables
    python manage.py diagnose_snowflake_tables --month 2026-08

Ne fait aucune écriture (lecture seule). Identifiants pris dans settings/env.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Diagnostic lecture seule — accessibilité tables Snowflake et couverture mensuelle"

    def add_arguments(self, parser):
        parser.add_argument(
            "--month",
            default="2026-08",
            help="Mois à auditer (YYYY-MM, défaut 2026-08)",
        )

    def handle(self, *args, **options):
        import snowflake.connector
        from django.conf import settings

        month_str = options["month"]
        year, mo = int(month_str[:4]), int(month_str[5:7])
        import calendar
        d_start = f"{year:04d}-{mo:02d}-01"
        d_end = f"{year:04d}-{mo:02d}-{calendar.monthrange(year, mo)[1]:02d}"

        W = self.style.WARNING
        OK = self.style.SUCCESS
        ERR = self.style.ERROR

        def connect(database):
            kwargs = dict(
                account=settings.SNOWFLAKE_ACCOUNT,
                user=settings.SNOWFLAKE_USER,
                warehouse=settings.SNOWFLAKE_WAREHOUSE,
                role=settings.SNOWFLAKE_ROLE,
                database=database,
                schema="GOLD",
            )
            if getattr(settings, "SNOWFLAKE_PRIVATE_KEY_PATH", None):
                kwargs["authenticator"] = "SNOWFLAKE_JWT"
                kwargs["private_key_file"] = settings.SNOWFLAKE_PRIVATE_KEY_PATH
                if getattr(settings, "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", None):
                    kwargs["private_key_file_pwd"] = settings.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE
            else:
                kwargs["password"] = settings.SNOWFLAKE_PASSWORD
            return snowflake.connector.connect(**kwargs)

        def try_count(cursor, sql, params=None):
            try:
                cursor.execute(sql, params or {})
                row = cursor.fetchone()
                return row[0] if row else 0
            except Exception as e:
                return f"ERREUR: {e}"

        def try_describe(cursor, fqn):
            try:
                cursor.execute(f"DESCRIBE TABLE {fqn}")
                return [r[0] for r in cursor.fetchall()]
            except Exception as e:
                return f"ERREUR: {e}"

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write(f"  DIAGNOSTIC SNOWFLAKE — mois {month_str} ({d_start} → {d_end})")
        self.stdout.write("═" * 80 + "\n")

        # ── 1. DB_GFMS_ANALYTICS_DEV (VW_FUEL_REPORT) ───────────────────────
        self.stdout.write("── 1. DB_GFMS_ANALYTICS_DEV.GOLD ──")
        try:
            conn_dev = connect("DB_GFMS_ANALYTICS_DEV")
            cur = conn_dev.cursor()
            self.stdout.write(OK("  Connexion OK"))
            n = try_count(
                cur,
                "SELECT COUNT(*) FROM DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT "
                "WHERE COUNTRY = 'Senegal' AND DATE >= %(s)s AND DATE <= %(e)s",
                {"s": d_start, "e": d_end},
            )
            if isinstance(n, str):
                self.stdout.write(ERR(f"  VW_FUEL_REPORT → {n}"))
            else:
                self.stdout.write(OK(f"  VW_FUEL_REPORT → {n} lignes Sénégal {month_str}") if n > 0
                                  else W(f"  VW_FUEL_REPORT → 0 lignes Sénégal {month_str} (base vide pour ce mois ?)"))
                cols = try_describe(cur, "DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT")
                if isinstance(cols, list):
                    self.stdout.write(f"  Colonnes : {', '.join(cols)}")
                else:
                    self.stdout.write(ERR(f"  DESCRIBE → {cols}"))
            conn_dev.close()
        except Exception as e:
            self.stdout.write(ERR(f"  Connexion DB_GFMS_ANALYTICS_DEV échouée : {e}"))

        # ── 2. DB_GFMS_PROD (SITE_FILTERED, GE_PROD_KWH, capteur, niveau) ──
        self.stdout.write("\n── 2. DB_GFMS_PROD.GOLD ──")
        try:
            conn_prod = connect("DB_GFMS_PROD")
            cur = conn_prod.cursor()
            self.stdout.write(OK("  Connexion OK"))

            for table, sql, params in [
                ("SITE_FILTERED (Sénégal)",
                 "SELECT COUNT(*) FROM DB_GFMS_PROD.GOLD.SITE_FILTERED WHERE COUNTRY = 'Senegal'",
                 None),
                ("GE_PROD_KWH",
                 "SELECT COUNT(*) FROM DB_GFMS_PROD.GOLD.GE_PROD_KWH WHERE DATE >= %(s)s AND DATE <= %(e)s",
                 {"s": d_start, "e": d_end}),
                ("GFMS_FUEL_SENSOR_MONITORING_DATA",
                 "SELECT COUNT(*) FROM DB_GFMS_PROD.GOLD.GFMS_FUEL_SENSOR_MONITORING_DATA WHERE DATE >= %(s)s AND DATE <= %(e)s",
                 {"s": d_start, "e": d_end}),
                ("TANK_LEVEL_AVG",
                 "SELECT COUNT(*) FROM DB_GFMS_PROD.GOLD.TANK_LEVEL_AVG WHERE DATE >= %(s)s AND DATE <= %(e)s",
                 {"s": d_start, "e": d_end}),
                ("GFMS_DATA_TRACKER_NC",
                 "SELECT COUNT(*) FROM DB_GFMS_PROD.GOLD.GFMS_DATA_TRACKER_NC "
                 "WHERE \"TIMESTAMP\" >= %(s)s AND \"TIMESTAMP\" < %(e)s LIMIT 1",
                 {"s": d_start, "e": d_end}),
                ("RECTIFIER_EFFICIENCY_STATUS",
                 "SELECT COUNT(*) FROM DB_GFMS_PROD.GOLD.RECTIFIER_EFFICIENCY_STATUS "
                 "WHERE \"TIMESTAMP\" >= %(s)s AND \"TIMESTAMP\" < %(e)s",
                 {"s": d_start, "e": d_end}),
            ]:
                n = try_count(cur, sql, params)
                if isinstance(n, str):
                    self.stdout.write(W(f"  {table} → {n}"))
                else:
                    self.stdout.write(OK(f"  {table} → {n} lignes") if n > 0
                                      else W(f"  {table} → 0 lignes"))
            conn_prod.close()
        except Exception as e:
            self.stdout.write(ERR(f"  Connexion DB_GFMS_PROD échouée : {e}"))

        # ── 3. DB_GFMS_ANALYTICS_PROD (GENSET_REPORT + autres) ──────────────
        self.stdout.write("\n── 3. DB_GFMS_ANALYTICS_PROD.GOLD ──")
        try:
            conn_aprod = connect("DB_GFMS_ANALYTICS_PROD")
            cur = conn_aprod.cursor()
            self.stdout.write(OK("  Connexion OK"))

            # GENSET_REPORT — schéma complet
            cols = try_describe(cur, "DB_GFMS_ANALYTICS_PROD.GOLD.GENSET_REPORT")
            if isinstance(cols, list):
                self.stdout.write(f"  GENSET_REPORT colonnes ({len(cols)}) : {', '.join(cols)}")
            else:
                self.stdout.write(ERR(f"  GENSET_REPORT DESCRIBE → {cols}"))

            # Couverture août
            for table, sql, params in [
                ("GENSET_REPORT (Sénégal, nb lignes)",
                 "SELECT COUNT(*) FROM DB_GFMS_ANALYTICS_PROD.GOLD.GENSET_REPORT g "
                 "JOIN DB_GFMS_ANALYTICS_PROD.GOLD.SITE_FILTERED s ON s.DATA_ID = g.DATA_ID "
                 "WHERE s.COUNTRY = 'Senegal' AND g.REPORT_DATE >= %(s)s AND g.REPORT_DATE <= %(e)s",
                 {"s": d_start, "e": d_end}),
                ("GENSET_REPORT (nb sites distincts, Sénégal)",
                 "SELECT COUNT(DISTINCT g.DATA_ID) FROM DB_GFMS_ANALYTICS_PROD.GOLD.GENSET_REPORT g "
                 "JOIN DB_GFMS_ANALYTICS_PROD.GOLD.SITE_FILTERED s ON s.DATA_ID = g.DATA_ID "
                 "WHERE s.COUNTRY = 'Senegal' AND g.REPORT_DATE >= %(s)s AND g.REPORT_DATE <= %(e)s",
                 {"s": d_start, "e": d_end}),
                ("SITE_FILTERED (Sénégal)",
                 "SELECT COUNT(*) FROM DB_GFMS_ANALYTICS_PROD.GOLD.SITE_FILTERED WHERE COUNTRY = 'Senegal'",
                 None),
                ("LOAD_REPORT",
                 "SELECT COUNT(*) FROM DB_GFMS_ANALYTICS_PROD.GOLD.LOAD_REPORT "
                 "WHERE DATE >= %(s)s AND DATE <= %(e)s",
                 {"s": d_start, "e": d_end}),
            ]:
                n = try_count(cur, sql, params)
                if isinstance(n, str):
                    self.stdout.write(W(f"  {table} → {n}"))
                else:
                    self.stdout.write(OK(f"  {table} → {n}") if n > 0
                                      else W(f"  {table} → 0"))

            # VW_INVOICE_DATA_REPORT (pour classification hybride CPH)
            n = try_count(cur,
                "SELECT COUNT(*) FROM DB_GFMS_ANALYTICS_PROD.GOLD.VW_INVOICE_DATA_REPORT "
                "WHERE \"Country\" = 'Senegal' AND \"Date\" >= %(s)s AND \"Date\" <= %(e)s",
                {"s": d_start, "e": d_end})
            if isinstance(n, str):
                self.stdout.write(W(f"  VW_INVOICE_DATA_REPORT → {n}"))
            else:
                self.stdout.write(OK(f"  VW_INVOICE_DATA_REPORT → {n} lignes") if n > 0
                                  else W(f"  VW_INVOICE_DATA_REPORT → 0 lignes (ou table absente)"))

            # GENSET_REPORT : colonnes clés nullable pour fuel tank
            self.stdout.write("\n  GENSET_REPORT — échantillon colonnes fuel/runtime (10 lignes Sénégal) :")
            try:
                cur.execute(
                    "SELECT g.REPORT_DATE, s.SITE_ID, g.DG_RUNTIME_CONTROLLER, g.DG_RUNTIME_CALCULATED, "
                    "g.FUEL_TANK_LEVEL, g.FUEL_CONSUMED, g.DG_FUEL_LEVEL, g.FUEL_LEVEL_START, "
                    "g.FUEL_LEVEL_END, g.FUEL_CONSUMPTION "
                    "FROM DB_GFMS_ANALYTICS_PROD.GOLD.GENSET_REPORT g "
                    "JOIN DB_GFMS_ANALYTICS_PROD.GOLD.SITE_FILTERED s ON s.DATA_ID = g.DATA_ID "
                    "WHERE s.COUNTRY = 'Senegal' AND g.REPORT_DATE >= %(s)s AND g.REPORT_DATE <= %(e)s "
                    "LIMIT 10",
                    {"s": d_start, "e": d_end},
                )
                rows = cur.fetchall()
                desc = [d[0] for d in cur.description]
                self.stdout.write("  " + " | ".join(desc))
                for row in rows:
                    self.stdout.write("  " + " | ".join(str(v) for v in row))
            except Exception as e:
                self.stdout.write(W(f"  Échantillon fuel échoué (colonnes peut-être absentes) : {e}"))
                self.stdout.write("  Retente avec colonnes de base (REPORT_DATE, DATA_ID, DG_RUNTIME_CONTROLLER, DG_RUNTIME_CALCULATED) :")
                try:
                    cur.execute(
                        "SELECT g.REPORT_DATE, s.SITE_ID, g.DG_RUNTIME_CONTROLLER, g.DG_RUNTIME_CALCULATED "
                        "FROM DB_GFMS_ANALYTICS_PROD.GOLD.GENSET_REPORT g "
                        "JOIN DB_GFMS_ANALYTICS_PROD.GOLD.SITE_FILTERED s ON s.DATA_ID = g.DATA_ID "
                        "WHERE s.COUNTRY = 'Senegal' AND g.REPORT_DATE >= %(s)s AND g.REPORT_DATE <= %(e)s "
                        "LIMIT 10",
                        {"s": d_start, "e": d_end},
                    )
                    rows = cur.fetchall()
                    desc = [d[0] for d in cur.description]
                    self.stdout.write("  " + " | ".join(desc))
                    for row in rows:
                        self.stdout.write("  " + " | ".join(str(v) for v in row))
                except Exception as e2:
                    self.stdout.write(ERR(f"  Échec total : {e2}"))

            conn_aprod.close()
        except Exception as e:
            self.stdout.write(ERR(f"  Connexion DB_GFMS_ANALYTICS_PROD échouée : {e}"))

        # ── 4. Résumé PostgreSQL local ────────────────────────────────────────
        self.stdout.write("\n── 4. FuelConsommationSyncRun (PostgreSQL) — 5 dernières runs ──")
        try:
            from fuel_tracking.models import FuelConsommationSyncRun
            runs = FuelConsommationSyncRun.objects.order_by("-created_at")[:5]
            for r in runs:
                self.stdout.write(
                    f"  {r.created_at:%Y-%m-%d %H:%M} | {r.month_from}→{r.month_to} | "
                    f"{r.status} | sites={r.sites_fetched} | err={r.error_message or '-'}"
                )
        except Exception as e:
            self.stdout.write(W(f"  Lecture FuelConsommationSyncRun échouée : {e}"))

        self.stdout.write("\n── 5. FuelCphSyncRun (PostgreSQL) — 5 dernières runs ──")
        try:
            from fuel_tracking.models import FuelCphSyncRun
            runs = FuelCphSyncRun.objects.order_by("-created_at")[:5]
            for r in runs:
                self.stdout.write(
                    f"  {r.created_at:%Y-%m-%d %H:%M} | {r.month_from}→{r.month_to} | "
                    f"{r.status} | sites={r.sites_synced} | err={r.error_message or '-'}"
                )
        except Exception as e:
            self.stdout.write(W(f"  Lecture FuelCphSyncRun échouée : {e}"))

        self.stdout.write("\n── 6. FuelConsommationMonthly 2026-08 — état PostgreSQL ──")
        try:
            from django.db.models import Count, Q
            from fuel_tracking.models import FuelConsommationMonthly
            agg = FuelConsommationMonthly.objects.filter(month_year="2026-08").aggregate(
                total=Count("id"),
                avec_conso=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
                avec_cph=Count("id", filter=Q(cph_runtime_h_total__isnull=False)),
            )
            self.stdout.write(
                f"  Total lignes 2026-08 : {agg['total']} | "
                f"conso_snowflake_l non-null : {agg['avec_conso']} | "
                f"cph_runtime_h_total non-null : {agg['avec_cph']}"
            )
        except Exception as e:
            self.stdout.write(W(f"  Lecture FuelConsommationMonthly échouée : {e}"))

        self.stdout.write("\n" + "═" * 80 + "\n")
