from pathlib import Path
import json
import os
from datetime import timedelta


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-4z%jl$rp-=z8swn1&9lhej9gd!suod(@fwga(ozd%*8lh%#7+m'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "192.168.1.29",
     "api-enertrack.camusatsn.com",
]

CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://192.168.1.29",       # ← ajoute ceci
    "http://192.168.1.29:80",
    "http://192.168.1.29:8000",
    "http://192.168.1.29:8080",
    "https://api-enertrack.camusatsn.com",
    "https://egrid.camusatsn.com",
]

# En-têtes autorisés : ceux par défaut + le jeton de la section Configuration du module Carburant.
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = (*default_headers, "x-fuel-config-token")

# (optionnel mais recommandé si tu fais des POST depuis ces origines)
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:5173",
    "http://192.168.1.29",
    "http://192.168.1.29:80",
    "http://192.168.1.29:8000",
    "http://192.168.1.29:8080",
    "https://api-enertrack.camusatsn.com",
    "https://egrid.camusatsn.com",
]
# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
    'users',
    'fms',
    'target',
    'invoices',
    'energy',
    'rectifiers',  # ✅ nouveau
    'billing',    # ✅ nouveau
    'certification',
    'powerquality',  # ✅ nouveau
    'pwmreport',           # ✅ nouveau
    'corsheaders',
    'gridoutages',  # ✅ nouveau
    'dashboard',
    'estimation',
    'financial',
    'prediction',
    'ml',
    'optimization',
    "fuel_tracking",
    "bo_analysis",
    "pilotage_grid",

]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'corsheaders.middleware.CorsMiddleware',
]



ROOT_URLCONF = 'enertrack_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'enertrack_backend.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get("POSTGRES_DB"),
        'USER': os.environ.get("POSTGRES_USER"),
        'PASSWORD': os.environ.get("POSTGRES_PASSWORD"),
        'HOST': os.environ.get("POSTGRES_HOST"),
        'PORT': os.environ.get("POSTGRES_PORT"),
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Répertoire des fichiers uploadés (partagé entre le container web et le worker Celery
# via le volume Docker "media_data" monté sur /code/media dans les deux services).
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'users.CustomUser'


REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
}




SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=4),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}


CELERY_BROKER_URL = 'redis://redis:6379/0'  # nom du service Docker
CELERY_RESULT_BACKEND = 'redis://redis:6379/0'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'

# Recyclage des process worker — sans ça, un worker tourne indéfiniment et
# toute croissance mémoire graduelle (pandas/openpyxl sur les gros fichiers,
# connexions Snowflake, imports lourds type lightgbm/scikit-learn/shap
# chargés une fois par process) s'accumule pour de bon au lieu d'être
# purgée. Valeurs de départ prudentes — à ajuster selon la RAM réellement
# observée en prod (docker stats), pas des chiffres mesurés ici.
# Une tâche en attente ne doit jamais rester bloquée derrière des synchros longues :
#  - prefetch 1 : un processus ne « réserve » pas des tâches qu'il ne traitera qu'après la synchro en cours ;
#  - les actions déclenchées par un utilisateur (import facturation, certification) passent par une file dédiée « interactive »,
#    consommée par le worker principal ET par un worker dédié (service celery-interactive) — jamais derrière les synchros Snowflake/ENOC.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Les ~1 800 sous-tâches d'une campagne de certification (une par facture) partent sur la file par défaut :
# dans « interactive », elles passaient devant les imports suivants, restés « En file d'attente… ».
# (Les noms exacts priment sur les motifs « * ».)
CELERY_TASK_ROUTES = {
    "certification.certify_single_invoice": {"queue": "celery"},
    "certification.finalize_certification_batch": {"queue": "celery"},
    "billing.*": {"queue": "interactive"},
    "certification.*": {"queue": "interactive"},
}
CELERY_WORKER_MAX_TASKS_PER_CHILD = 100
CELERY_WORKER_MAX_MEMORY_PER_CHILD = 500_000  # Ko (≈500 Mo)

