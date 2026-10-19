"""
Registre stock — bilan de recette en LECTURE SEULE (aucune écriture en base) : répond point par point au
questionnaire de contrôle (modèles, affectation des cuves, conversion, stock retenu, validations, gardiennage,
ENOC, profil énergétique, cas de bout en bout) avec les chiffres et des exemples réels de la base.

    python manage.py fuel_stock_audit                       # Markdown sur la sortie standard
    python manage.py fuel_stock_audit --out /mnt/Suivi_carburant_prod/bilan_stock.md
"""
from collections import Counter
from io import StringIO

from django.core.management.base import BaseCommand

SEP = " | "


def _f(v, digits=1):
    if v is None:
        return "NULL"
    try:
        return f"{float(v):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _d(v):
    return v.strftime("%d/%m/%Y %H:%M") if v else "—"


def _dims(t):
    return " × ".join(_f(x, 0) for x in (t.dimension_1_cm, t.dimension_2_cm, t.dimension_3_cm) if x) or "—"


class Report:
    def __init__(self):
        self.buf = StringIO()

    def h(self, text):
        self.buf.write(f"\n## {text}\n\n")

    def p(self, text=""):
        self.buf.write(f"{text}\n")

    def table(self, head, rows):
        if not rows:
            self.p("_Aucune ligne dans la base._\n")
            return
        self.p("| " + SEP.join(head) + " |")
        self.p("|" + "---|" * len(head))
        for r in rows:
            self.p("| " + SEP.join(str(x).replace("|", "/").replace("\n", " ") for x in r) + " |")
        self.p()


