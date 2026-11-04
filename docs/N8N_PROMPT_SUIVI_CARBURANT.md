# Prompt n8n — Automatisation « SUIVI CARBURANT » (étape 1 : Suivi Consommation)

> À coller tel quel dans l'assistant de création de workflows n8n. Les valeurs entre `<CHEVRONS>` sont à renseigner dans les *Credentials* / variables n8n, jamais en dur dans un workflow.

---

## 1. Rôle et objectif

Tu es un expert n8n, data-engineering et exploitation de plateformes carburant pour sites télécom. Construis des workflows n8n **robustes, idempotents et surveillés** qui alimentent et pilotent automatiquement le module **« Suivi Carburant » de la plateforme eGrid/EnerTrack** (backend Django/DRF, PostgreSQL, Celery ; frontend React).
L'objectif final est un outil **utilisable en continu, sans action manuelle répétitive**. On avance **étape par étape** :

| Étape | Partie | Statut |
|---|---|---|
| **1** | **Suivi Consommation** (Closing, relevés gardiens, ENOC, Snowflake → variation de stock, écart vs Closing, explication) | **À faire maintenant** |
| 2 | Suivi Stock (registre, cuves, relevés, certification) | plus tard |
| 3 | Commandes | plus tard |
| 4 | Estimation commande / prédiction | plus tard |

**Ne construis QUE l'étape 1.** Structure les workflows pour que les étapes suivantes se branchent sans refonte (sous-workflows réutilisables : *auth*, *dépôt de fichier*, *appel API*, *alerte*, *journal*).

## 2. Règles non négociables

1. **Snowflake = lecture seule.** Aucun INSERT/UPDATE/DELETE/DROP/CREATE. Si tu interroges Snowflake dans n8n, utilise un rôle dédié en SELECT uniquement et des requêtes bornées par date (économie de crédits). Les identifiants vivent dans les Credentials n8n.
2. **ENOC est lu, jamais modifié.**
3. **Aucune donnée inventée, aucun calcul simulé, aucun repli silencieux.** Une donnée absente reste `null` / « non calculable ». `null` ≠ `0` ≠ `estimé`. Un stock de 0 L est une valeur valide.
4. **Ne recalcule pas les consommations dans n8n.** Le moteur de calcul est dans la plateforme (règle bilan-v2). n8n **collecte, dépose, déclenche, vérifie, alerte, archive**. Il ne réimplémente ni la variation de stock, ni l'écart Closing, ni le CPH.
5. **Idempotence** : relancer un workflow ne doit jamais dupliquer des données. La plateforme dédoublonne par empreinte SHA-256 ; conserve toi aussi l'empreinte/nom/date de chaque fichier traité et saute les doublons.
6. **Aucun secret dans les workflows, logs, messages ou notifications.**
7. Une estimation n'est jamais présentée comme certifiée. La « Consommation réelle certifiée » reste vide tant que le Fuel Manager n'a pas certifié (action humaine, **jamais automatisée**).
8. Un site sans correspondance dans le fichier Closing est **affiché mais exclu des totaux** (comportement de la plateforme — ne le contourne pas).

## 3. Ce que la plateforme fait DÉJÀ seule (ne pas dupliquer)

Tâches Celery planifiées côté serveur :
- Synchro **Snowflake** consommation mesurée : toutes les 5 min (mois courant) ; faits journaliers (CPH/instrumentée) : toutes les heures + rattrapage nocturne J-35.
- Synchro **ENOC** (mouvements Truck, Carte Total, Ponction) : toutes les 5 min.
- Traitement des fichiers déposés dans `incoming/` : toutes les 15 min (`:07 :22 :37 :52`).
- **Calcul automatique des bilans des 2 derniers mois complets : chaque nuit à 03h11** (nouvelles données prises en compte seules).
- Contrôle santé CPH quotidien.

➡ Le rôle de n8n est donc **(a) amener les fichiers manquants, (b) surveiller que tout est frais, (c) forcer un recalcul après un import, (d) produire les rapports et alertes**.

## 4. Données et sources (étape 1)

| Source | Contenu | Fréquence | Où la déposer |
|---|---|---|---|
| **Fichier Closing** (Excel, envoyé à la Finance, stocks du mois précédent) | Stock initial/final, livraisons, mouvements, consommation déclarée par site. **C'est la base de référence : mêmes sites que ce fichier.** | mensuelle | `<FUEL_IMPORT_ROOT>/incoming/closing/` |
| **Relevés gardiens** Loliforce | Niveaux de cuve (cm ou L), **cumulés depuis janvier**, envoyés **toutes les 2 semaines** | bimensuelle | `<FUEL_IMPORT_ROOT>/incoming/loliforce/` |
| Relevés gardiens SCSS | idem | bimensuelle | `<FUEL_IMPORT_ROOT>/incoming/scss/` |
| Target fuel (Excel) | cible de consommation par site | à la demande | `<FUEL_IMPORT_ROOT>/incoming/target_fuel/` |
| **ENOC** | mouvements de carburant | continu | synchronisé par la plateforme |
| **Snowflake** | consommation instrumentée, faits journaliers (runtime GE, puissances) | continu | synchronisé par la plateforme |

