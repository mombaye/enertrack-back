"""
Recette du calcul CPH sur une période (lecture seule, données réelles en base) :
compteurs par étape de la chaîne, couvertures, synthèse des blocages et exemples
de sites avec toutes les valeurs intermédiaires.

    python manage.py cph_recette --start 2026-09-01 --end 2026-09-30
    python manage.py cph_recette --start 2026-10-01 --end 2026-10-12 --examples 10
"""
from datetime import date

from django.core.management.base import BaseCommand

from fuel_tracking import views_cph as V
from fuel_tracking.services import cph_engine as E
from fuel_tracking.services.cph_service import compute_period


def _v(x, places=2):
    return "—" if x is None else f"{float(x):.{places}f}"


class Command(BaseCommand):
    help = "Recette CPH : compteurs, couvertures, blocages et exemples réels sur une période."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=date.fromisoformat, required=True)
        parser.add_argument("--end", type=date.fromisoformat, required=True)
        parser.add_argument("--country", default=None)
        parser.add_argument("--examples", type=int, default=10)

    def handle(self, *args, start, end, country, examples, **opts):
        w = self.stdout.write
        all_rows = compute_period(start, end, country=country)
        rows = [r for r in all_rows if r["perimetre"] == E.PERIMETRE_GE]
        others = [r for r in all_rows if r["perimetre"] != E.PERIMETRE_GE]
        has = lambda m: [r for r in rows if r["power_method_days"].get(m)]  # noqa: E731
        periode = V._periode_info(start, end)
        w(self.style.MIGRATE_HEADING(f"== Recette CPH {start} → {end} · règle {E.RULE_VERSION}"))
        if periode["incomplete"]:
            w(self.style.WARNING(f"PÉRIODE INCOMPLÈTE : faits Snowflake jusqu'au {periode['donnees_jusqu_au']} "
                                 f"({periode['jours_sans_donnees']} jour(s) sans données)."))
        counts = [
            ("Sites du référentiel (affichés)", len(all_rows)),
            ("Sites hors calcul (sans GE confirmé)", len(others)),
            ("  dont GE déclaré Ops non confirmé", sum(1 for r in others if r["motif_cph"]["code"] == E.MC_GE_A_CONFIRMER)),
            ("Sites GE du périmètre", len(rows)),
            ("Runtime qualifié", sum(1 for r in rows if r["runtime_days"] > 0)),
            ("Mapping valide (courbe utilisable)", sum(1 for r in rows if r["curve"] is not None)),
            ("Puissance DSE directe / production GE", len(has(E.PM_DIRECT))),
            ("Puissance Outdoor P_DC / rendement", len(has(E.PM_PDC))),
            ("Puissance Indoor P_DC + load AC", len(has(E.PM_INDOOR))),
            ("CPH calculé (complet)", sum(1 for r in rows if r["statut_cph"] == E.SC_CPH_CALCULE)),
            ("CPH partiel", sum(1 for r in rows if r["statut_cph"] == E.SC_CPH_PARTIEL)),
            ("CPH non calculé", sum(1 for r in rows if r["statut_cph"] == E.SC_CPH_NON_CALCULE)),
            ("Consommation mesurée", sum(1 for r in rows if r["comparaison"]["conso_mesuree_l"] is not None)),
            ("Rapprochement stock possible", sum(1 for r in rows if r["statut_rapprochement_calcul"] == E.SR_CALCULE)),
        ]
        for label, n in counts:
            w(f"  {label:<42} {n}")
        ac = {}
        for r in rows:
            st = (r["ac_reference"] or {}).get("statut")
            if st:
                ac[st] = ac.get(st, 0) + 1
        w(f"  Load AC historique (sites indoor)         {ac or '—'}")

        w(self.style.MIGRATE_HEADING("\nCouvertures"))
        for c in V._couvertures(rows):
            w(f"  {c['label']:<38} {c['numerateur']}/{c['denominateur']} = {_v(c['pct'], 1)} %")

        w(self.style.MIGRATE_HEADING("\nSynthèse des blocages (sites · jours · runtime h · mesurée L · volume potentiel L)"))
        for b in V._diagnostic_blocages(rows):
            if b["sites"]:
                w(f"  {b['label']:<52} {b['sites']:>4} · {b['jours']:>5} · {_v(b['runtime_h'], 1):>8} · "
                  f"{_v(b['conso_mesuree_l'], 1):>8} · {_v(b['volume_potentiel_l'], 1):>8}")

        w(self.style.MIGRATE_HEADING(f"\n{examples} exemples (un jour type en marche + la période)"))
        for r in self._pick(rows, examples):
            self._example(r)

    @staticmethod
    def _pick(rows, n):
        """Exemples variés : chaque méthode de puissance, partiels, puis non calculés (ordre stable)."""
        order = sorted(rows, key=lambda r: r["site_id"])
        buckets = [
            [r for r in order if r["power_method_days"].get(E.PM_DIRECT)],
            [r for r in order if r["power_method_days"].get(E.PM_PDC)],
            [r for r in order if r["power_method_days"].get(E.PM_INDOOR)],
            [r for r in order if r["statut_cph"] == E.SC_CPH_PARTIEL],
            [r for r in order if r["statut_cph"] == E.SC_CPH_NON_CALCULE and r["running_days"] > 0],
            [r for r in order if (r["ac_reference"] or {}).get("statut") == E.AC_MESURE_ZERO],
        ]
        picked, seen = [], set()
        while len(picked) < n and any(buckets):
            for b in buckets:
                while b and b[0]["site_id"] in seen:
                    b.pop(0)
                if b and len(picked) < n:
                    r = b.pop(0)
                    picked.append(r)
                    seen.add(r["site_id"])
        return picked

    def _example(self, r):
        w = self.stdout.write
        days = [d for d in r["daily"] if d["runtime_h"] is not None and d["runtime_h"] > E.D0]
        day = next((d for d in days if d["cph_l_h"] is not None), days[0] if days else None)
        c, cmp_ = r["curve"], r["comparaison"]
        conso = r["conso_theorique_l"] if r["conso_theorique_l"] is not None else r["conso_partielle_l"]
        ac = r["ac_reference"] or {}
        w(f"\n  {r['site_id']} {r['site_name']} · {r['kind'] or 'INCONNUE'} · {r['ge_label']} · "
          f"{_v(c.prp_kva, 0) if c else '—'} kVA · courbe {c.curve_id if c else '—'}")
        w(f"    période {r['start']} → {r['end']} : runtime {_v(r['runtime_total_h'], 1)} h ({r['runtime_source_main']}) · "
          f"P_GE moy {_v(r['p_ge_moy_kw'])} kW ({V._power_method_main(r) or '—'}) · charge {_v((r['charge_moy'] or 0) * 100 if r['charge_moy'] is not None else None, 1)} % · "
          f"CPH {_v(r['cph_moy_l_h'])} L/h · conso estimée {_v(conso, 1)} L [{r['statut_cph']} {r['conso_days']}/{r['days']} j] · "
          f"mesurée {_v(cmp_['conso_mesuree_l'], 1)} L · écart {_v(cmp_['ecart_l'], 1)} L / {_v(cmp_['ecart_pct'], 1)} % ({cmp_['statut']})")
        if ac:
            w(f"    load AC historique {_v(ac.get('p_ac_aux_kw'), 3)} kW [{ac.get('statut')}, {ac.get('jours_reference')} j, "
              f"médiane brute {_v(ac.get('mediane_ac_brut'), 1)} W]")
        if r["statut_cph"] != E.SC_CPH_CALCULE:
            w(f"    motif : {(r['motif_cph'] or {}).get('code')} — {(r['motif_cph'] or {}).get('detail')}")
        if day:
            raw = day["power_raw"] or {}
            w(f"    jour {day['date']} : runtime {_v(day['runtime_h'])} h ({day['runtime_source']}) · P_DSE {_v(raw.get('p_dse_kw'), 3)} kW · "
              f"P_DC {_v(raw.get('p_dc_kw'), 3)} kW · η {_v(raw.get('rendement'), 4)} · Active Power Avg {_v(raw.get('ac_avg_brut'), 1)} W · "
              f"P_GE {_v(day['p_ge_kw'], 3)} kW [{day['power_method'] or '—'}] · charge {_v(day['charge'] * 100 if day['charge'] is not None else None, 1)} % · "
              f"CPH {_v(day['cph_l_h'], 3)} L/h · conso {_v(day['conso_l'])} L · mesurée {_v(day['measured_l'])} L")
            if day["motifs"]:
                w(f"      motifs : {' ; '.join(day['motifs'][:2])}")
