# certification/management/commands/diagnose_acm.py
"""
Diagnostic ciblé sur la table AC Meter Report Day.
v2 — utilise act_energy_p directement (énergie active kWh/jour)

Usage:
    docker compose exec web python manage.py diagnose_acm
    docker compose exec web python manage.py diagnose_acm --site-id DKR_0698
    docker compose exec web python manage.py diagnose_acm --site-id DKR_0698 --days 90
    docker compose exec web python manage.py diagnose_acm --site-id DKR_0698 --date-debut 2025-10-01 --date-fin 2025-10-31
"""
import sys
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand

from certification.services.efms import EfmsService


class Command(BaseCommand):
    help = "Diagnostique act_energy_p dans silver.gfms_AC_Meter_Report_day"

    def add_arguments(self, parser):
        parser.add_argument("--site-id",    type=str, default=None)
        parser.add_argument("--days",       type=int, default=60,
                            help="Fenêtre en jours (défaut: 60). Ignoré si --date-debut/fin fournis.")
        parser.add_argument("--date-debut", type=str, default=None,
                            help="YYYY-MM-DD")
        parser.add_argument("--date-fin",   type=str, default=None,
                            help="YYYY-MM-DD")

    def handle(self, *args, **options):
        efms    = EfmsService()
        site_id = options.get("site_id")
        days    = options["days"]

        # ── Période ──────────────────────────────────────────────────────────
        if options["date_debut"] and options["date_fin"]:
            date_debut = date.fromisoformat(options["date_debut"])
            date_fin   = date.fromisoformat(options["date_fin"])
        else:
            date_fin   = date.today()
            date_debut = date_fin - timedelta(days=days - 1)

        nb_jours_periode = (date_fin - date_debut).days + 1

        self.stdout.write("\n" + "═" * 65)
        self.stdout.write("  DIAGNOSTIC — silver.gfms_AC_Meter_Report_day  (v2)")
        self.stdout.write("═" * 65 + "\n")

        # ── Connexion ─────────────────────────────────────────────────────────
        try:
            conn = efms._open_connection()
            self.stdout.write(self.style.SUCCESS("  ✓ Connexion SQL Server OK\n"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  ✗ Connexion échouée : {e}"))
            sys.exit(1)

        cursor = conn.cursor()
        TABLE  = efms.TABLE_ACM

        # ── 1. Colonnes énergie disponibles ───────────────────────────────────
        self.stdout.write("  ── Colonnes énergie détectées (act_*energy* / acm_*energy*) ──")
        try:
            cursor.execute(f"SELECT TOP 0 * FROM {TABLE}")
            all_cols = [d[0] for d in cursor.description]
            energy_cols = [c for c in all_cols if "energy" in c.lower()]
            for c in energy_cols:
                self.stdout.write(f"    • {c}")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  ✗ {e}"))
            conn.close(); sys.exit(1)

        # ── 2. Sites présents (TOP 10) ────────────────────────────────────────
        self.stdout.write("\n  ── Sites présents dans ACM (TOP 10 par nb jours) ─────────")
        try:
            cursor.execute(f"""
                SELECT TOP 10
                    site_id,
                    COUNT([Date])  AS nb_jours,
                    MIN([Date])    AS date_min,
                    MAX([Date])    AS date_max,
                    AVG(act_energy_p)  AS avg_energy_p,
                    AVG(act2_energy_p) AS avg_energy_p2
                FROM {TABLE}
                WHERE act_energy_p IS NOT NULL OR act2_energy_p IS NOT NULL
                GROUP BY site_id
                ORDER BY nb_jours DESC
            """)
            rows = cursor.fetchall()
            if rows:
                self.stdout.write(
                    f"    {'site_id':<18} {'nb_j':>6} {'date_min':<12} "
                    f"{'date_max':<12} {'avg_ep(kWh)':>12} {'avg_ep2(kWh)':>13}"
                )
                self.stdout.write("    " + "-" * 77)
                for r in rows:
                    ep  = f"{float(r[4]):.2f}" if r[4] is not None else "NULL"
                    ep2 = f"{float(r[5]):.2f}" if r[5] is not None else "NULL"
                    self.stdout.write(
                        f"    {str(r[0]):<18} {str(r[1]):>6} {str(r[2]):<12} "
                        f"{str(r[3]):<12} {ep:>12} {ep2:>13}"
                    )
            else:
                self.stdout.write(self.style.WARNING(
                    "    Aucun site avec act_energy_p non-NULL."
                ))
                # Tenter sans filtre
                cursor.execute(f"""
                    SELECT TOP 10 site_id, COUNT([Date]) AS nb_jours,
                           MIN([Date]), MAX([Date])
                    FROM {TABLE}
                    GROUP BY site_id ORDER BY nb_jours DESC
                """)
                rows2 = cursor.fetchall()
                self.stdout.write("    Tous sites (sans filtre NULL) :")
                for r in rows2:
                    self.stdout.write(f"      {r[0]}  {r[1]} jours  {r[2]} → {r[3]}")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  ✗ TOP sites : {e}"))
            rows = []

        # ── 3. Choix du site ──────────────────────────────────────────────────
        if not site_id:
            if rows:
                site_id = rows[0][0]
                self.stdout.write(
                    self.style.WARNING(f"\n  → Aucun --site-id, utilisation de : {site_id}")
                )
            else:
                conn.close(); sys.exit(0)

        self.stdout.write(
            f"\n  ── Analyse [{site_id}]  {date_debut} → {date_fin} "
            f"({nb_jours_periode} jours) ──────"
        )

        # ── 4. Couverture brute ────────────────────────────────────────────────
        try:
            cursor.execute(f"""
                SELECT
                    COUNT([Date])                           AS nb_lignes,
                    SUM(CASE WHEN act_energy_p  > 0 THEN 1 ELSE 0 END) AS nb_ep_ok,
                    SUM(CASE WHEN act2_energy_p > 0 THEN 1 ELSE 0 END) AS nb_ep2_ok,
                    SUM(CASE WHEN act_energy_p  IS NULL THEN 1 ELSE 0 END) AS nb_ep_null,
                    MIN(act_energy_p),  MAX(act_energy_p),
                    AVG(act_energy_p),  SUM(act_energy_p),
                    MIN(act2_energy_p), MAX(act2_energy_p),
                    AVG(act2_energy_p), SUM(act2_energy_p)
                FROM {TABLE}
                WHERE site_id = ? AND [Date] >= ? AND [Date] <= ?
            """, (site_id, date_debut, date_fin))
            r = cursor.fetchone()

            nb_lignes = int(r[0]) if r[0] else 0
            nb_ep_ok  = int(r[1]) if r[1] else 0
            nb_ep2_ok = int(r[2]) if r[2] else 0
            nb_ep_null= int(r[3]) if r[3] else 0

            col = self.style.SUCCESS if nb_ep_ok >= 20 else (
                self.style.WARNING if nb_ep_ok >= 3 else self.style.ERROR
            )

            self.stdout.write(f"\n    Lignes dans la période       : {nb_lignes} / {nb_jours_periode}")
            self.stdout.write(col(f"    act_energy_p  > 0           : {nb_ep_ok} jours"))
            self.stdout.write(f"    act_energy_p  IS NULL        : {nb_ep_null} jours")
            self.stdout.write(f"    act2_energy_p > 0           : {nb_ep2_ok} jours")

            if nb_ep_ok > 0:
                self.stdout.write(f"\n    act_energy_p  min/moy/max    : "
                                  f"{float(r[4]):.3f} / {float(r[6]):.3f} / {float(r[5]):.3f} kWh")
                self.stdout.write(f"    act_energy_p  SUM            : {float(r[7]):.2f} kWh")

            if nb_ep2_ok > 0:
                self.stdout.write(f"    act2_energy_p min/moy/max   : "
                                  f"{float(r[8]):.3f} / {float(r[10]):.3f} / {float(r[9]):.3f} kWh")
                self.stdout.write(f"    act2_energy_p SUM           : {float(r[11]):.2f} kWh")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"    ✗ Couverture : {e}"))
            nb_ep_ok = 0

        # ── 5. Échantillon 10 lignes (act_energy_p) ───────────────────────────
        if nb_ep_ok > 0 or nb_lignes > 0:
            self.stdout.write(f"\n    ── Échantillon 10 lignes (act_energy_p / act2_energy_p) ──")
            try:
                cursor.execute(f"""
                    SELECT TOP 10
                        [Date],
                        act_energy_p,
                        act2_energy_p,
                        act_active_power_avg,
                        act_power_factor
                    FROM {TABLE}
                    WHERE site_id = ? AND [Date] >= ? AND [Date] <= ?
                    ORDER BY [Date]
                """, (site_id, date_debut, date_fin))
                sample = cursor.fetchall()
                self.stdout.write(
                    f"    {'Date':<12} {'act_energy_p':>14} {'act2_energy_p':>14} "
                    f"{'act_pwr_avg':>12} {'pwr_factor':>11}"
                )
                self.stdout.write("    " + "-" * 67)
                for row in sample:
                    def fmt(v):
                        return f"{float(v):.3f}" if v is not None else "NULL"
                    self.stdout.write(
                        f"    {str(row[0]):<12} {fmt(row[1]):>14} {fmt(row[2]):>14} "
                        f"{fmt(row[3]):>12} {fmt(row[4]):>11}"
                    )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"    ✗ Échantillon : {e}"))

        # ── 6. Simulation get_conso_acm (logique MAX-MIN delta) ──────────────
        self.stdout.write(f"\n    ── Simulation logique ACM : MAX(idx) - MIN(idx) ────────────")
        conso_sim = 0.0
        try:
            # act_energy_p est un INDEX CUMULATIF → conso = MAX - MIN
            cursor.execute(f"""
                SELECT
                    MAX(act_energy_p)  AS idx_max,
                    MIN(act_energy_p)  AS idx_min,
                    COUNT([Date])      AS nb_pts,
                    DATEDIFF(day, MIN([Date]), MAX([Date])) AS span_days
                FROM {TABLE}
                WHERE site_id = ?
                  AND [Date] >= ? AND [Date] <= ?
                  AND act_energy_p IS NOT NULL AND act_energy_p > 0
            """, (site_id, date_debut, date_fin))
            r2 = cursor.fetchone()
            nb_pts    = int(r2[2]) if r2 and r2[2] else 0
            conso_sim = float(r2[0] - r2[1]) if r2 and r2[0] and r2[1] else 0.0
            span      = int(r2[3]) if r2 and r2[3] else 0

            if nb_pts >= 20 and conso_sim > 0:
                moy_j = conso_sim / max(1, span)
                self.stdout.write(self.style.SUCCESS(
                    f"    Mode EXACT  : MAX-MIN = {conso_sim:.2f} kWh "
                    f"sur {nb_pts} pts ({span} jours span)"
                ))
                self.stdout.write(self.style.SUCCESS(
                    f"    Moy/jour    : {moy_j:.2f} kWh/j"
                ))
            elif nb_pts >= 3 and conso_sim > 0:
                intervals = max(1, nb_pts - 1)
                moy_j     = conso_sim / intervals
                extrapol  = moy_j * nb_jours_periode
                self.stdout.write(self.style.WARNING(
                    f"    Mode EXTRAP : {nb_pts} pts, delta={conso_sim:.2f} kWh, "
                    f"moy={moy_j:.2f} kWh/j → extrapol={extrapol:.2f} kWh/{nb_jours_periode}j"
                ))
            else:
                # Fenêtre élargie ±90j
                f_debut = date_debut - timedelta(days=90)
                f_fin   = date_fin   + timedelta(days=90)
                cursor.execute(f"""
                    SELECT
                        MAX(act_energy_p) - MIN(act_energy_p) AS delta,
                        COUNT([Date])                          AS nb_pts,
                        DATEDIFF(day, MIN([Date]), MAX([Date])) AS span_days
                    FROM {TABLE}
                    WHERE site_id = ?
                      AND [Date] >= ? AND [Date] <= ?
                      AND act_energy_p IS NOT NULL AND act_energy_p > 0
                """, (site_id, f_debut, f_fin))
                r3 = cursor.fetchone()
                nb3 = int(r3[1]) if r3 and r3[1] else 0
                if nb3 >= 3 and r3[0] and float(r3[0]) > 0:
                    moy3     = float(r3[0]) / max(1, int(r3[2]))
                    extrapol = moy3 * nb_jours_periode
                    self.stdout.write(self.style.WARNING(
                        f"    Mode ±90j   : {nb3} pts, moy={moy3:.2f} kWh/j "
                        f"→ extrapol={extrapol:.2f} kWh"
                    ))
                else:
                    self.stdout.write(self.style.ERROR(
                        f"    ACM indisponible : {nb_pts} pts période, {nb3} pts ±90j"
                    ))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"    ✗ Simulation : {e}"))

        # ── 7. Comparaison avec Grid ──────────────────────────────────────────
        self.stdout.write(f"\n    ── Comparaison ACM vs Grid sur la même période ────────────")
        try:
            col_grid = efms._resolve_col_conso(conn)
            cursor.execute(f"""
                SELECT SUM([{col_grid}]), COUNT([Date])
                FROM {efms.TABLE_GRID}
                WHERE site_id = ? AND [Date] >= ? AND [Date] <= ?
                  AND [{col_grid}] IS NOT NULL AND [{col_grid}] > 0.1
            """, (site_id, date_debut, date_fin))
            rg = cursor.fetchone()
            if rg and rg[0]:
                conso_grid = float(rg[0])
                self.stdout.write(
                    f"    Grid [{col_grid}] : {conso_grid:.2f} kWh  ({rg[1]} jours)"
                )
                if conso_sim > 0:
                    ratio = conso_grid / conso_sim
                    color = self.style.SUCCESS if 0.85 <= ratio <= 1.15 else self.style.WARNING
                    self.stdout.write(color(
                        f"    Ratio Grid/ACM : {ratio:.3f}  "
                        f"({'cohérent ✓' if 0.85 <= ratio <= 1.15 else 'écart > 15%'})"
                    ))
            else:
                self.stdout.write(self.style.WARNING("    Grid : aucune donnée sur cette période"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"    Grid : {e}"))

        conn.close()
        self.stdout.write("\n" + "═" * 65 + "\n")
        self.stdout.write(self.style.SUCCESS("  Diagnostic ACM v2 terminé.\n"))
        sys.exit(0)