# Planification (nécessite le service "beat" — voir docker-compose.yml) :
# suivi-carburant Consommation, toutes les 5 min sur le mois en cours, pour
# rattraper rapidement les nouvelles données Snowflake/ENOC. CPH (Running
# Time/Type de GE/Énergie site télémétrie) et Stock étaient absentes d'ici
# jusqu'au 2026-08 — jamais synchronisées automatiquement en prod, d'où ces
# colonnes vides malgré Consommation/ENOC qui tournaient bien. CPH est plus
# coûteuse (télémétrie 5 min sur tous les sites) et n'a pas besoin d'une
# fraîcheur à la minute (agrégat mensuel) : toutes les 30 min suffit.
from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    'fuel-consommation-sync-5min': {
        'task': 'fuel_tracking.sync_fuel_consommation_current_month',
        'schedule': crontab(minute='*/5'),
    },
    'fuel-enoc-sync-5min': {
        'task': 'fuel_tracking.sync_enoc_fuel_movements_current_month',
        'schedule': crontab(minute='*/5'),
    },
    'fuel-daily-facts-sync-hourly': {
        'task': 'fuel_tracking.sync_fuel_daily_facts_recent',
        'schedule': crontab(minute=17),
    },
    # Rattrapage nocturne J-35 (données arrivées en retard, conso mesurée) — Africa/Dakar = UTC.
    'fuel-daily-facts-resync-nightly': {
        'task': 'fuel_tracking.resync_fuel_daily_facts_nightly',
        'schedule': crontab(hour=2, minute=41),
    },
    'fuel-cph-health-daily': {
        'task': 'fuel_tracking.check_cph_health_daily',
        'schedule': crontab(hour=6, minute=53),
    },
    # Bilans de stock des 2 derniers mois complets : recalculés chaque nuit (nouvelles données Closing / gardiens / ENOC prises en compte seules).
    'fuel-balances-nightly': {
        'task': 'fuel_tracking.auto_compute_stock_balances',
        'schedule': crontab(hour=3, minute=11),
    },
    'fuel-stock-sync-5min': {
        'task': 'fuel_tracking.sync_fuel_stock_current',
        'schedule': crontab(minute='*/5'),
    },
    'financial-conso-sync-30min': {
        'task': 'financial.sync_financial_conso_auto',
        'schedule': crontab(minute='*/30'),
    },
    # Vérifie toutes les 15 min si un nouveau fichier Stan a été déposé dans
    # data_imports/stan/ et l'importe automatiquement. Idempotent : pas de
    # ré-import si le fichier n'a pas changé et tous les mois sont couverts.
    'stan-auto-import-15min': {
        'task': 'fuel_tracking.auto_import_stan_periodic',
        'schedule': crontab(minute='*/15'),
    },
    # Registre stock : fichiers déposés dans FUEL_IMPORT_ROOT/incoming/{loliforce,scss,closing,modeles_cuves}
    # (dédoublonnage par empreinte : un fichier déjà traité n'est jamais réimporté).
    'fuel-stock-registry-incoming-15min': {
        'task': 'fuel_tracking.process_fuel_import_incoming',
        'schedule': crontab(minute='7,22,37,52'),
    },
    # Santé des sources (lecture seule des journaux) : toute source en retard / échec est journalisée en ERROR.
    'fuel-sources-health-hourly': {
        'task': 'fuel_tracking.check_fuel_sources_hourly',
        'schedule': crontab(minute=29),
    },
}

# Expiration des tâches planifiées : une synchro qui n'a pas pu démarrer avant la suivante est abandonnée
# (la suivante couvre la même fenêtre — mois en cours / jours récents), au lieu de s'empiler.
# Constaté en prod le 2026-11-09 : 20 432 tâches en attente dans la file « celery », workers saturés par
# les synchros 5 min, et les ~1 900 sous-tâches d'une campagne de certification bloquées derrière (1 % pendant des heures).
_BEAT_EXPIRES_S = {
    'fuel-consommation-sync-5min': 4 * 60,
    'fuel-enoc-sync-5min': 4 * 60,
    'fuel-stock-sync-5min': 4 * 60,
    'stan-auto-import-15min': 14 * 60,
    'fuel-stock-registry-incoming-15min': 14 * 60,
    'fuel-sources-health-hourly': 55 * 60,
    'financial-conso-sync-30min': 28 * 60,
    'fuel-daily-facts-sync-hourly': 55 * 60,
    'fuel-daily-facts-resync-nightly': 6 * 3600,
    'fuel-balances-nightly': 6 * 3600,
    'fuel-cph-health-daily': 6 * 3600,
}
for _name, _entry in CELERY_BEAT_SCHEDULE.items():
    _entry.setdefault('options', {}).setdefault('expires', _BEAT_EXPIRES_S.get(_name, 3600))

