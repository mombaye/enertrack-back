"""
Analyse PRÉALABLE, en lecture seule, de TOUS les fichiers de stock d'un dossier (aucun import, aucune écriture en base).
Pour chaque fichier : feuille retenue, période, lignes, colonnes, mapping colonne → champ eGrid, unité, sites reconnus / non reconnus,
relevés en litres / en cm, lignes qui seraient rejetées et motifs.

    python manage.py analyze_stock_files                       # incoming/* (source = sous-dossier)
    python manage.py analyze_stock_files --dossier /mnt/Suivi_carburant_prod/archive --json /tmp/analyse.json
    python manage.py analyze_stock_files --fichier /chemin/Loliforce.xlsx --source LOLIFORCE
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services import stock_import_pipeline as P
from fuel_tracking.services.stock_file_analysis import IMPORT_SOURCE_LABEL, analyze


def source_of(path: Path, forced: str | None) -> str | None:
    if forced:
        return forced.upper()
    for part in reversed(path.parts[:-1]):
        if part.lower() in P.INCOMING:
            return P.INCOMING[part.lower()]
    return None  # détection automatique (colonnes / nom du fichier)


class Command(BaseCommand):
    help = "Analyse en lecture seule des fichiers de stock (mapping, unités, sites, lignes rejetées) avant tout import."

    def add_arguments(self, parser):
        parser.add_argument("--dossier", default=None, help="Dossier à parcourir (défaut : <FUEL_IMPORT_ROOT>/incoming).")
        parser.add_argument("--fichier", default=None, help="Un seul fichier.")
        parser.add_argument("--source", default=None, help="LOLIFORCE, SCSS, CLOSING, HISTORICAL_REFUELING, TANK_MODELS")
        parser.add_argument("--json", default=None, help="Écrit l'analyse complète (JSON) dans ce fichier.")

    def handle(self, *args, dossier, fichier, source, json: str | None = None, **opts):  # noqa: A002
        if fichier:
            files = [Path(fichier)]
        else:
            base = Path(dossier) if dossier else P.root() / "incoming"
            if not base.is_dir():
                raise CommandError(f"Dossier introuvable : {base}")
            files = sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in P.ACCEPTED_EXT and not p.name.startswith((".", "~$")))
        if not files:
            raise CommandError("Aucun fichier .xlsx / .xlsm / .xlsb trouvé.")
        out, w = [], self.stdout.write
        for f in files:
            if not f.is_file():
                raise CommandError(f"Fichier introuvable : {f}")
            try:
                res = analyze(f.read_bytes(), f.name, source_of(f, source))
            except Exception as e:  # fichier illisible : signalé, les autres continuent
                w(f"\n=== {f.name}\n  ILLISIBLE : {e.__class__.__name__}: {e}")
                out.append({"fichier": f.name, "erreur": str(e)})
                continue
            out.append(res)
            self.print_one(res, w)
        if json:
            import json as jsonlib
            Path(json).write_text(jsonlib.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            w(f"\nAnalyse complète écrite dans {json}")
        w("\nAUCUNE donnée importée ni modifiée.")

    def print_one(self, r: dict, w):
        w(f"\n=== {r['fichier']}")
        w(f"  Source        : {r.get('libelle_source') or r.get('source_detectee') or 'non détectée'} (détectée : {r.get('source_detectee')}, mapping {r.get('version_mapping')})")
        w(f"  Empreinte     : {r['empreinte_sha256'][:16]}… · {r['taille_octets']} octets")
        w(f"  Feuilles      : retenues {r.get('feuilles_retenues')} · ignorées {[x['feuille'] for x in r['feuilles'] if x.get('ignoree')]}")
        w(f"  Période       : {r.get('periode_detectee')} · lignes de données : {r.get('lignes_total')}")
        cov = r["couverture_sites"]
        w(f"  Sites         : {cov['distincts']} distincts, {cov['reconnus']} reconnus, {cov['inconnus']} non reconnus")
        cs = r.get("closing_stocks")
        if cs:
            w("  Stocks du Closing (aperçu, non importé) :")
            if not cs.get("en_tete_trouve"):
                w("    en-tête « id du site / date de relevé initiale » introuvable")
            else:
                w(f"    sites lus {cs['sites_lus']} (reconnus {cs['sites_reconnus']}) · stock initial {cs['stock_initial_present']} · stock final {cs['stock_final_present']} · les deux + dates {cs['sites_avec_les_deux_stocks_et_dates']}")
                w(f"    dates initiales {cs['dates_initiales']} · dates finales {cs['dates_finales']}")
                w(f"    stock initial (L) {cs['stock_initial_l']} · stock final (L) {cs['stock_final_l']} · livré (L) {cs['livre_l']}")
                cc = cs["controle_coherence"]
                w(f"    contrôle : {cc['formule_coherente']}/{cc['sites_verifies']} sites cohérents avec la conso du Closing — {cc['formule']}")
                for x in cc["exemples_ecarts"]:
                    w(f"      écart {x}")
                w(f"    colonnes utilisées : {cs['colonnes_utilisees']}")
        c = r.get("controle_lignes")
        if c:
            if c.get("gardiennage_declare"):
                w(f"  Gardiennage déclaré dans le fichier : {c['gardiennage_declare']} (colonne « Site gardé par »)")
            if c.get("lignes_modele_vides_ignorees"):
                w(f"  Lignes de modèle vides ignorées (ni date ni niveau) : {c['lignes_modele_vides_ignorees']}")
            for fs in c.get("feuilles_suspectes") or []:
                w(f"  ⚠ Feuille « {fs['feuille']} » : {fs['message']}")
            if c.get("dates_invraisemblables"):
                w(f"  Dates invraisemblables (rejetées) : {c['dates_invraisemblables']}")
            w(f"  Unité         : {c['unite_detectee']} — relevés en litres {c['releves_en_litres']}, en cm {c['releves_en_cm']}, lignes avec deux unités {c['lignes_avec_deux_unites']}")
            w(f"  Contrôle      : {c['lignes_lues']} lues, {c['lignes_acceptees']} acceptées, {c['lignes_rejetees']} rejetées — motifs {c['motifs_rejet']}")
            if c["sites_non_reconnus_detail"]:
                w(f"  Non reconnus  : {c['sites_non_reconnus_detail']}")
            for x in c["exemples_rejets"][:5]:
                w(f"    rejet {x['feuille']} l.{x['ligne']} {x['site']} : {' | '.join(x['motifs'])}")
        for s in r["feuilles"]:
            w(f"  - Feuille « {s['feuille']} » : en-tête ligne {s['ligne_en_tete']}, {s['lignes_de_donnees']} lignes, dates {s['plage_dates']}"
              + (f" — IGNORÉE : {s['ignoree'][:80]}" if s.get("ignoree") else ""))
            if s["colonnes_mappees"]:
                w("      mapping : " + " ; ".join(f"{k} → {v}" for k, v in s["colonnes_mappees"].items()))
            nr = s["colonnes_non_reconnues"]
            if nr:
                w(f"      non reconnues ({len(nr)}) : " + " ; ".join(nr[:12]) + (" …" if len(nr) > 12 else ""))
            elif not s["colonnes_mappees"] and s.get("colonnes"):
                w("      colonnes : " + " ; ".join(map(str, s["colonnes"][:15])) + (" …" if len(s["colonnes"]) > 15 else ""))
            if s.get("dates_invraisemblables"):
                w(f"      dates invraisemblables : {s['dates_invraisemblables']}")
