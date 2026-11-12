# Données par site — module Carburant

Écran Suivi Carburant → Données par site : une ligne par site GE pour un mois.

| Méthode | Adresse | Rôle |
| --- | --- | --- |
| GET | `/api/fuel-tracking/bi/sites-table/` | KPI + lignes paginées (`page`, `page_size` ≤ 200), tri serveur (`sort`, `desc`) |
| GET | `/api/fuel-tracking/bi/export/sites/` | classeur Excel, toutes les lignes, mêmes filtres |

Filtres : `month` (AAAA-MM, défaut : dernier mois complet), `zone`, `q` (identifiant ou nom), `alerte`.

| Groupe | Colonnes | Source |
| --- | --- | --- |
| Activité GE | kVA, running time, h/jour, CPH, charge, kWh GE | moteur CPH (`cph_service.compute_period_summary`), FuelConsommationMonthly |
| Conso | mesurée, L/jour, **estimée (CPH × running time)**, écart mesuré − estimé (L, %), L/kWh | Snowflake VW_FUEL_REPORT, moteur CPH |
| Comparaisons | mois précédent (évolution du L/jour), target | FuelConsommationMonthly, targets actifs |
| Stock | stock retenu, date, source, estimé, remplissage, autonomie | `services/current_stock.py` |
| Livraisons | livré ENOC, nombre de livraisons | ENOC |

Mesuré et estimé ne sont jamais additionnés. L'écart global ne porte que sur les sites qui ont les deux.
Valeur inconnue = `null` (« — »), jamais 0 ; les KPI indiquent sur combien de sites ils portent.
Moteur CPH indisponible → `cph_disponible: false`, colonnes CPH vides, le reste s'affiche.

Alertes (seuils dans `views_sites_table.py`) : écart mesuré / estimé > 20 %, L/kWh > 0,30 (au moins 100 kWh),
au-dessus du target (tolérance Fuel Manager), GE sous-chargé (< 30 %), autonomie < 3 jours, relevé de stock > 7 jours.
