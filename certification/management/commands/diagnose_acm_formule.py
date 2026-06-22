# certification/management/commands/diagnose_acm_formule.py
"""
Comparaison CDC (U×I) vs act_energy_p sur un site donné.

Usage :
  python manage.py diagnose_acm_formule
  python manage.py diagnose_acm_formule --site-id DKR_0698 --date-debut 2025-10-01 --date-fin 2025-10-31
"""

from datetime import date, timedelta
from django.core.management.base import BaseCommand
from certification.services.efms import EfmsService


TABLE = "[SQL1-ProdDB].[dbo].[silver.gfms_AC_Meter_Report_day]"


class Command(BaseCommand):
    help = "Compare formule CDC (U×I) vs act_energy_p (index cumulatif)"

    def add_arguments(self, parser):
        parser.add_argument("--site-id",    default="DKR_0698")
        parser.add_argument("--date-debut", default="2025-10-01")
        parser.add_argument("--date-fin",   default="2025-10-31")

    def handle(self, *args, **options):
        site_id    = options["site_id"]
        date_debut = date.fromisoformat(options["date_debut"])
        date_fin   = date.fromisoformat(options["date_fin"])
        nb_jours   = (date_fin - date_debut).days + 1

        efms = EfmsService()
        self.stdout.write("\n" + "═" * 65)
        self.stdout.write(f"  COMPARAISON FORMULE  —  {site_id}  {date_debut} → {date_fin}")
        self.stdout.write("═" * 65)

        conn = None
        try:
            conn   = efms._open_connection()
            cursor = conn.cursor()

            # ── 1. Colonnes disponibles ───────────────────────────────────────
            cursor.execute(f"SELECT TOP 0 * FROM {TABLE}")
            cols = [d[0] for d in cursor.description]
            voltage_cols = [c for c in cols if "voltage" in c.lower() and "act" in c.lower()]
            current_cols = [c for c in cols if "current" in c.lower() and "act" in c.lower()]
            self.stdout.write(f"\n  Colonnes tension  : {voltage_cols}")
            self.stdout.write(f"  Colonnes courant  : {current_cols}")

            # ── 2. Échantillon brut sur 10 lignes ─────────────────────────────
            self.stdout.write(f"\n  ── Échantillon 10 lignes ──────────────────────────────────")
            header = f"  {'Date':<12} {'act_ep':>10} {'U1avg':>8} {'U2avg':>8} {'U3avg':>8} {'I1avg':>8} {'I2avg':>8} {'I3avg':>8} {'UxI_kWh':>10} {'pwr_fac':>8}"
            self.stdout.write(header)
            self.stdout.write("  " + "-" * 90)

            cursor.execute(f"""
                SELECT TOP 10
                    [Date],
                    act_energy_p,
                    act_voltage1_avg, act_voltage2_avg, act_voltage3_avg,
                    act_current1_avg, act_current2_avg, act_current3_avg,
                    act_active_power_avg,
                    act_power_factor
                FROM {TABLE}
                WHERE site_id = ?
                  AND [Date] >= ? AND [Date] <= ?
                ORDER BY [Date]
            """, (site_id, date_debut, date_fin))

            rows = cursor.fetchall()
            for r in rows:
                d, ep, u1, u2, u3, i1, i2, i3, pwr, pf = r
                uxi = None
                if all(v is not None for v in [u1, u2, u3, i1, i2, i3]):
                    pavg_va = float(u1)*float(i1) + float(u2)*float(i2) + float(u3)*float(i3)
                    uxi = pavg_va * 24 / 1000
                ep_str  = f"{float(ep):.1f}"  if ep  is not None else "NULL"
                uxi_str = f"{uxi:.1f}"        if uxi is not None else "NULL"
                self.stdout.write(
                    f"  {str(d):<12} "
                    f"{ep_str:>10} "
                    f"{float(u1) if u1 else 0:>8.2f} "
                    f"{float(u2) if u2 else 0:>8.2f} "
                    f"{float(u3) if u3 else 0:>8.2f} "
                    f"{float(i1) if i1 else 0:>8.3f} "
                    f"{float(i2) if i2 else 0:>8.3f} "
                    f"{float(i3) if i3 else 0:>8.3f} "
                    f"{uxi_str:>10} "
                    f"{float(pf) if pf else 0:>8.2f}"
                )

            # ── 3. Statistiques act_power_factor ─────────────────────────────
            self.stdout.write(f"\n  ── act_power_factor (clé pour comprendre l'unité) ──────────")
            cursor.execute(f"""
                SELECT
                    MIN(act_power_factor)  AS pf_min,
                    MAX(act_power_factor)  AS pf_max,
                    AVG(act_power_factor)  AS pf_avg,
                    COUNT(*)               AS nb_pts
                FROM {TABLE}
                WHERE site_id = ?
                  AND [Date] >= ? AND [Date] <= ?
                  AND act_power_factor IS NOT NULL
            """, (site_id, date_debut, date_fin))
            pf = cursor.fetchone()
            if pf and pf[0]:
                self.stdout.write(f"  min={float(pf[0]):.3f}  max={float(pf[1]):.3f}  avg={float(pf[2]):.3f}  sur {pf[3]} jours")
                if float(pf[2]) > 1:
                    self.stdout.write(self.style.WARNING(
                        f"  ⚠ Moyenne > 1 ({float(pf[2]):.1f}) — power_factor probablement en % ou x100"
                    ))
                else:
                    self.stdout.write(self.style.SUCCESS("  ✓ Power factor en [0–1] — utilisable directement"))

            # ── 4. Calcul agrégé sur la période ──────────────────────────────
            self.stdout.write(f"\n  ── Calcul agrégé sur {nb_jours} jours ──────────────────────────")

            # 4a. act_energy_p (MAX - MIN, index cumulatif)
            cursor.execute(f"""
                SELECT
                    MAX(act_energy_p) - MIN(act_energy_p) AS delta_kwh,
                    COUNT([Date])                          AS nb_pts,
                    DATEDIFF(day, MIN([Date]), MAX([Date])) AS span
                FROM {TABLE}
                WHERE site_id = ?
                  AND [Date] >= ? AND [Date] <= ?
                  AND act_energy_p IS NOT NULL AND act_energy_p > 0
            """, (site_id, date_debut, date_fin))
            r_ep = cursor.fetchone()
            conso_ep = float(r_ep[0]) if r_ep and r_ep[0] else None

            # 4b. Formule CDC brute : SUM(U1I1 + U2I2 + U3I3) * 24 / 1000
            cursor.execute(f"""
                SELECT
                    SUM(
                        (act_voltage1_avg * act_current1_avg +
                         act_voltage2_avg * act_current2_avg +
                         act_voltage3_avg * act_current3_avg)
                        * 24.0 / 1000.0
                    )              AS conso_uxi_brut,
                    COUNT([Date]) AS nb_pts
                FROM {TABLE}
                WHERE site_id = ?
                  AND [Date] >= ? AND [Date] <= ?
                  AND act_voltage1_avg IS NOT NULL
                  AND act_current1_avg IS NOT NULL
            """, (site_id, date_debut, date_fin))
            r_uxi = cursor.fetchone()
            conso_uxi_brut = float(r_uxi[0]) if r_uxi and r_uxi[0] else None

            # 4c. Formule CDC corrigée par power factor
            #     Si pf > 1, supposer qu'il est en % (diviser par 100)
            pf_avg_val = float(pf[2]) if pf and pf[0] else None
            pf_divisor = 100.0 if pf_avg_val and pf_avg_val > 1 else 1.0

            cursor.execute(f"""
                SELECT
                    SUM(
                        (act_voltage1_avg * act_current1_avg +
                         act_voltage2_avg * act_current2_avg +
                         act_voltage3_avg * act_current3_avg)
                        * (act_power_factor / ?)
                        * 24.0 / 1000.0
                    )              AS conso_uxi_pf,
                    COUNT([Date]) AS nb_pts
                FROM {TABLE}
                WHERE site_id = ?
                  AND [Date] >= ? AND [Date] <= ?
                  AND act_voltage1_avg IS NOT NULL
                  AND act_current1_avg IS NOT NULL
                  AND act_power_factor IS NOT NULL AND act_power_factor > 0
            """, (pf_divisor, site_id, date_debut, date_fin))
            r_uxi_pf = cursor.fetchone()
            conso_uxi_pf = float(r_uxi_pf[0]) if r_uxi_pf and r_uxi_pf[0] else None

            # 4d. act_active_power_avg (puissance active déjà calculée par le compteur)
            cursor.execute(f"""
                SELECT
                    SUM(act_active_power_avg) * 24.0 / 1000.0 AS conso_pwr,
                    COUNT([Date])                               AS nb_pts
                FROM {TABLE}
                WHERE site_id = ?
                  AND [Date] >= ? AND [Date] <= ?
                  AND act_active_power_avg IS NOT NULL AND act_active_power_avg > 0
            """, (site_id, date_debut, date_fin))
            r_pwr = cursor.fetchone()
            conso_pwr = float(r_pwr[0]) if r_pwr and r_pwr[0] else None

            # ── 5. Tableau comparatif ─────────────────────────────────────────
            self.stdout.write(f"\n  ── Résultats comparatifs ───────────────────────────────────")

            def _ratio(a, b):
                if a and b and b != 0:
                    return f"  (×{a/b:.3f} vs act_energy_p)"
                return ""

            self.stdout.write(f"\n  {'Méthode':<40} {'kWh':>12}  {'Ratio vs EP':>12}")
            self.stdout.write("  " + "-" * 68)

            if conso_ep:
                self.stdout.write(self.style.SUCCESS(
                    f"  {'act_energy_p  (MAX-MIN index)':<40} {conso_ep:>12.1f}  {'← référence':>12}"
                ))
            else:
                self.stdout.write(self.style.ERROR(f"  act_energy_p : INDISPONIBLE"))

            if conso_uxi_brut:
                ratio = conso_uxi_brut / conso_ep if conso_ep else 0
                col = self.style.SUCCESS if 0.85 <= ratio <= 1.15 else self.style.WARNING
                self.stdout.write(col(
                    f"  {'CDC brut : ∑(U×I)×24/1000':<40} {conso_uxi_brut:>12.1f}  {f'×{ratio:.3f}':>12}"
                ))

            if conso_uxi_pf:
                ratio = conso_uxi_pf / conso_ep if conso_ep else 0
                pf_note = f"(pf÷{pf_divisor:.0f})"
                col = self.style.SUCCESS if 0.85 <= ratio <= 1.15 else self.style.WARNING
                self.stdout.write(col(
                    f"  {f'CDC corrigé : ∑(U×I×pf) {pf_note}×24/1000':<40} {conso_uxi_pf:>12.1f}  {f'×{ratio:.3f}':>12}"
                ))

            if conso_pwr:
                ratio = conso_pwr / conso_ep if conso_ep else 0
                col = self.style.SUCCESS if 0.85 <= ratio <= 1.15 else self.style.WARNING
                self.stdout.write(col(
                    f"  {'act_active_power_avg×24/1000':<40} {conso_pwr:>12.1f}  {f'×{ratio:.3f}':>12}"
                ))

            # ── 6. Verdict ────────────────────────────────────────────────────
            self.stdout.write(f"\n  ── Verdict ─────────────────────────────────────────────────")
            if conso_ep and conso_uxi_brut:
                ratio_brut = conso_uxi_brut / conso_ep
                ratio_pf   = (conso_uxi_pf / conso_ep) if conso_uxi_pf else None
                ratio_pwr  = (conso_pwr    / conso_ep) if conso_pwr    else None

                best = min(
                    [(abs(r - 1), label) for r, label in [
                        (ratio_brut, "CDC brut U×I"),
                        (ratio_pf,   f"CDC corrigé U×I×pf÷{pf_divisor:.0f}"),
                        (ratio_pwr,  "act_active_power_avg"),
                        (1.0,        "act_energy_p (MAX-MIN)"),
                    ] if r is not None]
                )

                self.stdout.write(self.style.SUCCESS(
                    f"\n  ✓ Méthode la plus proche de act_energy_p : {best[1]}"
                ))
                if ratio_brut > 1.5:
                    self.stdout.write(self.style.ERROR(
                        f"  ✗ CDC brut surévalue de ×{ratio_brut:.2f} — NE PAS utiliser sans cos(φ)"
                    ))
                if ratio_pf and 0.90 <= ratio_pf <= 1.10:
                    self.stdout.write(self.style.SUCCESS(
                        "  ✓ CDC corrigé (U×I×pf) ≈ act_energy_p — formule CDC valide si pf fiable"
                    ))
                elif ratio_pwr and 0.90 <= ratio_pwr <= 1.10:
                    self.stdout.write(self.style.SUCCESS(
                        "  ✓ act_active_power_avg ≈ act_energy_p — alternative CDC fiable"
                    ))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\n  ✗ Erreur : {e}"))
            import traceback
            traceback.print_exc()
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

        self.stdout.write("\n" + "═" * 65 + "\n")
# patch: add √3 variant query after existing ones - already handled in script