# Cache Redis (DB 1, distincte du broker Celery en DB 0) — utilisé notamment pour
# mettre en cache les lectures vers des sources externes lentes (Mongo ENOC,
# Snowflake) afin que l'utilisateur ne ressente pas leur latence réseau.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': 'redis://redis:6379/1',
    }
}

# Email — bascule sur un vrai SMTP en définissant EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
# + EMAIL_HOST/EMAIL_PORT/EMAIL_HOST_USER/EMAIL_HOST_PASSWORD/EMAIL_USE_TLS. Par défaut, les emails sont
# juste loggés en console pour ne jamais bloquer le workflow tant que le SMTP n'est pas configuré.
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@enertrack.local")

# URL du frontend, utilisée dans les emails transactionnels (compte créé/désactivé...)
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://egrid.camusatsn.com")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EFMS_SQL_HOST      = os.environ.get("EFMS_SQL_HOST",     "172.30.0.149")
EFMS_SQL_PORT      = int(os.environ.get("EFMS_SQL_PORT", 1433))
EFMS_SQL_DB        = os.environ.get("EFMS_SQL_DB",       "SQL1-ProdDB")
EFMS_SQL_USER      = os.environ.get("EFMS_SQL_USER",     "")
EFMS_SQL_PASSWORD  = os.environ.get("EFMS_SQL_PASSWORD", "")
EFMS_SQL_DRIVER    = os.environ.get("EFMS_SQL_DRIVER",   "ODBC Driver 17 for SQL Server")
EFMS_SQL_TIMEOUT   = int(os.environ.get("EFMS_SQL_TIMEOUT",     10))
EFMS_SQL_MAX_RETRIES = int(os.environ.get("EFMS_SQL_MAX_RETRIES", 2))

SNOWFLAKE_ACCOUNT   = os.environ.get("SNOWFLAKE_ACCOUNT", "")
SNOWFLAKE_USER      = os.environ.get("SNOWFLAKE_USER", "")
SNOWFLAKE_PASSWORD  = os.environ.get("SNOWFLAKE_PASSWORD", "")
_sf_key_path        = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "")
SNOWFLAKE_PRIVATE_KEY_PATH = str(BASE_DIR / _sf_key_path) if _sf_key_path else ""
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "")
SNOWFLAKE_ROLE      = os.environ.get("SNOWFLAKE_ROLE", "")
# Délais de connexion : sans eux, une connexion Snowflake bloquée garde un processus Celery indéfiniment (file saturée, imports « en attente »).
SNOWFLAKE_LOGIN_TIMEOUT = int(os.environ.get("SNOWFLAKE_LOGIN_TIMEOUT", "60"))
SNOWFLAKE_NETWORK_TIMEOUT = int(os.environ.get("SNOWFLAKE_NETWORK_TIMEOUT", "600"))
SNOWFLAKE_DATABASE  = os.environ.get("SNOWFLAKE_DATABASE", "DB_GFMS_ANALYTICS_PROD")
SNOWFLAKE_SCHEMA    = os.environ.get("SNOWFLAKE_SCHEMA", "GOLD")

ENOC_BASE_URL = os.getenv("ENOC_BASE_URL", "").rstrip("/")

# Répertoire contenant la liste des sites GRID/ESCO.
# Par défaut : data_imports/grid_sites/ (monté via le volume Docker →
#   déposer le fichier xlsx/csv là pour le voir en local sans config).
# En production : surcharger via GRID_SITES_LIST_DIR=/mnt/ESCO/Energie Desk/GRID/Listes Sites
GRID_SITES_LIST_DIR = os.environ.get(
    "GRID_SITES_LIST_DIR",
    str(BASE_DIR / "data_imports" / "grid_sites"),
)
ENOC_INTEGRATION_CLIENT_ID = os.getenv("ENOC_INTEGRATION_CLIENT_ID", "enertrack")
ENOC_INTEGRATION_SHARED_SECRET = os.getenv("ENOC_INTEGRATION_SHARED_SECRET", "")