Formats acceptés : `.xlsx`, `.xlsm`, `.xlsb`. Les fichiers gardiens étant **cumulés depuis janvier**, un nouveau fichier contient les anciennes lignes : c'est normal, la plateforme dédoublonne ligne par ligne. **Ne jamais modifier ni renommer le contenu d'un fichier original.** Ajoute un horodatage au nom seulement si le nom existe déjà.

⚠️ Je ne sais pas par quel canal arrivent les fichiers (e-mail, Google Drive, SFTP, dossier partagé). **Pose-moi la question avant de figer les nœuds d'entrée**, et prévois ces entrées sous forme de déclencheurs interchangeables (Gmail/IMAP, Google Drive, SFTP, Webhook).

## 4 bis. Dossier monté = point d'entrée unique

Un seul dossier partagé (`/mnt/Suivi_carburant_prod` sur l'hôte, monté dans les conteneurs backend/Celery) sert d'interface. **n8n surveille et alimente ce dossier ; la plateforme le traite toute seule toutes les 15 min.**

```
Suivi_carburant_prod/
  incoming/closing/  incoming/loliforce/  incoming/scss/  incoming/target_fuel/  incoming/modeles_cuves/   ← dépôt (n8n ou humain)
  processing/   accepted/   rejected/   archive/AAAA-MM/                                                  ← gérés par la plateforme
```
- Un fichier déposé dans `incoming/<source>/` est traité puis **déplacé** vers `accepted/` ou `rejected/` (avec rapport CSV) ; l'original est archivé avec son empreinte. Ne jamais écrire dans `processing/`, `accepted/`, `rejected/`, `archive/`.
- n8n peut donc utiliser le déclencheur **Local File Trigger** (ou SFTP/Drive) sur les dossiers sources, et un second déclencheur sur `accepted/` / `rejected/` pour savoir quand le traitement est terminé et lancer WF-04 / alerter avec le rapport.
- Si n8n tourne dans Docker, monter le même chemin hôte dans le conteneur n8n (`/mnt/suivi_carburant_prod:/files/suivi_carburant`) ; n8n n'a besoin d'écrire que dans `incoming/`.

## 5. Accès à la plateforme

- Base : `<EGRID_API_URL>` (se termine par `/api`).
- **Auth JWT** : `POST /auth/login/` `{username, password}` → `{access, refresh}` ; `POST /auth/refresh/` pour renouveler. Utilise un compte de service dédié (rôle autorisé à valider/calculer) stocké dans les Credentials n8n. Gère l'expiration (refresh automatique, une seule reprise).
- Dépôt de fichiers : le volume partagé `<FUEL_IMPORT_ROOT>` (nœud SFTP / SSH / Read-Write Files selon l'infrastructure). Vérifier qu'un fichier est bien arrivé avant de le déclarer traité.

Endpoints utiles (préfixe `/fuel-tracking/`) :

| Besoin | Appel |
|---|---|
| Santé des sources (fraîcheur Closing/gardiens/ENOC/Snowflake/target, relevés en retard, sites sans relevé) | `GET consommations/sante/` |
| Périodes disposant d'un bilan | `GET consommations/periodes/` |
| Tableau Suivis Consommations + KPI + fraîcheur (`date_debut`, `date_fin`, filtres, `page`, `page_size`) | `GET consommations/` |
| Évolution par période | `GET consommations/evolution/` |
| Lots d'import et statut | `GET stock/imports/` ; rapport CSV : `GET stock/imports/<id>/rapport/` ; lignes : `GET stock/imports/<id>/lignes/` |
| Analyse d'un fichier **sans import** (multipart `file`, `source_type`) | `POST stock/imports/analyser/` |
| Forcer le calcul d'une période (`debut`, `fin`, optionnel `site`) | `POST stock/bilans/calculer/` |
| Exports CSV (`controle_complet`, `anomalies_fuel`, `priorites_donnees`, `rapport_imports`) | `GET consommations/export/<kind>/?date_debut=&date_fin=` |

Si un endpoint renvoie 4xx/5xx : **ne continue pas comme si de rien n'était** → journalise, alerte, arrête la branche.

## 6. Workflows à construire (étape 1)

**WF-00 — Sous-workflows communs** : `auth_egrid` (login + refresh, renvoie le token), `deposer_fichier` (calcule SHA-256, vérifie le doublon dans le journal, dépose, vérifie la présence), `alerter` (e-mail et/ou Slack/Teams/WhatsApp selon `<CANAL_ALERTE>`, message court sans secret), `journal` (table/Google Sheet/Data Table n8n : date, workflow, fichier, empreinte, résultat, détail).

**WF-01 — Fichier Closing mensuel**
Déclencheur : arrivée du fichier (canal à confirmer) + cron de rattrapage les 1er–7 de chaque mois. Étapes : récupérer la pièce jointe/fichier → n'accepter que `.xlsx/.xlsm/.xlsb` → `POST stock/imports/analyser/` (source `CLOSING`) → si illisible : alerte + stop → déposer dans `incoming/closing/` → attendre le prochain cycle (≤15 min) puis lire `GET stock/imports/` : le lot doit être `ACCEPTED` (ou `VALIDATION_REQUIRED` → alerte « validation humaine requise ») ; `REJECTED` → alerte avec le rapport CSV. Après succès : déclencher WF-04.

**WF-02 — Relevés gardiens (toutes les 2 semaines)**
Mêmes étapes pour Loliforce → `incoming/loliforce/` et SCSS → `incoming/scss/`. Rappeler que les fichiers sont cumulés depuis janvier (pas de découpage, pas de filtrage). Si aucun fichier n'est reçu **J+16** après le précédent : alerte « relevés gardiens en retard » avec les sites listés par `consommations/sante/` (`releves_en_retard`, `sites_sans_releve`).

**WF-03 — Contrôle de fraîcheur (toutes les heures)**
`GET consommations/sante/` et `GET consommations/` (bloc `fraicheur`). Alerter (anti-spam : 1 alerte par cause et par 24 h, message de résolution quand ça revient) si : Snowflake sans journée récente > 3 j, synchro en échec, ENOC sans synchro récente, aucun Closing importé, target fuel non importé, période incomplète. **Ne corrige rien** : signale.

**WF-04 — Recalcul après import + contrôle qualité**
Déclencheur : fin d'un import accepté (WF-01/02) ou cron quotidien 04h00 (après le calcul nocturne de 03h11). Étapes : `GET consommations/periodes/` → pour la dernière période complète : `POST stock/bilans/calculer/` (si le calcul nocturne n'a pas déjà couvert les nouvelles données) → `GET consommations/` et vérifier les invariants : nombre de sites calculables > 0, `kpis.sans_correspondance` raisonnable, pas de `variation.negatives` en hausse brutale. Écart anormal → alerte (ex. variation négative, stock identique suspect, écart Closing extrême) en s'appuyant sur `kpis.alertes`.

**WF-05 — Rapport hebdomadaire / mensuel**
Chaque lundi 07h00 (et le 3 de chaque mois pour M-1) : télécharger `controle_complet`, `anomalies_fuel`, `priorites_donnees` pour la dernière période complète ; les archiver (Drive/SharePoint, nom `AAAA-MM_<type>.csv`) ; envoyer un résumé : KPIs (variation de stock, Closing, instrumentée, CPH complète), nombre de sites par statut de bilan, sites hors Closing, top 10 des écarts vs Closing, sites sans relevé gardien. **Chiffres repris tels que renvoyés par l'API, avec `—` pour les valeurs absentes.**

**WF-06 — Surveillance des workflows** : Error Trigger global → `alerter` + `journal`. Chaque workflow : 3 tentatives espacées (backoff 1/5/15 min) sur les erreurs réseau, **jamais** sur une erreur de validation de fichier.

## 7. Exigences de qualité

- Tous les nœuds nommés en français, un commentaire par bloc, variables et credentials centralisés.
- Fuseau **Africa/Dakar (UTC+0)** pour les crons et les dates ; périodes en jours entiers `00:00:00 → 23:59:59`.
- Aucun traitement en masse sans pagination (`page_size` ≤ 200).
- Chaque workflow est livré avec : schéma, déclencheur, entrées/sorties, cas d'erreur, et **jeu de tests** : (1) fichier valide, (2) doublon, (3) fichier illisible, (4) fichier sans les feuilles attendues, (5) API indisponible, (6) token expiré.
- Livre d'abord **WF-00, WF-03, WF-01**, fais-moi valider, puis WF-02, WF-04, WF-05, WF-06.

## 8. Ce que tu me renvoies

1. Les questions bloquantes (canal d'arrivée des fichiers, accès au volume `incoming/`, canal d'alerte, compte de service).
2. Les workflows exportés (JSON n8n) un par un, avec leur mode d'emploi.
3. La liste des Credentials à créer (noms uniquement, aucune valeur).
4. Un récapitulatif de ce qui reste manuel (certification Fuel Manager, import du target fuel, arbitrage des conflits ENOC/Closing) et de ce qui est automatisé.
