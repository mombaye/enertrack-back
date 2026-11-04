# Prompt n8n — Automatisation « SUIVI CARBURANT » · Étape 1 : Suivi Consommation

> **Mode d'emploi** : colle tout ce document dans l'assistant de création de workflows n8n. Il est **complet** : n8n doit produire les workflows, les exécuter, et **afficher les sorties** (résultats de chaque étape + rapport final). Les valeurs `<ENTRE_CHEVRONS>` se renseignent dans les *Credentials* / *Variables* n8n, jamais en dur.

---

## 0. Ce que tu dois livrer (et afficher)

Construis, active et teste **3 workflows** + 1 sous-workflow, puis **affiche pour chacun** : le schéma des nœuds, la sortie (JSON) de chaque nœud clé, et le **RAPPORT FINAL** défini au §7.

| # | Workflow | Déclencheur | Rôle |
|---|---|---|---|
| 0 | `SC_00_auth_egrid` (sous-workflow) | appelé | obtient/renouvelle le jeton JWT |
| 1 | `SC_01_import_fichiers` | **fichier déposé** dans un dossier + cron de secours */10 min | traite les fichiers, affiche le résultat par fichier |
| 2 | `SC_02_controle_et_calcul` | fin de SC_01 + cron 04h00 | vérifie la fraîcheur, recalcule, vérifie les KPI |
| 3 | `SC_03_rapport` | cron lundi 07h00 + bouton manuel | rapport hebdo + page HTML des dernières sorties |

On avance **étape par étape** : ne construis **que** l'étape 1 (Suivi Consommation). Étapes futures (ne pas faire maintenant) : 2 Suivi Stock · 3 Commandes · 4 Estimation. Garde des sous-workflows réutilisables (auth, alerte, journal) pour les brancher plus tard.

## 1. Règles non négociables

1. **Snowflake en lecture seule.** n8n n'interroge **pas** Snowflake : la plateforme le synchronise déjà (toutes les 5 min / 1 h). n8n vérifie seulement la fraîcheur via l'API.
2. **ENOC lu, jamais modifié.**
3. **Aucune donnée inventée, aucun calcul simulé, aucun repli silencieux.** Valeur absente → afficher `—` (jamais `0`). `null` ≠ `0` ≠ `estimé`.
4. **Ne recalcule rien dans n8n.** Les chiffres affichés sont ceux renvoyés par l'API, tels quels.
5. **Idempotence** : relancer ne duplique rien (la plateforme dédoublonne par empreinte SHA-256 et ligne par ligne). Tiens quand même un journal.
6. **Aucun secret** dans les workflows, logs, messages. La certification « réelle » par le Fuel Manager reste **humaine**.
7. Un site absent du fichier Closing est **affiché mais exclu des totaux** (comportement de la plateforme).
8. **Erreur API ou fichier illisible → ne continue pas comme si de rien n'était** : alerte + arrêt de la branche + ligne dans le rapport.

## 2. Dossier partagé (point d'entrée unique)

Dossier monté (hôte) : `/mnt/suivi_carburant_prod` — vu par la plateforme comme `/mnt/Suivi_carburant_prod`. Si n8n est dans Docker : ajouter le volume `/mnt/suivi_carburant_prod:/files/suivi_carburant` au conteneur n8n (chemin n8n : `/files/suivi_carburant`).

```
incoming/closing/        ← fichier Closing mensuel  (ex. « Esco Senegal - Synthèse Conso Fuel Septembre 2026 Final v1.xlsb »)
incoming/loliforce/      ← relevés Loliforce CUMULÉS  (ex. « Template remontées Loliforce Camusat 2026 v03112026.xlsx »)
incoming/scss/           ← relevés SCSS HEBDOMADAIRES (ex. « REMONTEES DE CARBURANT DU 26 OCTOBRE AU 01 NOVEMBRE 2026.xlsx »)
incoming/target_fuel/    ← target fuel (à la demande)
processing/ accepted/ rejected/ archive/AAAA-MM/   ← gérés par la plateforme : n8n ne les modifie JAMAIS
```
Formats : `.xlsx`, `.xlsm`, `.xlsb`. **La source se déduit du dossier, la période du contenu — jamais du nom du fichier.** n8n n'écrit rien dans ce dossier : c'est l'utilisateur qui y dépose les fichiers.

### Règle « dernières données »
- **Loliforce = CUMULÉ** (11 feuilles par zone, blocs « SEMAINE n », ~13 000 lignes depuis février) → seule la **version la plus récente** compte (date `vJJMMAAAA` du nom, sinon date de modification). Si plusieurs fichiers Loliforce sont présents, n8n **affiche un avertissement** « plusieurs versions cumulées » ; il ne les supprime pas.
- **SCSS = HEBDOMADAIRE, non cumulé** → chaque nouveau fichier est traité.
- **Closing = mensuel** → le plus récent par mois.

