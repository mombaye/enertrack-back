"""
Audit complet du calcul CPH d'un site sur une période (lecture seule) : rattachement
inventaire → faits journaliers, chaîne runtime, chaîne puissance jour par jour
(méthodes tentées / retenue / rejetées), load AC historique (valeurs Active Power Avg
réellement lues, unité, médianes) et raisons des jours non calculés.

    python manage.py diagnose_cph_site DBL_0068 --start 2026-09-01 --end 2026-09-30
    python manage.py diagnose_cph_site DBL_0068 --start 2026-09-01 --end 2026-09-30 --snowflake

--snowflake relit en LECTURE SEULE les points bruts AC_METER et GENSET_REPORT du site
(SELECT uniquement) pour prouver qu'un load AC à 0 kW vient de mesures réelles.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services import cph_engine as E
from fuel_tracking.services.cph_service import compute_period


def _v(x, places=3):
    if x is None:
        return "NULL"
    return f"{float(x):.{places}f}"


class Command(BaseCommand):
    help = "Audit CPH d'un site : jointures, runtime, méthodes de puissance, load AC, jours non calculés."

    def add_arguments(self, parser):
        parser.add_argument("site_id")
        parser.add_argument("--start", type=date.fromisoformat, required=True)
        parser.add_argument("--end", type=date.fromisoformat, required=True)
        parser.add_argument("--snowflake", action="store_true",
                            help="Relit les points bruts AC_METER / GENSET_REPORT du site (lecture seule).")

    def handle(self, *args, site_id, start, end, snowflake, **opts):
        from fuel_tracking.models import FuelSiteDailyFacts, FuelSiteInventory

        w = self.stdout.write
        rows = compute_period(start, end, site_ids=[site_id])
        if not rows:
            raise CommandError(f"{site_id} absent de l'inventaire des sites avec GE (SITE_ESCO_CURRENT.DG_COUNT > 0).")
        r = rows[0]

        w(self.style.MIGRATE_HEADING(f"== {site_id} — {r['site_name']} — {start} → {end} ({r['days']} j) — règle {E.RULE_VERSION}"))
        w(f"Configuration : {r['kind'] or 'INCONNUE'} (source : {r['kind_source'] or '—'}) · fichier facturation : "
          f"{r['configuration_fichier'] or '—'} · réseau : {r['grid_supply']} (off-grid={r['off_grid']}) · "
          f"type de site : {r['site_type'] or '—'} · facturé avec GE : {r['facture_avec_ge']}")
        m = r["correspondance"] or {}
        c = r["curve"]
        w(f"GE : {r['ge_label']} · {_v(r['ge_kva'], 1)} kVA · mapping {m.get('statut')} (score {m.get('score')}) · "
          f"courbe {c.curve_id if c else '—'} ({_v(c.prp_kva, 1) if c else '—'} kVA, a={_v(c.a, 6) if c else '—'}, "
          f"b={_v(c.b, 6) if c else '—'}, c={_v(c.c, 6) if c else '—'}) · origine {m.get('curve_source_status') or '—'}")

        # 1. Jointures : SITE_ID → (COUNTRY, DATA_ID) → faits journaliers.
        w(self.style.MIGRATE_HEADING("\n1. Jointures site → DATA_ID → faits journaliers"))
        inv = list(FuelSiteInventory.objects.filter(site_id=site_id).values("country", "data_id", "dg_count", "grid_supply"))
        w(f"SITE_ESCO_CURRENT : {inv}")
        if r["data_issue"]:
            w(self.style.ERROR(f"Rattachement ambigu : {r['data_issue']}"))
        lookback = start - timedelta(days=E.AC_REFERENCE_LOOKBACK_DAYS)
        facts = {f["date"]: f for f in FuelSiteDailyFacts.objects.filter(
            country=r["country"], data_id=r["data_id"], date__gte=lookback, date__lte=end).values()}
        period_days = list(E.daterange(start, end))
        missing = [d for d in period_days if d not in facts]
        w(f"Clé de jointure (country={r['country']}, data_id={r['data_id']}) · jour métier = DATE / REPORT_DATE Snowflake, "
          f"créneaux 5 min regroupés par CAST(TIMESTAMP AS DATE)")
        w(f"Faits sur la période : {len(period_days) - len(missing)}/{len(period_days)} jours · historique AC "
          f"({E.AC_REFERENCE_LOOKBACK_DAYS} j avant) : {sum(1 for d in facts if d < start)} jours")
        if missing:
            w(self.style.WARNING(f"Jours sans aucune ligne de faits : {', '.join(d.isoformat() for d in missing)}"))

        # 2. Runtime.
        w(self.style.MIGRATE_HEADING("\n2. Runtime (priorité DSE > redresseur > Day DG On > compteur, dispo ≥ 50 %)"))
        for s, ev in r["sources"].items():
            w(f"  {s:<18} dispo {ev['availability'] * 100:5.1f} % · {ev['days_valid']} j valides · "
              f"{'EXPLOITABLE' if ev['exploitable'] else 'rejetée'}{' · ' + ev['rejection'] if ev['rejection'] else ''}")
        w(f"  Retenue : {r['runtime_source_main']} · runtime total {_v(r['runtime_total_h'], 1)} h · "
          f"{r['running_days']} jour(s) GE en marche")

        # 3. Load AC historique (indoor).
        ac = r["ac_reference"]
        w(self.style.MIGRATE_HEADING("\n3. Load AC historique (indoor, jours réseau sans GE)"))
        if ac is None:
            w("  Non applicable (site non indoor).")
        else:
            w(f"  Statut : {ac['statut']} · load AC retenu : {_v(ac['p_ac_aux_kw'])} kW")
            w(f"  Jours de référence : {ac['jours_reference']} · mesures Active Power Avg : {ac['mesures'] if ac['mesures'] is not None else 'NULL (resynchro requise)'}")
            w(f"  Unité brute {ac['unite_brute']} ÷ {ac['diviseur']} → kW · médiane brute {_v(ac['mediane_ac_brut'], 1)} W "
              f"= {_v(ac['mediane_ac_kw'])} kW · médiane P_DC entrée {_v(ac['mediane_p_dc_entree_kw'])} kW · "
              f"médiane (P_AC − P_DC entrée) {_v(ac['mediane_ecart_brut_kw'])} kW · jours écart < 0 : {ac['jours_ecart_negatif']}")
            if ac["reason"]:
                w(f"  Motif : {ac['reason']}")
            for cand in ac["candidats"]:
                w(f"    {cand['date']} · AC brut {_v(cand['ac_brut'], 1)} W → {_v(cand['ac_kw'])} kW · "
                  f"P_DC entrée {_v(cand['p_dc_entree_kw'])} kW · écart {_v(cand['ac_kw'] - cand['p_dc_entree_kw'])} kW · "
                  f"points {cand['points'] if cand['points'] is not None else 'NULL'}")
            if ac["statut"] == E.AC_MESURE_ZERO:
                w(self.style.WARNING("  → 0 kW issu de mesures réelles : la médiane de (P_AC − P_DC entrée) est dans la "
                                     "tolérance de zéro. Vérifier avec --snowflake que les points AC ne sont pas nuls."))

        # 4. Jour par jour.
        w(self.style.MIGRATE_HEADING("\n4. Jour par jour (méthodes de puissance tentées)"))
        for d in r["daily"]:
            raw = d["power_raw"] or {}
            line = (f"  {d['date']} {d['status']:<24} runtime {_v(d['runtime_h'], 2)} h ({d['runtime_source'] or '—'}) · "
                    f"P_GE {_v(d['p_ge_kw'])} kW [{d['power_method'] or '—'}] · CPH {_v(d['cph_l_h'])} L/h · "
                    f"conso {_v(d['conso_l'], 2)} L · mesurée {_v(d['measured_l'], 2)} L")
            w(line)
            if d["power_trace"]:
                w(f"      brut : production {_v(raw.get('production_kwh'))} kWh · P_DSE {_v(raw.get('p_dse_kw'))} kW · "
                  f"P_DC {_v(raw.get('p_dc_kw'))} kW · η {_v(raw.get('rendement'), 4)} · Active Power Avg {_v(raw.get('ac_avg_brut'), 1)} W · "
                  f"plafond {_v(d['power_cap_kw'])} kW")
                for meth, t in d["power_trace"].items():
                    if t["statut"] == "NON_APPLICABLE" and not t["motif"]:
                        continue
                    w(f"      {meth:<22} {t['statut']:<14} {_v(t['valeur_kw'])} kW · {t['motif'] or t['detail'] or ''}")
            elif d["motifs"]:
                w(f"      {' ; '.join(d['motifs'][:2])}")

        # 5. Synthèse.
        w(self.style.MIGRATE_HEADING("\n5. Synthèse"))
        w(f"  statut_cph {r['statut_cph']} · jours calculés {r['conso_days']}/{r['days']} (dont CPH {r['cph_days']}) · "
          f"conso estimée {_v(r['conso_theorique_l'] if r['conso_theorique_l'] is not None else r['conso_partielle_l'], 1)} L")
        w(f"  méthodes de puissance : {r['power_method_days'] or '—'}")
        for code, b in sorted(r["blocked_days"].items(), key=lambda kv: -kv[1]["jours"]):
            w(f"  jours non calculés · {code} : {b['jours']} j, runtime {_v(b['runtime_h'], 1)} h, mesurée {_v(b['mesuree_l'], 1)} L")
        w(f"  rapprochement : {r['statut_rapprochement_calcul']} ({r['rapprochement_statut']})")

        if snowflake:
            self._snowflake(r, ac, start, end)

    def _snowflake(self, r, ac, start, end):
        """Relecture brute LECTURE SEULE (SELECT) des points AC_METER et GENSET_REPORT du site."""
        from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, _connect

        w = self.stdout.write
        if r["data_id"] is None:
            w(self.style.ERROR("DATA_ID inconnu : relecture Snowflake impossible."))
            return
        lookback = start - timedelta(days=E.AC_REFERENCE_LOOKBACK_DAYS)
        conn = _connect()
        try:
            cur = conn.cursor()
            w(self.style.MIGRATE_HEADING("\n6. Snowflake AC_METER (lecture seule) — points par jour"))
            cur.execute(
                f"""SELECT DATE, COUNT(*) AS lignes, COUNT(ACT_ACTIVE_POWER_AVG) AS points,
                           SUM(IFF(ACT_ACTIVE_POWER_AVG = 0, 1, 0)) AS points_zero,
                           MIN(ACT_ACTIVE_POWER_AVG), MEDIAN(ACT_ACTIVE_POWER_AVG), AVG(ACT_ACTIVE_POWER_AVG),
                           MAX(ACT_ACTIVE_POWER_AVG)
                    FROM {ANALYTICS}.AC_METER
                    WHERE DATA_ID = %(id)s AND DATE >= %(s)s AND DATE <= %(e)s
                    GROUP BY DATE ORDER BY DATE""",
                {"id": r["data_id"], "s": lookback, "e": end})
            ref = set((ac or {}).get("reference_dates") or [])
            for d, lignes, pts, zeros, mn, med, avg, mx in cur.fetchall():
                tag = " ← jour de référence" if d in ref else ""
                w(f"  {d} · lignes {lignes} · points non NULL {pts} · points = 0 : {zeros} · min {_v(mn, 1)} · "
                  f"médiane {_v(med, 1)} · moyenne {_v(avg, 1)} · max {_v(mx, 1)} (W){tag}")
            w(self.style.MIGRATE_HEADING("\n7. Snowflake GENSET_REPORT (lecture seule) — production et runtime DSE"))
            cur.execute(
                f"""SELECT REPORT_DATE, COUNT(*), MAX(DG_RUNTIME_CONTROLLER), MAX(DG_RUNTIME_CALCULATED), MAX(DG_PRODUCTION_KWH)
                    FROM {ANALYTICS}.GENSET_REPORT
                    WHERE DATA_ID = %(id)s AND REPORT_DATE >= %(s)s AND REPORT_DATE <= %(e)s
                    GROUP BY REPORT_DATE ORDER BY REPORT_DATE""",
                {"id": r["data_id"], "s": start, "e": end})
            for d, n, dse, dgon, prod in cur.fetchall():
                w(f"  {d} · lignes {n} · DSE {_v(dse, 2)} · Day DG On {_v(dgon, 2)} · production {_v(prod, 3)} kWh")
        finally:
            conn.close()