ENOC_MONGO_HOST     = os.getenv("ENOC_MONGO_HOST", "")
ENOC_MONGO_PORT     = int(os.getenv("ENOC_MONGO_PORT", "27017"))
ENOC_MONGO_USERNAME = os.getenv("ENOC_MONGO_USERNAME", "")
ENOC_MONGO_PASSWORD = os.getenv("ENOC_MONGO_PASSWORD", "")
ENOC_MONGO_DB_NAME  = os.getenv("ENOC_MONGO_DB_NAME", "")
# Ponction ENOC (règle validée) : statuts comptabilisables et seuil d'alerte Fuel Manager
# écart de contrôle > max(FUEL_PONCTION_GAP_MIN_L ; FUEL_PONCTION_GAP_PCT % de la quantité approuvée)
# Mapping figé des statuts ENOC (fuel_tracking/services/enoc_canonical.py::map_status, docs/fuel_stock_registry.md) :
#   validated (ENOC actuel) → VALIDEE_EXECUTABLE ; done (ENOC actuel, validation tracée) → VALIDEE_EXECUTEE ;
#   tout statut de l'ancien connecteur REST → HISTORIQUE_NON_VERIFIABLE ; rejected → REJETEE ; absent / inconnu → STATUT_NON_VERIFIABLE.
FUEL_ENOC_VALIDATED_STATUSES = [x.strip() for x in os.getenv("FUEL_ENOC_VALIDATED_STATUSES", "validated,validee,validée").split(",") if x.strip()]
FUEL_ENOC_DONE_IS_EXECUTED = os.getenv("FUEL_ENOC_DONE_IS_EXECUTED", "true").strip().lower() not in ("0", "false", "non", "no")
FUEL_ENOC_DONE_REQUIRES_VALIDATION = os.getenv("FUEL_ENOC_DONE_REQUIRES_VALIDATION", "true").strip().lower() not in ("0", "false", "non", "no")
FUEL_PONCTION_GAP_MIN_L = float(os.getenv("FUEL_PONCTION_GAP_MIN_L", "20"))
FUEL_PONCTION_GAP_PCT = float(os.getenv("FUEL_PONCTION_GAP_PCT", "10"))
# Lien vers une demande ENOC dans l'interface ENOC (optionnel), ex. https://enoc.example/fuel/requests/{reference}
ENOC_REQUEST_URL_TEMPLATE = os.getenv("ENOC_REQUEST_URL_TEMPLATE", "")

# Registre stock carburant (Suivis Stock) — répertoire partagé monté (voir docker-compose.yml) :
#   incoming/{loliforce,scss,closing,modeles_cuves}, processing, accepted, rejected, archive.
FUEL_IMPORT_ROOT = os.getenv("FUEL_IMPORT_ROOT", "/mnt/Suivi_carburant_prod")
# Tolérance de dépassement de capacité cuve (en %) avant de déclarer un volume invalide.
FUEL_CAPACITY_TOLERANCE_PCT = float(os.getenv("FUEL_CAPACITY_TOLERANCE_PCT", "5"))
# Priorité des sources entre relevés validés du même jour (stock retenu). Snowflake n'est jamais retenu.
FUEL_STOCK_SOURCE_PRIORITY = tuple(
    x.strip() for x in os.getenv("FUEL_STOCK_SOURCE_PRIORITY", "TERRAIN_PREUVE,LOLIFORCE,SCSS,ENOC").split(",") if x.strip()
)
# Conflit Loliforce / SCSS : prestataire prioritaire (LOLIFORCE ou SCSS) ; vide = exception (les deux à traiter).
FUEL_GUARD_CONFLICT_PRIORITY = os.getenv("FUEL_GUARD_CONFLICT_PRIORITY", "")
ENOC_INTEGRATION_TIMEOUT = int(os.getenv("ENOC_INTEGRATION_TIMEOUT", "30"))

FUEL_TRACKING_SITE_MODEL = "core.Site"