## 3. Accès API

- Base : `<EGRID_API_URL>` (se termine par `/api`, ex. `https://egrid.exemple.sn/api`).
- **Connexion** : `POST {BASE}/auth/login/` JSON `{"username":"<SERVICE_USER>","password":"<SERVICE_PASSWORD>"}` → `{"access":"…","refresh":"…"}`. Compte de service **admin ou manager** (les imports/calculs sont réservés à ces rôles).
- Ensuite : header `Authorization: Bearer <access>`. Si `401` → `POST {BASE}/auth/refresh/` `{"refresh":"…"}` → `{"access":"…"}` puis **une seule** reprise ; sinon alerte « authentification impossible ».
- Préfixe métier : `{BASE}/fuel-tracking/`.

| Besoin | Appel | Champs à utiliser dans la réponse |
|---|---|---|
| **Traiter le dossier maintenant** | `POST stock/imports/traiter-dossier/` (corps vide) | `nombre_traites`, `statuts`, `restant_dans_incoming`, `traites[]` : `file_name`, `source_type`, `status`, `status_message`, `resume.{lignes_lues, acceptees, rejetees, doublons, a_valider, taux_rejet_pct, periode, motifs_rejet}`, `summary.dates_impossibles`, `rapport_url` |
| Rapport CSV d'un lot | `GET stock/imports/{id}/rapport/` | CSV (lignes rejetées + motifs) |
| Historique des lots | `GET stock/imports/?source_type=&statut=&page=` | `data[]`, `pagination` |
| Santé des sources | `GET consommations/sante/` | `sources.{closing, gardiennage, enoc, snowflake, target_fuel}`, `couverture.{sites_base_closing, sites_releve_gardien_recent, seuil_jours, sites_releve_en_retard, sites_sans_releve_gardien, sites_enoc_hors_base_closing}`, `releves_en_retard[]`, `sites_sans_releve[]` |
| Périodes avec bilan | `GET consommations/periodes/` | `periodes[]` (`debut`, `fin`, `sites`, `calculables`), `defaut` |
| Calcul d'une période | `POST stock/bilans/calculer/` JSON `{"debut":"AAAA-MM-JJ","fin":"AAAA-MM-JJ"}` | `sites`, `calculables`, `incomplets` |
| Tableau + KPI | `GET consommations/?date_debut=&date_fin=&page=1&page_size=200` | `periode`, `fraicheur`, `kpis`, `rows[]`, `total` |
| Évolution | `GET consommations/evolution/` | `periodes[]` (`variation_l`, `closing_l`) |
| Exports CSV | `GET consommations/export/{controle_complet\|anomalies_fuel\|priorites_donnees\|rapport_imports}/?date_debut=&date_fin=` | CSV (en-tête `Content-Disposition`, `X-Row-Count`) |

`kpis` utiles : `variation.{total_l, sites, negatives}`, `closing.{total_l, sites}`, `instrumentee.{total_l, sites}`, `cph_complete.{total_l, sites}`, `bilans.{prets, avec_reserve, variation_calculee, non_calculables, certifies}`, `sans_correspondance.{sites, base_closing_sites}`, `alertes.*`. Chaque `rows[]` : `site_id`, `site_name`, `stock.{variation_l, statut}`, `closing.{conso_l, ecart_l, ecart_pct, explication}`, `correspondance.utilise`.

## 4. SC_00_auth_egrid (sous-workflow)

Entrée : rien. Sortie : `{ "token": "<access>", "base": "<EGRID_API_URL>" }`. Nœuds : `Execute Workflow Trigger` → `HTTP Request` (login) → `Set` (token). Met le jeton en cache 20 min (variable statique du workflow). **Ne jamais écrire le jeton ni le mot de passe dans une sortie affichée** (masquer : `token: "***"` dans les rapports).

## 5. SC_01_import_fichiers