class Command(BaseCommand):
    help = "Bilan de recette du registre stock (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--out", help="Fichier Markdown de sortie (sinon : sortie standard).")

    def handle(self, *args, out=None, **opts):
        r = Report()
        r.p("# Bilan de recette — registre stock carburant (lecture seule, données de la base)")
        for title, fn in [
            ("1. Bibliothèque de modèles de cuves", self.models),
            ("2. Affectation des cuves aux sites", self.tanks),
            ("3. Conversion cm → litres", self.conversions),
            ("4. Stock retenu", self.retained),
            ("5. Validations et traçabilité", self.validations),
            ("6. Gardiennage (Loliforce / SCSS)", self.guard),
            ("7. ENOC", self.enoc),
            ("8. Typologie et profil énergétique", self.energy),
            ("9. Cas de bout en bout (données réelles)", self.e2e),
        ]:
            r.h(title)
            try:
                fn(r)
            except Exception as e:  # une section en échec n'empêche pas les autres
                r.p(f"**Section en erreur : {type(e).__name__} : {e}**")
        text = r.buf.getvalue()
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.stdout.write(f"Bilan écrit dans {out}")
        else:
            self.stdout.write(text)

    # 1 ────────────────────────────────────────────────────────────────────────────────────────────
    def models(self, r):
        from fuel_tracking.models import FuelTankModel
        from fuel_tracking.services import tank_geometry as G

        qs = list(FuelTankModel.objects.order_by("tank_model_id"))
        r.p(f"Modèles extraits : **{len(qs)}** — statuts : {dict(Counter(m.model_status for m in qs))}.")
        r.p(f"Formes prises en charge par le moteur ({G.GEOMETRIC_FORMULA_VERSION}) : "
            + ", ".join(f"{k} = {v}" for k, v in G.FAMILIES.items()) + ".")
        r.p("Profils de fond : " + ", ".join(f"{k} = {v}" for k, v in G.PROFILES.items()) + ".\n")
        r.p("Formules (Notice §4 du calculateur, tables Ψ/Φ de l'onglet Profils) :")
        r.p("- Horizontales (h-circ, h-elli, h-oblo) : V(h) = A(h)·L + 2·Z·(A_pleine/π)·Ψ(h/Hc) ; A(h) = aire du segment "
            "(cercle, ellipse ou oblong) à la hauteur h.")
        r.p("- Verticale cylindrique (v-cyl) : fond V = πR²·Zb·[Φ(1) − Φ(1 − h/Zb)] ; virole V = πR²·[Zb·Φ(1) + (h − Zb)] ; ciel idem.")
        r.p("- Tronconique (v-tronc) : V = (πh/3)(R1² + R1·r + r²), r = R1 + (R2 − R1)·h/H.")
        r.p("- Rectangulaire (rect) : V = A × h × L.")
        r.p("- Volume calibré = volume géométrique × k, k = capacité de plaque / volume géométrique plein, "
            "appliqué UNIQUEMENT si |écart| ≤ 5 % ; sinon volume géométrique = valeur technique (statut GEOMETRIC_ONLY).\n")
        r.table(["Modèle", "Forme", "Cotes (cm)", "Fond", "Plaque (L)", "Géom. plein (L)", "k", "Écart", "Statut", "Motif"],
                [[m.tank_model_id, m.shape_code, _dims(m), f"{m.bottom_profile or '—'} {('Z ' + _f(m.bottom_z_cm, 0)) if m.bottom_z_cm else ''}",
                  _f(m.nominal_capacity_l, 0), _f(m.geometric_full_l, 1), _f(m.calibration_k, 4),
                  (f"{float(m.plate_gap_pct):+.2f} %" if m.plate_gap_pct is not None else "—"), m.model_status, m.status_reason or ""]
                 for m in qs])

    # 2 ────────────────────────────────────────────────────────────────────────────────────────────
    def tanks(self, r):
        from fuel_tracking.models import FuelTankMaster

        tanks = list(FuelTankMaster.objects.filter(active=True).select_related("tank_model", "validated_by"))
        sites = {t.site_id for t in tanks}
        by_site = Counter(t.site_id for t in tanks)
        r.p(f"Cuves actives : **{len(tanks)}** sur **{len(sites)}** site(s) ; sites à plusieurs cuves : {sum(1 for n in by_site.values() if n > 1)}.")
        r.p(f"Sites avec au moins une cuve VALIDÉE : **{len({t.site_id for t in tanks if t.validation_status == 'VALIDEE'})}**.")
        r.p(f"Cuves par statut : {dict(Counter(t.validation_status for t in tanks))}.")
        r.p(f"Cuves par source d'affectation : {dict(Counter(t.source_type for t in tanks))} ; validées manuellement "
            f"(utilisateur) : {sum(1 for t in tanks if t.validated_by_id)}.")
        r.p(f"Modèle de bibliothèque : {dict(Counter(t.model_match_status or 'NONE' for t in tanks))} "
            "(PROPOSED = proposition, CONFIRMED = confirmé par un utilisateur).")
        no_dims = [t for t in tanks if t.tank_model_id and t.model_match_status in ("PROPOSED", "CONFIRMED")
                   and (t.shape_family is None or t.dimension_1_cm is None)]
        auto_confirmed = [t for t in tanks if t.model_match_status == "CONFIRMED" and not t.validated_by_id]
        r.p(f"Contrôle « modèle sur la capacité seule » : modèles reliés à une cuve SANS forme/cotes = **{len(no_dims)}** ; "
            f"modèles CONFIRMÉS sans utilisateur = **{len(auto_confirmed)}** (attendu : 0 et 0 — la proposition exige forme + "
            "cotes à ±3/±5 cm d'un seul modèle ; une capacité identique ne suffit jamais).\n")
        # Exemples variés : chaque statut, avec / sans plaque, avec / sans modèle proposé.
        picks, seen = [], set()
        for pred in (lambda t: t.validation_status == "VALIDEE" and t.capacity_plate_l and t.tank_model_id,
                     lambda t: t.validation_status == "VALIDEE" and t.capacity_plate_l and not t.tank_model_id,
                     lambda t: t.validation_status == "VALIDEE" and not t.capacity_plate_l and t.tank_model_id,
                     lambda t: t.validation_status == "VALIDEE" and not t.capacity_plate_l and not t.tank_model_id,
                     lambda t: t.validation_status == "PROPOSEE" and t.tank_model_id,
                     lambda t: t.validation_status == "PROPOSEE" and not t.tank_model_id,
                     lambda t: t.validation_status == "A_CONFIRMER",
                     lambda t: t.validation_status == "PLUSIEURS_CUVES_A_IDENTIFIER",
                     lambda t: t.validation_status == "INCOMPLETE",
                     lambda t: t.validation_status == "REJETEE"):
            t = next((x for x in sorted(tanks, key=lambda x: x.site_id) if pred(x) and x.pk not in seen), None)
            if t:
                picks.append(t)
                seen.add(t.pk)
        ex = picks[:10]
        r.table(["Site", "Cuve", "Capacité (L)", "Plaque (L)", "Forme", "Cotes (cm)", "Modèle", "Source", "Statut", "Motif"],
                [[t.site_id, t.tank_id, _f(t.capacity_l, 0), _f(t.capacity_plate_l, 0), t.shape_family or "—", _dims(t),
                  (f"{t.tank_model.tank_model_id} ({t.model_match_status})" if t.tank_model_id else "—"),
                  f"{t.source_type} · {t.source_file or ''} l.{t.source_row or '—'}", t.validation_status, t.validation_reason or ""]
                 for t in ex])

    # 3 ────────────────────────────────────────────────────────────────────────────────────────────
    def conversions(self, r):
        from fuel_tracking.models import FuelStockObservation as O, FuelTankMaster

        cm = O.objects.filter(level_unit="CM").exclude(source_type__in=("SNOWFLAKE", "CLOSING"))
        r.p(f"Relevés en cm : {cm.count()} — par statut de conversion : "
            f"{dict(Counter(cm.values_list('conversion_status', flat=True)))} ; par méthode : "
            f"{dict(Counter(cm.values_list('conversion_method', flat=True)))}.")
        validated_sites = set(FuelTankMaster.objects.filter(active=True, validation_status="VALIDEE").values_list("site_id", flat=True))
        no_tank = cm.exclude(site_id__in=validated_sites)
        r.p(f"Relevés cm SANS cuve validée : {no_tank.count()} → litres retenus NULL : {no_tank.filter(level_liters__isnull=True).count()}, "
            f"litres = 0 : {no_tank.filter(level_liters=0).count()}, litres non NULL : {no_tank.filter(level_liters__isnull=False).count()} (attendu 0).")
        over = [o for o in O.objects.exclude(source_type="SNOWFLAKE").only("issue_codes", "level_liters", "conversion_status").iterator()
                if "OVER_CAPACITY" in (o.issue_codes or []) or "LEVEL_ABOVE_HEIGHT" in (o.issue_codes or [])]
        r.p(f"Relevés au-dessus de capacité + 5 % / hauteur utile : {len(over)} → statut INVALID : "
            f"{sum(1 for o in over if o.conversion_status == 'INVALID')}, litres retenus non NULL : "
            f"{sum(1 for o in over if o.level_liters is not None)} (attendu 0).")
        r.p(f"Données source conservées : niveau brut renseigné sur {cm.exclude(level_raw__isnull=True).count()} / {cm.count()} relevés cm "
            "(level_raw + level_value + unité + lot et ligne d'import).")
        conv = cm.filter(volume_geometric_l__isnull=False)
        r.p(f"Traçabilité de la formule : règle de conversion renseignée sur {conv.exclude(conversion_rule__isnull=True).count()} / "
            f"{conv.count()} relevés convertis ; modèle de bibliothèque renseigné sur {conv.exclude(tank_model_ref__isnull=True).count()}.\n")
        tanks = {(t.site_id, t.tank_id): t for t in FuelTankMaster.objects.filter(active=True)}
        sample = list(cm.filter(conversion_method="CALIBRATED").order_by("-observation_datetime")[:6]) + \
            list(cm.filter(conversion_status="GEOMETRIC_ONLY").order_by("-observation_datetime")[:4])
        rows = []
        for o in sample:
            t = tanks.get((o.site_id, o.tank_id))
            rows.append([o.site_id, _d(o.observation_datetime), o.level_raw, o.tank_id or "—",
                         _f(t.capacity_plate_l if t else None, 0), (t.shape_family if t else "—"), (_dims(t) if t else "—"),
                         _f(o.volume_geometric_l), _f(o.volume_calibrated_l), _f(o.level_liters), o.conversion_status,
                         o.validation_status, o.conversion_rule or "—"])
        r.table(["Site", "Date", "Niveau brut", "Cuve", "Plaque (L)", "Forme", "Cotes", "Géom. (L)", "Calibré (L)", "Retenu (L)",
                 "Conversion", "Validation", "Règle appliquée"], rows)

    # 4 ────────────────────────────────────────────────────────────────────────────────────────────
    def retained(self, r):
        from fuel_tracking.models import FuelStockObservation as O
        from fuel_tracking.views_stock import build_registry_rows

        rows, k = build_registry_rows()
        ret = [x for x in rows if x["statut"] == "RETENU"]
        r.p(f"Tableau officiel : **{len(rows)} lignes pour {len({x['site_id'] for x in rows})} sites** (une ligne par site).")
        r.p(f"Sites avec stock retenu en litres : **{len(ret)} / {k['sites_total']}** — dénominateur affiché : « {k['denominateur']} ».")
        r.p(f"Stock retenu par source : {dict(Counter(x['source'] for x in ret))}. Stocks retenus venant de Snowflake : "
            f"**{sum(1 for x in ret if x['source'] == 'SNOWFLAKE')}** (attendu 0 : Snowflake et Closing sont exclus du calcul).")
        r.p("Le tableau technique Snowflake/ENOC est une section séparée, repliée par défaut "
            "(« Historique technique / sources non retenues », onglet Imports & paramètres).\n")
        obs = {o.pk: o for o in O.objects.filter(pk__in=[x["observation_id"] for x in ret[:10]])}
        r.table(["Site", "Stock (L)", "Source", "Date relevé", "Méthode", "Remplissage", "Pourquoi retenu"],
                [[x["site_id"], _f(x["stock_l"]), x["source"], _d(x["observation_datetime"]), x["conversion_method"],
                  (f"{x['fill_pct']} %" if x["fill_pct"] is not None else "—"),
                  (obs[x["observation_id"]].validation_reason if x["observation_id"] in obs else "") or
                  "Dernier relevé validé, converti avec une cuve validée"] for x in ret[:10]])

    # 5 ────────────────────────────────────────────────────────────────────────────────────────────
    def validations(self, r):
        from fuel_tracking.models import FuelMovement, FuelRegistryCorrection, FuelStockObservation as O, FuelTankMaster
        from fuel_tracking.services.stock_work_queue import counters
        from fuel_tracking.views_cph import VALIDATOR_ROLES

        r.p("Cas à traiter : Suivis Stock → onglet « À traiter » (compteurs cliquables ; chaque ligne : anomalie, valeur brute, "
            "attendu, règle échouée, action à faire, bouton d'action).")
        r.table(["Catégorie", "Nombre"], [[c["label"], c["count"]] for c in counters()])
        r.p(f"Rôles autorisés à valider (cuve, relevé, mouvement, classification ENOC) : {sorted(VALIDATOR_ROLES)} ; "
            "page Suivi Carburant ouverte aux rôles admin et analyst (un manager peut valider via l'API mais n'ouvre pas la page).")
        r.p(f"Validations manuelles enregistrées : cuves {FuelTankMaster.objects.filter(validated_by__isnull=False).count()}, "
            f"relevés {O.objects.filter(validated_by__isnull=False).count()}, mouvements {FuelMovement.objects.filter(validated_by__isnull=False).count()}. "
            "Chaque validation enregistre utilisateur, date, motif obligatoire ; la preuve (photo / ticket) est celle de la source, "
            "et une référence de preuve peut être saisie sur la cuve.")
        r.p(f"Journal des corrections : {FuelRegistryCorrection.objects.count()} entrées — par action : "
            f"{dict(Counter(FuelRegistryCorrection.objects.values_list('action', flat=True)))}.")
        r.p("Proposition de modèle : bouton « Confirmer le modèle » sur la cuve (une action, motif obligatoire) → applique forme, "
            "cotes, fonds et plaque du modèle, reconvertit les relevés du site ; aucune ressaisie.")

    # 6 ────────────────────────────────────────────────────────────────────────────────────────────
    def guard(self, r):
        from fuel_tracking.models import FuelGuardScope, FuelStockObservation as O
        from fuel_tracking.services import stock_registry as R

        g = O.objects.filter(source_type__in=("LOLIFORCE", "SCSS"))
        r.p(f"Relevés par source : {dict(Counter(g.values_list('source_type', flat=True)))} ; par statut gardiennage : "
            f"{dict(Counter(g.values_list('guard_scope_status', flat=True)))}.")
        scopes = list(FuelGuardScope.objects.all())
        prov = {}
        for s in scopes:
            prov.setdefault(s.site_id, set()).add(s.provider)
        r.p(f"Périmètres configurés : {dict(Counter(s.provider for s in scopes))} ; sites affectés aux DEUX prestataires : "
            f"{sum(1 for v in prov.values() if len(v) > 1)} (aucun site n'est affecté par défaut).")
        both = g.filter(level_unit="L", level_cm__isnull=False)
        r.p(f"Relevés avec cm ET litres déclarés : {both.count()} (les deux conservés ; écart calculé sur "
            f"{both.exclude(cm_liters_gap_l__isnull=True).count()}).")
        r.p("Règle de comparaison : même site (même cuve si plusieurs), relevés à moins de 48 h, en litres, capacité validée ; "
            "tolérance = max(100 L ; 10 % capacité) → COHÉRENT ou CONFLIT, jamais de moyenne.\n")
        sites = sorted(set(g.filter(source_type="LOLIFORCE").values_list("site_id", flat=True))
                       & set(g.filter(source_type="SCSS").values_list("site_id", flat=True)))
        comps = [c for s in sites for c in R.guard_comparisons(s)]
        r.p(f"Sites ayant des relevés des deux prestataires : {len(sites)} ; paires à moins de 48 h : {len(comps)} — "
            f"{dict(Counter(c['status'] for c in comps))}.")
        shown = [c for c in comps if c["status"] == "STOCK_CONFLICT"][:1] + [c for c in comps if c["status"] == "COHERENT"][:1] + comps[:2]
        r.table(["Site", "Loliforce", "SCSS", "Écart (L)", "Tolérance (L)", "Résultat"],
                [[c["site_id"], f"{_d(c['loliforce']['datetime'])} · {c['loliforce']['raw']} → {_f(c['loliforce']['level_l'])} L",
                  f"{_d(c['scss']['datetime'])} · {c['scss']['raw']} → {_f(c['scss']['level_l'])} L",
                  _f(c.get("ecart_l")), _f(c.get("tolerance_l")), f"{c['status']} {c.get('reason') or ''}"] for c in shown[:3]])

    # 7 ────────────────────────────────────────────────────────────────────────────────────────────
    def enoc(self, r):
        from fuel_tracking.models import FuelEnocMovement, FuelMovement
        from fuel_tracking.services.stock_enoc import enoc_type_dictionary

        d = enoc_type_dictionary()
        r.p(f"Mouvements ENOC bruts reçus : {FuelEnocMovement.objects.count()} ; types bruts distincts : {len(d)}.")
        r.table(["Type brut ENOC", "Lignes", "Statuts ENOC", "Classification appliquée", "Version", "Suggestion (non appliquée)", "Exemples"],
                [[x["raw_value"], x["count"], x["statuts"], x["categorie"], x["version"] or "—", x["suggestion"] or "—",
                  ", ".join(str(e) for e in x["exemples"])] for x in d])
        mv = FuelMovement.objects.filter(source_type="ENOC")
        r.p(f"Mouvements ENOC du registre par catégorie : {dict(Counter(mv.values_list('source_category', flat=True)))} ; "
            f"retenus dans le stock : {mv.filter(retained_in_stock=True).count()} ; demandes ou cartes Total retenues : "
            f"{mv.filter(retained_in_stock=True, source_category__in=['DEMANDE_LIVRAISON', 'CARTE_TOTAL']).count()} (attendu 0).")
        allmv = FuelMovement.objects.all()
        r.p(f"Tous mouvements (ENOC + historique refueling) par catégorie : {dict(Counter(allmv.values_list('source_category', flat=True)))}.\n")
        rows = []
        for cat in ("RELEVE_STOCK", "LIVRAISON_CONFIRMEE", "DEMANDE_LIVRAISON", "CARTE_TOTAL", "TRANSFERT_ENTRANT", "TRANSFERT_SORTANT",
                    "RETRAIT", "INCONNU"):
            m = allmv.filter(source_category=cat).order_by("-movement_datetime").first()
            if m:
                rows.append([cat, m.site_id, m.source_type, _d(m.movement_datetime), m.raw_operation_type or "—",
                             _f(m.requested_quantity_l, 0), _f(m.quantity_l, 0), m.status, m.retained_in_stock, m.mapping_rule or "—"])
        r.table(["Catégorie", "Site", "Source", "Date", "Type brut", "Demandé (L)", "Réalisé (L)", "Statut", "Retenu stock", "Règle"], rows)

    # 8 ────────────────────────────────────────────────────────────────────────────────────────────
    def energy(self, r):
        from fuel_tracking.services.site_energy_profile import UNKNOWN, audit_energy_fields, energy_profiles

        r.p("La typologie (colonne TYPOLOGIE = installed_typology / billing_typology de Gestion des sites) est affichée telle "
            "quelle et n'est jamais modifiée. Environnement, raccordement, GE et solaire sont des attributs séparés, "
            "alimentés uniquement par un champ source explicite ; sinon « Inconnu ». Aucun attribut n'est déduit du code de typologie.\n")
        audit = audit_energy_fields()
        if audit:
            keys = list(audit[0].keys())
            r.table(keys, [[a.get(k) for k in keys] for a in audit])
        prof = energy_profiles()
        n = len(prof) or 1
        rows = []
        for attr in ("environnement", "raccordement", "ge", "solaire", "profil"):
            c = Counter(p[attr] for p in prof.values())
            unknown = c.get(UNKNOWN, 0) + c.get("Incomplet / à confirmer", 0)
            rows.append([attr, dict(c.most_common(6)), f"{(n - unknown) / n * 100:.1f} %", unknown])
        r.table(["Attribut", "Valeurs", "Couverture", "Sites Inconnu / incomplets"], rows)
        r.p("Raison d'un « Inconnu » : la source explicite de l'attribut est vide pour ce site (aucune valeur de repli).")

    # 9 ────────────────────────────────────────────────────────────────────────────────────────────
    def e2e(self, r):
        from fuel_tracking.models import FuelMovement, FuelRegistryCorrection, FuelStockObservation as O
        from fuel_tracking.services import stock_registry as R

        def first_obs(pred, qs=None):
            for o in (qs if qs is not None else O.objects.exclude(source_type__in=("SNOWFLAKE", "CLOSING"))
                      .select_related("batch", "import_row").order_by("-observation_datetime")).iterator():
                if pred(o):
                    return o
            return None

        def trace(entity, pk, batch=None, row=None):
            n = FuelRegistryCorrection.objects.filter(entity=entity, entity_id=str(pk)).count()
            src = f"lot #{batch.pk} {batch.file_name}" if batch else ""
            if row is not None:
                src += f" · {row.sheet_name} l.{row.row_number}"
            return f"{src} · {n} entrée(s) d'historique".strip(" ·")

        cases = [
            ("Cuve validée + cm converti (relevé validé)", lambda o: o.level_unit == "CM" and o.conversion_method == "CALIBRATED"
             and o.validation_status == "VALIDATED" and "/" not in (o.level_raw or "")),
            ("Cuve validée + cm converti (en attente)", lambda o: o.level_unit == "CM" and o.conversion_method == "CALIBRATED"
             and o.validation_status != "VALIDATED"),
            ("Volume litres direct", lambda o: o.conversion_method == "DIRECT_LITERS"),
            ("Cuve validée sans plaque : géométrique seul", lambda o: o.level_unit == "CM" and o.conversion_status == "GEOMETRIC_ONLY"),
            ("Sans cuve validée", lambda o: o.level_unit == "CM" and o.conversion_status == "PENDING_TANK_MAPPING"
             and "NO_VALIDATED_TANK" in (o.issue_codes or [])),
            ("Dépassement capacité", lambda o: bool({"OVER_CAPACITY", "LEVEL_ABOVE_HEIGHT"} & set(o.issue_codes or []))),
            ("Site à plusieurs cuves", lambda o: bool({"MULTI_TANK_WITHOUT_ID", "TANK_UNKNOWN"} & set(o.issue_codes or []))),
            ("Relevé gardiennage dans le périmètre", lambda o: o.source_type in ("LOLIFORCE", "SCSS") and o.guard_scope_status == "IN_SCOPE"
             and o.validation_status == "VALIDATED" and "/" not in (o.level_raw or "")),
        ]
        rows = []
        for label, pred in cases:
            o = first_obs(pred)
            if o is None:
                rows.append([label, "—", "—", "—", "aucun cas dans la base", "—", "—", "—", "—"])
                continue
            rows.append([label, o.site_id, o.source_type, _d(o.observation_datetime), o.level_raw,
                         o.conversion_rule or o.conversion_message or "—", f"{_f(o.level_liters)} L ({o.conversion_status})",
                         f"{o.validation_status} · anomalies {o.issue_codes or []}", trace("OBSERVATION", o.pk, o.batch, o.import_row)])
        for label, cat in (("ENOC non classifié", "INCONNU"), ("Demande de livraison", "DEMANDE_LIVRAISON"), ("Carte Total", "CARTE_TOTAL")):
            m = FuelMovement.objects.filter(source_category=cat).select_related("batch").order_by("-movement_datetime").first()
            if m is None:
                rows.append([label, "—", "—", "—", "aucun cas dans la base", "—", "—", "—", "—"])
                continue
            rows.append([label, m.site_id, m.source_type, _d(m.movement_datetime),
                         f"{m.raw_operation_type or '—'} · demandé {_f(m.requested_quantity_l, 0)} L", m.mapping_rule or "—",
                         f"réalisé {_f(m.quantity_l, 0)} L · retenu stock = {m.retained_in_stock}", m.status,
                         trace("MOVEMENT", m.pk, m.batch)])
        g = O.objects.filter(source_type__in=("LOLIFORCE", "SCSS"))
        sites = sorted(set(g.filter(source_type="LOLIFORCE").values_list("site_id", flat=True))
                       & set(g.filter(source_type="SCSS").values_list("site_id", flat=True)))
        comp = next((c for s in sites for c in R.guard_comparisons(s)), None)
        if comp:
            rows.append(["Comparaison Loliforce / SCSS", comp["site_id"], "LOLIFORCE + SCSS", _d(comp["loliforce"]["datetime"]),
                         f"{comp['loliforce']['raw']} / {comp['scss']['raw']}", "≤ 48 h, tolérance max(100 L ; 10 % capacité)",
                         f"écart {_f(comp.get('ecart_l'))} L / tolérance {_f(comp.get('tolerance_l'))} L", comp["status"], comp.get("reason") or "—"])
        else:
            rows.append(["Comparaison Loliforce / SCSS", "—", "—", "—", f"aucun site avec relevés des deux prestataires à moins de 48 h "
                         f"({len(sites)} site(s) avec les deux sources)", "≤ 48 h, max(100 L ; 10 % capacité)", "—", "—", "test automatisé seulement"])
        r.table(["Cas", "Site", "Source", "Date", "Donnée brute", "Règle appliquée", "Résultat", "Statut", "Preuve / historique"], rows)