# Suivi Carburant / CPH (fuel_tracking/services/cph_engine.py)
FUEL_CPH_COUNTRIES = [c.strip() for c in os.environ.get("FUEL_CPH_COUNTRIES", "Senegal").split(",") if c.strip()]
# Tant que les livraisons ENOC réelles ne sont pas raccordées, tout rapprochement
# reste « LIVRAISONS_ENOC_A_CONTROLER » (instruction §7). Ne passer à 1 qu'après raccordement.
FUEL_ENOC_DELIVERIES_CONNECTED = os.environ.get("FUEL_ENOC_DELIVERIES_CONNECTED", "0") == "1"
# Unités Snowflake à confirmer par `manage.py diagnose_cph_sources` (P_DC en W → kW, rendement en % → ratio).
FUEL_CPH_P_DC_TO_KW_DIVISOR = os.environ.get("FUEL_CPH_P_DC_TO_KW_DIVISOR", "1000")
FUEL_CPH_EFFICIENCY_TO_RATIO_DIVISOR = os.environ.get("FUEL_CPH_EFFICIENCY_TO_RATIO_DIVISOR", "100")
# AC_METER.ACT_ACTIVE_POWER_AVG en W → kW (à confirmer : diagnose_cph_site signale une unité suspecte).
FUEL_CPH_AC_POWER_TO_KW_DIVISOR = os.environ.get("FUEL_CPH_AC_POWER_TO_KW_DIVISOR", "1000")
# Correspondance plaque → courbe : écart relatif max entre kVA inventaire et kVA de la courbe.
FUEL_CPH_MATCH_POWER_TOLERANCE = os.environ.get("FUEL_CPH_MATCH_POWER_TOLERANCE", "0.15")
# Puissance active nominale GE = kVA × facteur (charge GE et plafond 1,05 × kVA × facteur).
FUEL_CPH_NOMINAL_POWER_FACTOR = os.environ.get("FUEL_CPH_NOMINAL_POWER_FACTOR", "0.8")
# Plage de plausibilité de la consommation spécifique GE (L/kWh) : hors plage = alerte (non bloquant).
FUEL_CPH_SFC_MIN_L_KWH = os.environ.get("FUEL_CPH_SFC_MIN_L_KWH", "0.20")
FUEL_CPH_SFC_MAX_L_KWH = os.environ.get("FUEL_CPH_SFC_MAX_L_KWH", "0.50")

# Surveillance CPH : données périmées au-delà de N jours, couverture minimale, part max d'alertes L/kWh.
FUEL_CPH_STALE_AFTER_DAYS = os.environ.get("FUEL_CPH_STALE_AFTER_DAYS", "3")
# Base Snowflake de la vue VW_FUEL_REPORT (conso mesurée par capteur). Aujourd'hui publiée en DEV seulement ;
# passer à DB_GFMS_ANALYTICS_PROD dès que l'admin Snowflake l'y publie, sans modifier le code.
FUEL_REPORT_DATABASE = os.environ.get("FUEL_REPORT_DATABASE", "DB_GFMS_ANALYTICS_DEV")
# Page Configuration du module Carburant : code d'accès à 6 chiffres, comparé à son EMPREINTE (jamais stocké en clair).
# Pour changer le code : générer une empreinte avec `python manage.py shell -c "from django.contrib.auth.hashers import
# make_password; print(make_password('NOUVEAU_CODE'))"` et la mettre dans FUEL_CONFIG_CODE_HASH (environnement du serveur).
FUEL_CONFIG_CODE_HASH = os.environ.get("FUEL_CONFIG_CODE_HASH", "pbkdf2_sha256$1000000$KJewnGGEW9vHH6qJbjxglJ$4oCrYkYiTZwJ6eeB4imsCT7K1dtBRXm6ZInhywV33ZE=")
FUEL_CONFIG_TOKEN_TTL_S = int(os.environ.get("FUEL_CONFIG_TOKEN_TTL_S", "7200"))
FUEL_CONFIG_MAX_ATTEMPTS = int(os.environ.get("FUEL_CONFIG_MAX_ATTEMPTS", "5"))
FUEL_CONFIG_LOCK_S = int(os.environ.get("FUEL_CONFIG_LOCK_S", "900"))
FUEL_CONFIG_MAX_UPLOAD_MB = int(os.environ.get("FUEL_CONFIG_MAX_UPLOAD_MB", "50"))
FUEL_CPH_MIN_COVERAGE = os.environ.get("FUEL_CPH_MIN_COVERAGE", "0.5")
FUEL_CPH_MAX_SFC_ALERT_SHARE = os.environ.get("FUEL_CPH_MAX_SFC_ALERT_SHARE", "0.2")