**Déclencheurs** : (a) `Local File Trigger` (ou SFTP/Drive Trigger selon l'accès) sur `incoming/closing`, `incoming/loliforce`, `incoming/scss`, `incoming/target_fuel` — événement « fichier ajouté », attendre 10 s que l'écriture soit finie (taille stable 2 lectures) ; (b) `Schedule` toutes les 10 min (secours si le dossier n'émet pas d'événement).

**Nœuds, dans l'ordre :**
1. **Lister** les fichiers de chaque sous-dossier (`Read/Write Files from Disk`, motif `*.xlsx;*.xlsm;*.xlsb`, ignorer `~$*` et `.*`). Si aucun fichier → fin silencieuse (aucune alerte).
2. **Règle « dernières données »** (`Code`) : par dossier, regrouper ; pour `loliforce` garder la version la plus récente (parse `v(\d{2})(\d{2})(\d{4})` sinon date de modification) et émettre un warning `PLUSIEURS_VERSIONS_CUMULEES` s'il y en a plusieurs ; pour `scss` garder tous ; pour `closing` le plus récent par mois.
3. **Contrôle préalable** : fichier de taille 0, extension inconnue, ou archive Excel illisible → alerte « fichier illisible/tronqué : <nom> » et **ne pas appeler l'API** pour ce fichier.
4. **Appel** `SC_00_auth_egrid` puis `POST stock/imports/traiter-dossier/` (timeout 300 s, **1 seul appel** même s'il y a plusieurs fichiers). Statut `503` → alerte « dossier d'import indisponible (montage ?) » ; `403` → alerte « compte de service sans droit admin/manager ».
5. **Pour chaque élément de `traites[]`** (`Split Out`) construire une ligne de sortie :
   `fichier | source | statut | lues | acceptées | rejetées | doublons | taux_rejet_% | période | motifs_rejet | dates_impossibles | rapport`.
6. **Règles d'alerte** (`IF`/`Code`) :
   - `status = REJECTED` → 🔴 « fichier rejeté » + lien du rapport CSV (télécharger `rapport_url` et joindre).
   - `status = VALIDATION_REQUIRED` → 🟠 « validation humaine requise ».
   - `taux_rejet_pct > 15` → 🟠 avec top 5 `motifs_rejet`.
   - `summary.dates_impossibles` non vide → 🟠 liste (feuille, ligne, site, date).
   - `motifs_rejet` contient `UNKNOWN_UNIT` / `MISSING_LEVEL` / `MISSING_DATE` → 🟠 consigne : « demander au gardien de corriger (date au format date, cm numérique, une seule unité par ligne) ».
   - doublon (`duplicate_of` non nul, ou tout en `rows_duplicate`) → ℹ️ « déjà reçu », **pas d'alerte**.
7. **Journal** : une ligne par fichier (date, source, nom, empreinte `file_hash`, statut, compteurs).
8. **Sortie du workflow** (affichée) : tableau des lignes du point 5 + liste des alertes + compteur `restant_dans_incoming` (doit être 0). Puis déclenche `SC_02`.

**Attendu avec les fichiers de test fournis** (à vérifier, en base réelle avec les sites connus) :
- `Template remontées Loliforce … v03112026.xlsx` → source `LOLIFORCE`, 11 feuilles retenues, période `2026-02-02 → 2026-10-30`, ~13 231 lignes lues, ~219 sites ; **attendu** : rejets pour `MISSING_LEVEL` (~1 598), `MISSING_DATE` (~344), `UNKNOWN_UNIT` (~93), `FUTURE_DATE` (1 : 14/06/2027) → alerte 🟠.
- `REMONTEES DE CARBURANT DU 26 OCTOBRE AU 01 NOVEMBRE 2026.xlsx` → source `SCSS`, 22 lignes, période `2026-10-30 → 2026-11-01` ; **attendu** : 1 `MISSING_DATE` (date texte `30/10:2026`) et 1 `MISSING_LEVEL` (THS_1690) → alerte 🟠.
- Fichier Closing `.xlsb` (34 feuilles, feuille de travail « Synthese conso fuel », contient aussi « Relevés Sociétés de Gardiennage ») → si le fichier est **tronqué** (archive incomplète), SC_01 doit lever 🔴 « fichier illisible » et demander de le renvoyer.

## 6. SC_02_controle_et_calcul

**Déclencheurs** : fin de `SC_01` (si au moins un lot `ACCEPTED`) + `Schedule` 04h00 (Africa/Dakar). Étapes :
1. **Santé** : `GET consommations/sante/`. Alertes (1 par cause / 24 h, message « résolu » au retour à la normale) :
   - `sources.closing = null` → 🔴 « aucun Closing importé » ;
   - `sources.snowflake.age_jours > 3` ou `derniere_journee = null` → 🔴 Snowflake en retard ;
   - `sources.enoc.statut` ≠ succès ou aucune synchro récente → 🔴 ENOC ;
   - `sources.target_fuel = null` → 🟠 « target fuel non importé » ;
   - `couverture.sites_releve_en_retard + sites_sans_releve_gardien > 0` → 🟠 avec la liste `releves_en_retard` (site, jours) et `sites_sans_releve` (top 30).
2. **Période** : `GET consommations/periodes/` → prendre `defaut`.
3. **Calcul** : `POST stock/bilans/calculer/` pour cette période (la plateforme le fait aussi chaque nuit à 03h11 : ne pas répéter si `periodes[].calculables` est déjà > 0 **et** qu'aucun lot n'a été accepté depuis).
4. **Contrôle qualité** : `GET consommations/?date_debut=…&date_fin=…&page_size=200` → vérifier `total > 0`, `kpis.variation.sites > 0`, `fraicheur.perime = false`, `fraicheur.periode_incomplete = false`. Sinon 🟠/🔴 avec `fraicheur.erreur_synchro`.
5. **Sortie (affichée)** : KPI de la période (variation de stock, Closing, instrumentée, CPH complète, bilans par statut, sites hors Closing) + top 10 des plus gros écarts `closing.ecart_l` (valeur absolue, hors `null`) avec `closing.explication.items[].label`.

## 7. SC_03_rapport — RAPPORT FINAL (à afficher à chaque exécution)

**Déclencheurs** : `Schedule` lundi 07h00, le 3 de chaque mois (M-1), et `Webhook` GET `/webhook/suivi-carburant/rapport` (bouton manuel). Produit :

**A. JSON de sortie (visible dans n8n)**
```json
{
  "genere_le": "AAAA-MM-JJ hh:mm",
  "periode": {"debut": "…", "fin": "…", "jours": 31},
  "fichiers_traites": [{"fichier":"…","source":"…","statut":"…","lues":0,"acceptees":0,"rejetees":0,"taux_rejet_pct":0,"periode":["…","…"]}],
  "sante": {"closing":"…","dernier_releve_gardien":"…","snowflake_derniere_journee":"…","enoc_derniere_synchro":"…","target_fuel":"…"},
  "kpis": {"variation_stock_l":null,"closing_l":null,"instrumentee_l":null,"cph_complete_l":null,
           "sites_par_statut":{"prets":0,"avec_reserve":0,"variation_a_controler":0,"non_calculables":0,"certifies":0},
           "sites_hors_closing":0},
  "top_ecarts_vs_closing": [{"site_id":"…","ecart_l":0,"cause_principale":"…"}],
  "releves_gardiens_en_retard": [{"site_id":"…","jours":0}],
  "alertes": [{"niveau":"ROUGE|ORANGE|INFO","code":"…","message":"…"}]
}
```
(valeur absente = `null`, affichée `—`.)

**B. Page HTML** (réponse du `Webhook`, `Respond to Webhook` en `text/html`) : titre « Suivi Consommation — <période> », 4 cartes (Variation de stock, Closing, Instrumentée, CPH complète ; unité L, `—` si null), tableau des fichiers traités (couleur : vert accepté, orange avec rejets, rouge rejeté), tableau « Top 10 écarts vs Closing », liste des alertes triées (rouge → orange → info), bandeau « Dernière mise à jour ». Aucune donnée secrète.

**C. Notification** (e-mail et/ou Slack/Teams, `<CANAL_ALERTE>`) : résumé en 10 lignes + lien vers la page HTML + pièces jointes `controle_complet`, `anomalies_fuel`, `priorites_donnees` (CSV, nommés `AAAA-MM_<type>.csv`) archivées dans `<DOSSIER_ARCHIVE_RAPPORTS>`.

## 8. Qualité et tests (à exécuter et afficher)

- Nœuds nommés en français, un commentaire par bloc, **Error Trigger** global → alerte + journal. Nouvel essai 3× (1/5/15 min) sur erreur réseau, **jamais** sur une erreur de validation de fichier.
- Fuseau `Africa/Dakar`. Pagination ≤ 200. Anti-spam : 1 alerte par cause et par 24 h.
- **Jeu de tests à lancer et à afficher (✔/✘ + sortie)** : (1) dossier vide → aucune alerte ; (2) fichier SCSS valide ; (3) même fichier redéposé → « déjà reçu » ; (4) fichier Loliforce cumulé → alerte 🟠 rejets ; (5) fichier tronqué → 🔴 ; (6) API indisponible (`503`) ; (7) jeton expiré → refresh puis succès ; (8) deux versions Loliforce → warning `PLUSIEURS_VERSIONS_CUMULEES`.

## 9. Ordre de livraison et questions

1. **Pose d'abord ces questions** (une seule fois) : accès au dossier partagé par n8n (volume Docker ou SFTP ?) ; canal d'alerte ; URL de l'API ; compte de service créé (admin/manager) ?
2. Livre dans l'ordre : `SC_00` → `SC_01` (+ tests 1–5) → `SC_02` → `SC_03`. À chaque livraison : JSON exporté du workflow, schéma, **sorties affichées**, liste des Credentials (noms seulement).
3. Termine par un tableau « ce qui est automatisé / ce qui reste humain » (certification Fuel Manager, arbitrage conflits ENOC/Closing, renvoi des fichiers corrigés par les gardiens).
