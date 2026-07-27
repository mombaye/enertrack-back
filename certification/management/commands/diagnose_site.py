# certification/management/commands/diagnose_site.py
"""
Diagnostic complet d'un site eFMS — pour comprendre pourquoi un site sort
des conso aberrantes (ex : KLD_2007 où ACM_période = 4553,98 sur Jan ET Fév
alors que la fenêtre SQL diffère).

Pour un site donné, affiche :
  1. Liste journalière des points ACM (act_energy_p) sur N mois
  2. Détection des ruptures d'index (rollover / reset compteur)
  3. Détection des outliers par méthode IQR (jour > Q3 + 3·IQR)
  4. Calcul comparatif de la conso mois par mois selon 3 méthodes :
     - MAX-MIN brut (méthode actuelle de _delta_to_conso)
     - SUM des deltas journaliers positifs (méthode robuste)
     - SUM(act_active_power_avg × 24 / 1000) si dispo (contrôle indépendant)
  5. Mêmes vérifs côté Grid (silver.gfms_Grid_Report_day)
  6. Affiche les CertificationResult existants pour le site
  7. Verdict : quelle méthode est cohérente, quel jour pose problème

Usage :
  python manage.py diagnose_site --site-id KLD_2007
  python manage.py diagnose_site --site-id KLD_2007 --months 6
  python manage.py diagnose_site --site-id KLD_2007 --raw     # liste tous les points
"""

from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date

from certification.models import CertificationResult
from certification.services.efms import EfmsService


TABLE_ACM  = "[SQL1-ProdDB].[dbo].[silver.gfms_AC_Meter_Report_day]"
TABLE_GRID = "[SQL1-ProdDB].[dbo].[silver.gfms_Grid_Report_day]"


