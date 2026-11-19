# fuel_tracking/management/commands/audit_suivi_conso.py
"""
AUDIT GLOBAL du tableau Suivi Conso — LECTURE SEULE (ENOC, Snowflake, fichiers et base EnerTrack jamais modifiés).

Pour chaque site du mois, chaque valeur AFFICHÉE est comparée à la valeur relue INDÉPENDAMMENT à la source, sur la plage
« Date de Relevé Initiale » → « Date de Relevé Finale » de la ligne du fichier du site :

  Fichier de clôture   copie d'archive du fichier du mois relue : dates, quantités et compteurs de terrain (initial / final) ;
  ENOC (MongoDB)       relevés de gardien du jour exact (initial / final) ; registre des GE ; demandes absentes de notre copie ;
  ENOC (copie locale)  toutes les opérations brutes du site sur la plage, avec la raison quand elles ne sont pas comptées ;
  Snowflake            niveau FMS et compteur GE du jour exact (VW_FUEL_REPORT, GENSET_REPORT) ; fabricant / type (SITE_DG).

Statuts par valeur : OK · MANQUANT (la source a une valeur, le tableau non) · ECART (valeurs différentes) ·
SANS_SOURCE (le tableau affiche une valeur que la source n'a pas) · ABSENT (ni l'un ni l'autre).

  python manage.py audit_suivi_conso --mois 2026-10 [--csv /tmp/audit.csv] [--sans-snowflake]
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand


def _f(v):
    try:
        return None if v is None or v == "" else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _d(v) -> date | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _status(shown, source, tol=0.5):
    if shown is None and source is None:
        return "ABSENT"
    if shown is None:
        return "MANQUANT"
    if source is None:
        return "SANS_SOURCE"
    if isinstance(shown, (int, float)) and isinstance(source, (int, float)):
        return "OK" if abs(shown - source) <= tol else "ECART"
    return "OK" if str(shown).strip().upper() == str(source).strip().upper() else "ECART"


class Command(BaseCommand):
    help = "Audit global du Suivi Conso : valeurs affichées vs sources (ENOC, Snowflake, fichiers), lecture seule."

    def add_arguments(self, parser):
        parser.add_argument("--mois", default="2026-10")
        parser.add_argument("--csv", default=None)
        parser.add_argument("--sans-snowflake", action="store_true", help="Ne pas relire Snowflake (VW_FUEL_REPORT est lente).")

    def handle(self, *args, mois, csv: str | None, sans_snowflake: bool, **opts):  # noqa: C901
        from fuel_tracking.services import conso_comparison as C
        from fuel_tracking.services import stock_initial_sources as S

        y, m = map(int, mois.split("-"))
        d_from, d_to = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        w = self.stdout.write
        w(f"== AUDIT Suivi Conso {mois} (lecture seule) ==")
        rows = C.build(d_from, d_to, {}, with_cph_quality=False)["rows"]
        S.enrich(rows)   # valeurs telles qu'affichées (gardien ENOC lu si absent du cache)
        sites = {r["site_id"].upper(): r for r in rows}
        w(f"Sites du tableau : {len(rows)}")
        out: list[dict] = []

        def add(sid, bloc, champ, shown, source, src_name, detail=""):
            out.append({"site": sid, "bloc": bloc, "champ": champ, "affiche": shown, "source": source,
                        "statut": _status(shown, source), "source_nom": src_name, "detail": detail})

        # ── 1. Fichier de clôture du mois (copie d'archive relue) ──
        fichier = self._file_lines(mois)
        w(f"Fichier du mois : {fichier.get('_nom') or 'introuvable'} ({len(fichier) - 1 if fichier else 0} lignes)")
        for sid, r in sites.items():
            fl = fichier.get(sid) or {}
            si, sf = r.get("stock_initial_sources") or {}, r.get("stock_final_sources") or {}
            src = f"Fichier ({fichier.get('_nom')})"
            add(sid, "STOCK INITIAL", "Date de Relevé Initiale", si.get("date"), _iso(fl.get("date_initiale")), src)
            add(sid, "STOCK INITIAL", "Qtité terrain", _f(si.get("terrain_l")), _f(fl.get("stock_initial_l")), src)
            add(sid, "STOCK INITIAL", "Compteur terrain", _f(si.get("compteur_terrain_h")), _f(fl.get("compteur_terrain_initial_h")), src)
            add(sid, "STOCK FINAL", "Date de Relevé Finale", sf.get("date"), _iso(fl.get("date_finale")), src)
            add(sid, "STOCK FINAL", "Qtité terrain", _f(sf.get("terrain_l")), _f(fl.get("stock_terrain_final_l")), src)
            add(sid, "STOCK FINAL", "Compteur terrain", _f(sf.get("compteur_terrain_h")), _f(fl.get("compteur_terrain_final_h")), src)

        # paires (site, jour) aux dates du FICHIER : la source de vérité des dates
        pairs_i = {(sid, _d(fl.get("date_initiale"))) for sid, fl in fichier.items() if sid != "_nom" and sid in sites and fl.get("date_initiale")}
        pairs_f = {(sid, _d(fl.get("date_finale"))) for sid, fl in fichier.items() if sid != "_nom" and sid in sites and fl.get("date_finale")}

        # ── 2. ENOC (MongoDB) : relevés de gardien du jour exact ──
        try:
            guard, gerr = S._fetch_guard({(r["site_id"], d) for (s, d) in pairs_i | pairs_f for r in [sites[s]]})
        except Exception as e:  # noqa: BLE001
            guard, gerr = {}, str(e)
        w(f"ENOC relevés de gardien : {len(guard)} relevé(s) trouvés aux dates du fichier{(' — ' + gerr) if gerr else ''}")
        for (sid, d) in pairs_i:
            g = guard.get((sites[sid]["site_id"], d)) or {}
            si = sites[sid].get("stock_initial_sources") or {}
            add(sid, "STOCK INITIAL", "Qtité gardiennage", _f(si.get("gardiennage_l")), _f(g.get("litres")), "ENOC fuel_level_readings", str(d))
            add(sid, "STOCK INITIAL", "Compteur gardiennage", _f(si.get("compteur_gardiennage_h")), _f(g.get("compteur")), "ENOC fuel_level_readings", str(d))
        for (sid, d) in pairs_f:
            g = guard.get((sites[sid]["site_id"], d)) or {}
            sf = sites[sid].get("stock_final_sources") or {}
            add(sid, "STOCK FINAL", "Qtité gardiennage", _f(sf.get("gardiennage_l")), _f(g.get("litres")), "ENOC fuel_level_readings", str(d))
            add(sid, "STOCK FINAL", "Compteur gardiennage", _f(sf.get("compteur_gardiennage_h")), _f(g.get("compteur")), "ENOC fuel_level_readings", str(d))

        # ── 3. Snowflake : niveau FMS et compteur du jour exact ; fabricant / type de GE ──
        if not sans_snowflake:
            fms, ge_sf, sferr = self._snowflake({s for s, _ in pairs_i | pairs_f}, {d for _, d in pairs_i | pairs_f})
            w(f"Snowflake : {len(fms)} valeur(s) site/jour, {len(ge_sf)} site(s) dans SITE_DG{(' — ' + sferr) if sferr else ''}")
            for blk, pairs, key in (("STOCK INITIAL", pairs_i, "stock_initial_sources"), ("STOCK FINAL", pairs_f, "stock_final_sources")):
                for (sid, d) in pairs:
                    v = fms.get((sid, d)) or {}
                    x = sites[sid].get(key) or {}
                    add(sid, blk, "Qtité FMS", _f(x.get("fms_l")), _f(v.get("niveau")), "Snowflake VW_FUEL_REPORT", str(d))
                    add(sid, blk, "Compteur FMS", _f(x.get("compteur_fms_h")), _f(v.get("compteur")), "Snowflake GENSET_REPORT", str(d))
        else:
            ge_sf = {}
        enoc_ge = self._enoc_ge()
        for sid, r in sites.items():
            e = r.get("estimation_theorique") or {}
            sfv, env = ge_sf.get(sid) or {}, enoc_ge.get(sid) or {}
            src_fab = sfv.get("fab") or env.get("fab")
            src_typ = sfv.get("typ") or env.get("typ")
            nom = "Snowflake SITE_DG" if sfv else ("ENOC ge_assets" if env else "—")
            add(sid, "ESTIMATION THEORIQUE", "Fabricant GE", e.get("fabricant_ge"), src_fab, nom)
            add(sid, "ESTIMATION THEORIQUE", "Type de GE", e.get("type_ge"), src_typ, nom)

        # ── 4. ENOC : opérations brutes sur la plage du site, comptées ou non (avec la raison) ──
        nc = self._enoc_operations(sites, fichier, add)
        w(f"ENOC : {nc['ops']} opération(s) brute(s) sur les plages des sites ; non comptées : {nc['non_comptees']} "
          f"({dict(nc['raisons'].most_common(8))})")
        manq = self._mongo_completeness(sites)
        if manq is not None:
            w(f"ENOC MongoDB → copie EnerTrack : {manq['absentes']} demande(s) des sites absentes de notre copie "
              f"(sur {manq['mongo']} dans MongoDB){(' ; ex. ' + ', '.join(manq['ex'])) if manq['ex'] else ''}")

        # ── Synthèse ──
        w("\n== SYNTHÈSE PAR CHAMP (OK / MANQUANT / ECART / SANS_SOURCE / ABSENT) ==")
        by = defaultdict(Counter)
        for o in out:
            by[(o["bloc"], o["champ"])][o["statut"]] += 1
        for (bloc, champ), c in sorted(by.items()):
            w(f"  {bloc:22} {champ:26} OK {c['OK']:4}  MANQUANT {c['MANQUANT']:4}  ECART {c['ECART']:4}  "
              f"SANS_SOURCE {c['SANS_SOURCE']:4}  ABSENT {c['ABSENT']:4}")
        w("\n== EXEMPLES (MANQUANT / ECART) ==")
        for (bloc, champ), _c in sorted(by.items()):
            ex = [o for o in out if o["bloc"] == bloc and o["champ"] == champ and o["statut"] in ("MANQUANT", "ECART")][:4]
            for o in ex:
                w(f"  {bloc} · {champ} · {o['site']} : affiché {o['affiche']} / source {o['source']} ({o['source_nom']} {o['detail']})".rstrip())
        path = csv or f"/tmp/audit_suivi_conso_{mois}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            wr = __import__("csv").DictWriter(fh, fieldnames=list(out[0].keys()) if out else ["site"], delimiter=";")
            wr.writeheader()
            wr.writerows(out)
        w(f"\nDétail complet : {path} ({len(out)} lignes)")

    # ── sources ──
    def _file_lines(self, mois: str) -> dict:
        """site → ligne du fichier de clôture du mois, relue dans la copie d'archive (Output App Fuel ou Closing)."""
        from pathlib import Path

        from fuel_tracking.models import FuelImportBatch
        from fuel_tracking.services import output_app_fuel as OAF
        from fuel_tracking.services.stock_file_analysis import CLOSING_SHEET, closing_stock_rows
        from fuel_tracking.services.stock_import_files import iter_sheets
        from fuel_tracking.services.stock_period_balance import month_file_batches
        from fuel_tracking.services.stock_registry import SiteResolver

        for bid in month_file_batches(mois):
            b = FuelImportBatch.objects.filter(pk=bid).first()
            if not b or not b.archive_path or not Path(b.archive_path).is_file():
                continue
            oaf = ((b.summary or {}).get("stocks_closing") or {}).get("format") == OAF.FILE_LABEL
            recs = []
            for _n, rows in iter_sheets(Path(b.archive_path).read_bytes(), b.file_name, only={OAF.SHEET if oaf else CLOSING_SHEET}):
                recs = OAF.parse_rows(list(rows)) if oaf else closing_stock_rows(list(rows))[0]
            res = SiteResolver()
            out = {"_nom": b.file_name}
            for r in recs:
                sid = (res.resolve(r["site"]) or r["site"]).upper()
                if r.get("date_initiale") or r.get("date_finale"):
                    out.setdefault(sid, r)   # première ligne datée du site
            if len(out) > 1:
                return out
        return {}

    def _snowflake(self, sites: set, days: set):
        from fuel_tracking.models import FuelSiteInventory as INV
        from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, FUEL_REPORT, _connect

        inv = {}
        for did, sid in INV.objects.filter(site_id__in=list(sites)).values_list("data_id", "site_id"):
            inv[int(did)] = sid.upper()
        fms, ge, errs = {}, {}, []
        if not inv or not days:
            return fms, ge, "aucun site / jour"
        conn = _connect()
        try:
            cur = conn.cursor()
            ids, ds = ",".join(str(i) for i in inv), ",".join(f"'{d.isoformat()}'" for d in sorted(days))
            for label, sql, key in (
                ("VW_FUEL_REPORT", f"SELECT DATA_ID, DATE, IFF(MIN(LAST_VALID_LEVEL)=MAX(LAST_VALID_LEVEL), MAX(LAST_VALID_LEVEL), NULL) "
                 f"FROM {FUEL_REPORT}.VW_FUEL_REPORT WHERE DATA_ID IN ({ids}) AND DATE IN ({ds}) GROUP BY 1, 2", "niveau"),
                ("GENSET_REPORT", f"SELECT DATA_ID, DATE, IFF(MIN(TOTAL_DG_RUNNING_HOUR)=MAX(TOTAL_DG_RUNNING_HOUR), MAX(TOTAL_DG_RUNNING_HOUR), NULL) "
                 f"FROM {ANALYTICS}.GENSET_REPORT WHERE DATA_ID IN ({ids}) AND DATE IN ({ds}) GROUP BY 1, 2", "compteur"),
            ):
                try:
                    cur.execute(sql)
                    for did, d, v in cur.fetchall():
                        if int(did) in inv and v is not None:
                            fms.setdefault((inv[int(did)], _d(d)), {})[key] = v
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{label} : {str(e).splitlines()[0][:120]}")
            try:
                cur.execute(f"SELECT UPPER(e.SITE_ID), d.NOM, d.VENDOR, d.GENSET_TYPE FROM {ANALYTICS}.SITE_DG d "
                            f"JOIN {ANALYTICS}.SITE_ESCO_CURRENT e ON e.ID = d.S_ID ORDER BY 1, 2")
                for sid, _nom, v, t in cur.fetchall():
                    g = ge.setdefault(sid, {"fab": [], "typ": []})
                    for k, x in (("fab", v), ("typ", t)):
                        x = " ".join(str(x).split()) if x not in (None, "", "None") else None
                        if x and x not in g[k]:
                            g[k].append(x)
                ge = {s: {"fab": " / ".join(g["fab"]) or None, "typ": " / ".join(g["typ"]) or None} for s, g in ge.items()}
            except Exception as e:  # noqa: BLE001
                errs.append(f"SITE_DG : {str(e).splitlines()[0][:120]}")
        finally:
            conn.close()
        return fms, ge, " ; ".join(errs)

    def _enoc_ge(self) -> dict:
        from django.conf import settings

        from fuel_tracking.services.enoc_mongo_service import _connect

        try:
            client = _connect()
        except Exception:  # noqa: BLE001
            return {}
        try:
            out: dict = {}
            for d in client[settings.ENOC_MONGO_DB_NAME].ge_assets.find({"asset_status": "INSTALLED"}, {"_id": 0}):
                sid = str(d.get("current_site_id") or d.get("site_id") or "").upper()
                g = out.setdefault(sid, {"fab": [], "typ": []})
                for k, x in (("fab", d.get("brand")), ("typ", d.get("model"))):
                    x = " ".join(str(x).split()) if x not in (None, "", "None") else None
                    if x and x not in g[k]:
                        g[k].append(x)
            return {s: {"fab": " / ".join(g["fab"]) or None, "typ": " / ".join(g["typ"]) or None} for s, g in out.items()}
        finally:
            client.close()

    def _enoc_operations(self, sites: dict, fichier: dict, add) -> dict:
        """Opérations ENOC brutes (copie locale) sur la plage du fichier de chaque site : comptées ou non, et pourquoi."""
        from django.utils import timezone

        from fuel_tracking.models import FuelEnocMovement as EM, FuelEnocQualifiedMovement as EQ

        tz = timezone.get_current_timezone()
        at = lambda d: timezone.make_aware(datetime.combine(d, datetime.min.time()), tz)  # noqa: E731
        bounds = {}
        for sid in sites:
            fl = fichier.get(sid) or {}
            d0, d1 = _d(fl.get("date_initiale")), _d(fl.get("date_finale"))
            if d0 and d1:
                bounds[sid] = (at(d0), at(d1))
        stats = {"ops": 0, "non_comptees": 0, "raisons": Counter()}
        if not bounds:
            return stats
        lo, hi = min(b[0] for b in bounds.values()), max(b[1] for b in bounds.values())
        q = {e.enoc_movement_id: e for e in EQ.objects.filter(enoc_movement__operation_date__gt=lo, enoc_movement__operation_date__lte=hi)}
        brut = defaultdict(lambda: defaultdict(float))
        for m in EM.objects.filter(operation_date__gt=lo, operation_date__lte=hi).exclude(status="rejected"):
            sid = (m.site_id or "").upper()
            if sid not in bounds or not (bounds[sid][0] < m.operation_date <= bounds[sid][1]):
                continue
            qty = _f(m.quantity_added_liters)
            if not qty:
                continue
            stats["ops"] += 1
            e = q.get(m.pk)
            typ = (m.operation_type or "?").upper()
            brut[sid][typ] += qty
            counted = e is not None and e.integration_status == "INTEGRE" and (e.site_id_resolved or "").upper() == sid
            if not counted:
                stats["non_comptees"] += 1
                why = ("non qualifiée" if e is None else f"{e.integration_status}"
                       + (f" / {e.non_integration_cause}" if getattr(e, "non_integration_cause", None) else "")
                       + ("" if (e.site_id_resolved or "").upper() == sid else f" / site résolu {e.site_id_resolved}"))
                stats["raisons"][f"{typ} : {why}"] += 1
                add(sid, "RAVITAILLEMENT", f"Opération ENOC non comptée ({typ})", None, qty, "ENOC fuel_requests/operations",
                    f"{m.source_id} du {m.operation_date:%d/%m/%Y} — {why}")
        for sid, r in sites.items():
            rv = r.get("ravitaillement") or {}
            tot = brut.get(sid) or {}
            add(sid, "RAVITAILLEMENT", "Total ENOC affiché (6 colonnes)",
                _f(sum((rv.get(k) or 0) for k in ("eydon_l", "carte_total_l", "ajout_l", "retrait_l", "vole_l", "fuite_l"))) if rv.get("debut") else None,
                _f(sum(tot.values())) if sid in bounds else None, "ENOC copie brute (toutes opérations)",
                ", ".join(f"{k} {v:g} L" for k, v in sorted(tot.items())))
        return stats

    def _mongo_completeness(self, sites: dict):
        from django.conf import settings

        from fuel_tracking.models import FuelEnocMovement as EM
        from fuel_tracking.services.enoc_mongo_service import _connect

        try:
            client = _connect()
        except Exception:  # noqa: BLE001
            return None
        try:
            ids = list({s for s in sites} | {s.lower() for s in sites})
            mongo = {str(r.get("request_code") or r.get("_id")) for r in client[settings.ENOC_MONGO_DB_NAME].fuel_requests.find(
                {"site_id": {"$in": ids}}, {"request_code": 1})}
            local = set(EM.objects.filter(source_id__in=list(mongo)).values_list("source_id", flat=True))
            miss = sorted(mongo - local)
            return {"mongo": len(mongo), "absentes": len(miss), "ex": miss[:5]}
        finally:
            client.close()


def _iso(v):
    d = _d(v)
    return d.isoformat() if d else None