# ── Bilan stock MVP (fuel_tracking/services/stock_period_balance.py) ─────────────────────────────────────────
# Fraîcheur : relevé à 0-3 j de la borne = exploitable ; 4-14 j = estimation avec réserve ; > 14 j = non calculable.
FUEL_BALANCE_FRESH_STOCK_DAYS = int(os.getenv("FUEL_BALANCE_FRESH_STOCK_DAYS", "3"))
FUEL_BALANCE_RESERVE_STOCK_DAYS = int(os.getenv("FUEL_BALANCE_RESERVE_STOCK_DAYS", "14"))
# Score de confiance 0-100 : ≥ READY (sans réserve) = prêt à certifier ; ≥ RESERVE = estimation avec réserve ; sinon non calculable.
FUEL_BALANCE_SCORE_READY = int(os.getenv("FUEL_BALANCE_SCORE_READY", "80"))
FUEL_BALANCE_SCORE_RESERVE = int(os.getenv("FUEL_BALANCE_SCORE_RESERVE", "50"))
# Barème (points) : stock_initial, stock_final, litre_direct_ou_cuve_validee, mouvements_complets, stock_recent, sans_alerte_majeure
FUEL_BALANCE_SCORE_WEIGHTS = json.loads(os.getenv("FUEL_BALANCE_SCORE_WEIGHTS", "{}") or "{}")
# Score minimal de compatibilité (0-100) pour estimer des litres avec une cuve PROPOSÉE (jamais validée automatiquement).
FUEL_PROPOSED_TANK_MIN_SCORE = int(os.getenv("FUEL_PROPOSED_TANK_MIN_SCORE", "70"))
# Décision Steve : une cuve VALIDÉE mais SANS plaque (dimensions cohérentes) peut-elle produire une estimation ? Désactivé par défaut.
# Si activé : résultat UNIQUEMENT « estimation avec réserve », jamais certifiable automatiquement.
# Closing mensuel et ENOC : une livraison présente au Closing et absente d'ENOC COMPLÈTE le calcul (estimation avec réserve, alerte
# LIVRAISON_CLOSING_ABSENTE_ENOC). Si les deux divergent : conflit tracé (CONFLIT_ENOC_CLOSING), jamais d'écrasement silencieux ;
# au-delà de FUEL_CLOSING_CONFLICT_REVIEW_L litres d'écart, le bilan est « à contrôler » (non calculable).
FUEL_CLOSING_CONFLICT_REVIEW_L = float(os.getenv("FUEL_CLOSING_CONFLICT_REVIEW_L", "300"))
# Périodes (mois AAAA-MM, séparés par des virgules) dont le Closing est VALIDÉ : sur ces périodes seulement, en cas de conflit, les totaux du
# Closing remplacent ceux d'ENOC (ENOC reste affiché). Jamais une règle globale applicable à tous les mois. Exemple : "2026-08".
FUEL_CLOSING_AUTHORITATIVE_PERIODS = [x.strip() for x in os.getenv("FUEL_CLOSING_AUTHORITATIVE_PERIODS", "").split(",") if x.strip()]
# Cuves à traiter en premier (décision métier), séparées par des virgules.
FUEL_PRIORITY_TANK_SITES = [x.strip() for x in os.getenv("FUEL_PRIORITY_TANK_SITES", "KLD_2035,KLD_2049,KLD_1259,KLD_1775,BKL_0516").split(",") if x.strip()]
# Cuve VALIDÉE sans plaque, dimensions cohérentes : estimation autorisée UNIQUEMENT « avec réserve » (jamais certifiable automatiquement).
ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE = os.getenv("ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE", "0").lower() in ("1", "true", "yes", "on")
# Données préparées pour la future recommandation de commande (indicatif, aucune commande automatique).
FUEL_ORDER_HORIZON_DAYS = int(os.getenv("FUEL_ORDER_HORIZON_DAYS", "30"))