class Command(BaseCommand):
    help = "Diagnostic eFMS d'un site — détecte outliers, rollovers, " \
           "compare MAX-MIN vs deltas journaliers."

    def add_arguments(self, parser):
        parser.add_argument("--site-id", default="KLD_2007",
                            help="Site eFMS à diagnostiquer (défaut: KLD_2007)")
        parser.add_argument("--months", type=int, default=5,
                            help="Profondeur historique en mois (défaut: 5)")
        parser.add_argument("--raw", action="store_true",
                            help="Liste TOUS les points journaliers (sinon : sample)")
        parser.add_argument("--from-date", default=None,
                            help="Date de début explicite (YYYY-MM-DD), prioritaire sur --months")

    # ───────────────────────────────────────────────────────────────────────
    # Helpers d'affichage
    # ───────────────────────────────────────────────────────────────────────

    def _hr(self, char="─", width=78):
        self.stdout.write(char * width)

    def _section(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO(f"━━ {title} " + "━" * (74 - len(title))))

    def _ok(self, msg):
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def _warn(self, msg):
        self.stdout.write(self.style.WARNING(f"  ⚠ {msg}"))

    def _err(self, msg):
        self.stdout.write(self.style.ERROR(f"  ✗ {msg}"))

    def _info(self, msg):
        self.stdout.write(f"  · {msg}")

    # ───────────────────────────────────────────────────────────────────────
    # Détection rollovers (act_energy_p doit être strictement croissant)
    # ───────────────────────────────────────────────────────────────────────

    def _find_rollovers(self, points):
        """
        points : liste de tuples (date, act_energy_p)  triés par date ASC
        Retourne la liste des ruptures (i, prev_val, curr_val, drop_pct)
        """
        ruptures = []
        for i in range(1, len(points)):
            d_prev, v_prev = points[i - 1]
            d_curr, v_curr = points[i]
            if v_prev is None or v_curr is None:
                continue
            if float(v_curr) < float(v_prev):
                drop = float(v_prev) - float(v_curr)
                drop_pct = drop / float(v_prev) * 100 if float(v_prev) > 0 else 0
                ruptures.append((d_prev, d_curr, float(v_prev), float(v_curr), drop_pct))
        return ruptures

    # ───────────────────────────────────────────────────────────────────────
    # Détection outliers par delta journalier (IQR)
    # ───────────────────────────────────────────────────────────────────────

    def _find_outlier_deltas(self, points):
        """
        Calcule les deltas journaliers positifs entre points consécutifs,
        flag les jours où le delta est > Q3 + 3·IQR (IQR robuste).
        Retourne (deltas_journaliers, outliers, median_delta).
        """
        deltas = []
        for i in range(1, len(points)):
            d_prev, v_prev = points[i - 1]
            d_curr, v_curr = points[i]
            if v_prev is None or v_curr is None:
                continue
            d = float(v_curr) - float(v_prev)
            if d > 0:                               # on ignore les rollovers ici
                deltas.append((d_curr, d))

        if len(deltas) < 5:
            return deltas, [], None

        vals = sorted(d for _, d in deltas)
        n    = len(vals)
        q1   = vals[n // 4]
        q3   = vals[3 * n // 4]
        iqr  = q3 - q1
        threshold_high = q3 + 3 * iqr
        med  = median(vals)

        outliers = [(d, v) for (d, v) in deltas if v > threshold_high]
        return deltas, outliers, med

    # ───────────────────────────────────────────────────────────────────────
    # Calcul conso mensuelle selon 3 méthodes
    # ───────────────────────────────────────────────────────────────────────

    def _compute_monthly_conso(self, points, deltas):
        """
        points : [(date, act_energy_p)]   tous les points triés ASC
        deltas : [(date, delta_journalier_positif)]  filtré sur > 0
        Retourne {(year, month): {"max_min": float, "robuste": float, "nb_pts": int}}
        """
        by_month = {}
        for d, v in points:
            key = (d.year, d.month)
            by_month.setdefault(key, []).append((d, float(v) if v else None))
        for key, pts in by_month.items():
            valid = [v for (_, v) in pts if v is not None]
            by_month[key] = {
                "nb_pts":  len(valid),
                "max":     max(valid) if valid else None,
                "min":     min(valid) if valid else None,
                "max_min": (max(valid) - min(valid)) if len(valid) >= 2 else None,
                "robuste": 0.0,
            }
        # méthode robuste : SUM des deltas journaliers positifs par mois
        for d, delta in deltas:
            key = (d.year, d.month)
            if key in by_month:
                by_month[key]["robuste"] += delta
        return by_month

    # ───────────────────────────────────────────────────────────────────────
    # MAIN
    # ───────────────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        site_id = options["site_id"]
        months  = options["months"]
        raw     = options["raw"]

        if options["from_date"]:
            date_from = parse_date(options["from_date"])
        else:
            today = date.today()
            date_from = date(today.year, today.month, 1) - timedelta(days=months * 31)
        date_to = date.today()

        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("═" * 78))
        self.stdout.write(self.style.HTTP_INFO(
            f"  DIAGNOSTIC eFMS — site={site_id}   {date_from} → {date_to}  ({months} mois)"
        ))
        self.stdout.write(self.style.HTTP_INFO("═" * 78))

        efms = EfmsService()
        conn = None
        try:
            conn   = efms._open_connection()
            cursor = conn.cursor()

            # ═══════════════════════════════════════════════════════════════
            # ACM — Récupération brute
            # ═══════════════════════════════════════════════════════════════
            self._section("1. ACM — données brutes (act_energy_p)")

            cursor.execute(f"""
                SELECT [Date], act_energy_p, act_active_power_avg
                FROM {TABLE_ACM}
                WHERE site_id = ?
                  AND [Date] >= ? AND [Date] <= ?
                ORDER BY [Date] ASC
            """, (site_id, date_from, date_to))
            acm_rows = cursor.fetchall()

            acm_points = [(r[0], r[1]) for r in acm_rows if r[1] is not None]
            acm_power  = [(r[0], r[2]) for r in acm_rows if r[2] is not None]

            self._info(f"Lignes totales en BDD       : {len(acm_rows)}")
            self._info(f"Avec act_energy_p non null  : {len(acm_points)}")
            self._info(f"Avec act_active_power_avg   : {len(acm_power)}")

            if not acm_points:
                self._err("Aucune donnée ACM — site introuvable ou inactif sur la période")
            else:
                first_d, first_v = acm_points[0]
                last_d,  last_v  = acm_points[-1]
                self._info(f"Premier point : {first_d}  →  act_energy_p = {float(first_v):>12.1f}")
                self._info(f"Dernier point : {last_d}  →  act_energy_p = {float(last_v):>12.1f}")
                span = (last_d - first_d).days
                if span > 0:
                    delta_total = float(last_v) - float(first_v)
                    self._info(f"Delta total   : {delta_total:>+12.1f} kWh sur {span} jours "
                               f"(moyenne {delta_total / span:.1f} kWh/jour)")

            # Liste des points (sample ou raw)
            if acm_points:
                self.stdout.write("")
                if raw or len(acm_points) <= 40:
                    sample = acm_points
                else:
                    self._info(f"Sample : 15 premiers + 15 derniers (utiliser --raw pour tout voir)")
                    sample = acm_points[:15] + [(None, "...")] + acm_points[-15:]
                self.stdout.write(f"  {'Date':<12}  {'act_energy_p':>14}  {'Δ vs J-1':>12}")
                self.stdout.write("  " + "-" * 46)
                prev = None
                for d, v in sample:
                    if d is None:
                        self.stdout.write(f"  {'...':<12}")
                        prev = None
                        continue
                    delta_str = ""
                    if prev is not None and v is not None:
                        d_val = float(v) - float(prev)
                        delta_str = f"{d_val:>+12.1f}"
                        if d_val < 0:
                            delta_str = self.style.ERROR(delta_str)
                    self.stdout.write(f"  {str(d):<12}  {float(v):>14.1f}  {delta_str}")
                    prev = v

            # ═══════════════════════════════════════════════════════════════
            # ACM — Détection rollovers
            # ═══════════════════════════════════════════════════════════════
            self._section("2. ACM — détection ruptures d'index (rollover/reset)")

            ruptures = self._find_rollovers(acm_points)
            if not ruptures:
                self._ok("Aucune rupture détectée — index strictement croissant")
            else:
                self._err(f"{len(ruptures)} rupture(s) détectée(s) : ")
                self.stdout.write(f"  {'De':<12}  {'À':<12}  {'avant':>12}  {'après':>12}  {'chute':>8}")
                self.stdout.write("  " + "-" * 62)
                for d_prev, d_curr, v_prev, v_curr, drop_pct in ruptures:
                    self.stdout.write(self.style.ERROR(
                        f"  {str(d_prev):<12}  {str(d_curr):<12}  {v_prev:>12.1f}  {v_curr:>12.1f}  {drop_pct:>7.1f}%"
                    ))
                self._warn("→ Une rupture fausse MAX(act_energy_p) - MIN(act_energy_p) sur tout "
                           "intervalle qui l'enjambe.")

            # ═══════════════════════════════════════════════════════════════
            # ACM — Détection outliers
            # ═══════════════════════════════════════════════════════════════
            self._section("3. ACM — détection outliers sur deltas journaliers (IQR)")

            deltas, outliers, med = self._find_outlier_deltas(acm_points)
            self._info(f"Deltas journaliers positifs : {len(deltas)}")
            if med is not None:
                self._info(f"Médiane delta journalier    : {med:>10.2f} kWh/jour")

            if not outliers:
                self._ok("Aucun outlier de consommation journalière")
            else:
                self._err(f"{len(outliers)} outlier(s) (delta > Q3 + 3·IQR) : ")
                self.stdout.write(f"  {'Date':<12}  {'Δ journalier':>14}  {'× médiane':>12}")
                self.stdout.write("  " + "-" * 44)
                for d, v in outliers:
                    factor = v / med if med and med > 0 else 0
                    self.stdout.write(self.style.ERROR(
                        f"  {str(d):<12}  {v:>14.1f}  {factor:>11.1f}×"
                    ))
                self._warn("→ Un outlier sur act_energy_p inflate MAX-MIN si la fenêtre l'inclut.")

            # ═══════════════════════════════════════════════════════════════
            # ACM — Conso mensuelle 3 méthodes
            # ═══════════════════════════════════════════════════════════════
            self._section("4. ACM — conso mensuelle : MAX-MIN vs robuste")

            monthly = self._compute_monthly_conso(acm_points, deltas)
            power_monthly = {}
            for d, p in acm_power:
                key = (d.year, d.month)
                power_monthly[key] = power_monthly.get(key, 0.0) + float(p) * 24 / 1000

            self.stdout.write(
                f"  {'Mois':<10}  {'Pts':>4}  {'MAX-MIN':>11}  {'Σ deltas+':>11}  "
                f"{'Σ pwr×24h':>11}  {'écart M-M / Σ':>14}"
            )
            self.stdout.write("  " + "-" * 70)

            for key in sorted(monthly.keys()):
                y, m  = key
                d     = monthly[key]
                pw    = power_monthly.get(key)
                mm    = d["max_min"]
                ro    = d["robuste"]
                ratio = (mm / ro) if (mm and ro and ro > 0) else None
                ratio_str = f"×{ratio:.2f}" if ratio else "—"
                line = (
                    f"  {y}-{m:02d}      {d['nb_pts']:>4}  "
                    f"{mm if mm else 0:>10.1f}   "
                    f"{ro:>10.1f}   "
                    f"{(pw if pw else 0):>10.1f}   "
                    f"{ratio_str:>14}"
                )
                if ratio and ratio > 2:
                    self.stdout.write(self.style.ERROR(line))
                elif ratio and ratio > 1.3:
                    self.stdout.write(self.style.WARNING(line))
                else:
                    self.stdout.write(line)

            self.stdout.write("")
            self._info("Lecture : si MAX-MIN ≫ Σ deltas+, l'index a un saut artificiel ce mois-là.")
            self._info("La méthode 'Σ deltas+' rejette automatiquement les sauts négatifs (rollovers).")

            # ═══════════════════════════════════════════════════════════════
            # GRID — Récupération brute (compact)
            # ═══════════════════════════════════════════════════════════════
            self._section("5. GRID — silver.gfms_Grid_Report_day")

            try:
                col = efms._resolve_col_conso(conn)
                self._info(f"Colonne conso résolue : [{col}]")
            except Exception as e:
                self._err(f"Résolution colonne conso échouée : {e}")
                col = None

            if col:
                cursor.execute(f"""
                    SELECT [Date], TRY_CAST([{col}] AS FLOAT)
                    FROM {TABLE_GRID}
                    WHERE site_id = ?
                      AND [Date] >= ? AND [Date] <= ?
                      AND [{col}] IS NOT NULL
                    ORDER BY [Date] ASC
                """, (site_id, date_from, date_to))
                grid_rows = [(r[0], r[1]) for r in cursor.fetchall() if r[1] is not None]

                self._info(f"Lignes Grid avec conso non null : {len(grid_rows)}")

                if grid_rows:
                    grid_monthly = {}
                    for d, v in grid_rows:
                        key = (d.year, d.month)
                        grid_monthly.setdefault(key, []).append(float(v))

                    self.stdout.write("")
                    self.stdout.write(f"  {'Mois':<10}  {'Pts':>4}  {'Σ Grid':>11}  {'moy/jour':>10}")
                    self.stdout.write("  " + "-" * 42)
                    for key in sorted(grid_monthly.keys()):
                        y, m = key
                        vals = grid_monthly[key]
                        s = sum(vals)
                        avg = s / len(vals) if vals else 0
                        self.stdout.write(
                            f"  {y}-{m:02d}      {len(vals):>4}  {s:>10.1f}   {avg:>9.2f}"
                        )
                else:
                    self._warn("Aucune donnée Grid sur la période — fallback ACM systématique pour ce site")

            # ═══════════════════════════════════════════════════════════════
            # CertificationResults existants
            # ═══════════════════════════════════════════════════════════════
            self._section("6. Résultats de certification déjà calculés pour ce site")

            cert_results = (
                CertificationResult.objects
                .filter(site__site_id=site_id)
                .select_related("invoice", "cert_batch")
                .order_by("-invoice__date_debut_periode")[:10]
            )

            if not cert_results:
                self._info("Aucun CertificationResult pour ce site")
            else:
                self.stdout.write(
                    f"  {'Facture':<14} {'Période':<24} {'Conso fact':>10} "
                    f"{'ACM_p':>10} {'r_p':>6} {'r_30j':>6} {'Statut':<22} {'Alerte':>7}"
                )
                self.stdout.write("  " + "-" * 105)
                for r in cert_results:
                    inv = r.invoice
                    period = ""
                    if inv.date_debut_periode and inv.date_fin_periode:
                        period = f"{inv.date_debut_periode}→{inv.date_fin_periode}"
                    line = (
                        f"  {inv.numero_facture:<14} "
                        f"{period:<24} "
                        f"{float(r.conso_facturee_periode or 0):>10.1f} "
                        f"{float(r.estim_conso_acm_periode or 0):>10.1f} "
                        f"{float(r.ratio_acm_periode or 0):>6.2f} "
                        f"{float(r.ratio_acm_30j or 0):>6.2f} "
                        f"{r.status:<22} "
                        f"{'OUI' if r.flag_mesure_alert else 'non':>7}"
                    )
                    if r.flag_mesure_alert:
                        self.stdout.write(self.style.WARNING(line))
                    else:
                        self.stdout.write(line)

            # ═══════════════════════════════════════════════════════════════
            # Verdict
            # ═══════════════════════════════════════════════════════════════
            self._section("7. Verdict")

            issues = []
            if ruptures:
                issues.append(f"{len(ruptures)} rupture(s) d'index ACM (rollover ou reset compteur)")
            if outliers:
                issues.append(f"{len(outliers)} outlier(s) journalier(s) ACM (×médiane élevé)")

            # Détection : la même valeur MAX-MIN sur plusieurs mois ?
            mm_vals = [d["max_min"] for d in monthly.values() if d["max_min"]]
            if len(mm_vals) >= 2:
                mm_set = {round(v, 1) for v in mm_vals}
                if len(mm_set) < len(mm_vals) - 1:
                    issues.append(
                        "Plusieurs mois renvoient EXACTEMENT le même MAX-MIN — "
                        "signe que la même paire de points borne plusieurs fenêtres."
                    )

            if not issues:
                self._ok("Aucune anomalie détectée — données ACM saines.")
            else:
                self._err("Anomalies trouvées :")
                for i in issues:
                    self.stdout.write(self.style.ERROR(f"     · {i}"))

                self.stdout.write("")
                self._info("Recommandations :")
                self.stdout.write("     1. Remplacer MAX-MIN par Σ(deltas journaliers positifs)")
                self.stdout.write("        dans _delta_to_conso (méthode robuste aux outliers + rollovers).")
                self.stdout.write("     2. Plafonner les deltas journaliers à p99 × 5 pour rejeter")
                self.stdout.write("        les pics d'index manifestement aberrants.")
                self.stdout.write("     3. Loguer un warning quand un mois certifié a une rupture.")

        except Exception as e:
            self._err(f"Erreur inattendue : {e}")
            import traceback
            traceback.print_exc()
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("═" * 78))
        self.stdout.write("")