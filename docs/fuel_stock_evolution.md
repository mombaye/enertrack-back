# Évolution mensuelle du stock (Suivi Stock)

API : `GET /api/fuel-tracking/bi/stock/evolution/?zone=&mois=12` (3 à 24 mois, défaut 12 ; `mois` invalide → 400).

Règle (services/stock_history.py) — stock de fin de mois d'un site = son relevé VALIDE le plus récent **daté dans le mois** :
- relevés terrain / gardiennage / ENOC retenus par le registre à la fin du mois ;
- stock final VALIDÉ de la Synthèse (Closing) — marqué estimé si son origine est « Estimation » ;
- capteur Snowflake : snapshot mensuel archivé (`sync_fuel_stock --month`) ; pour le mois en cours, snapshot courant.

Même jour : terrain > capteur > Synthèse. Un site sans relevé dans le mois n'est **pas compté** (ni 0 L, ni report du mois
précédent). Chaque mois expose `nb_sites_releves` / `nb_sites_ge`, et `remplissage_pct` = stock ÷ capacité des seuls sites
relevés avec capacité connue — c'est l'indicateur comparable d'un mois à l'autre ; le total en litres dépend du nombre de sites relevés.

Limite : l'historique capteur n'existe que pour les mois archivés par `sync_fuel_stock --month` (commande non planifiée à ce jour).
Résultat mis en cache 15 min.
