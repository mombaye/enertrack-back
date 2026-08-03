from pathlib import Path
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
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}


CELERY_BROKER_URL = 'redis://redis:6379/0'  # nom du service Docker
CELERY_RESULT_BACKEND = 'redis://redis:6379/0'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'

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
SNOWFLAKE_DATABASE  = os.environ.get("SNOWFLAKE_DATABASE", "DB_GFMS_ANALYTICS_PROD")
SNOWFLAKE_SCHEMA    = os.environ.get("SNOWFLAKE_SCHEMA", "GOLD")

ENOC_BASE_URL = os.getenv("ENOC_BASE_URL", "").rstrip("/")
ENOC_INTEGRATION_CLIENT_ID = os.getenv("ENOC_INTEGRATION_CLIENT_ID", "enertrack")
ENOC_INTEGRATION_SHARED_SECRET = os.getenv("ENOC_INTEGRATION_SHARED_SECRET", "")

ENOC_MONGO_HOST     = os.getenv("ENOC_MONGO_HOST", "")
ENOC_MONGO_PORT     = int(os.getenv("ENOC_MONGO_PORT", "27017"))
ENOC_MONGO_USERNAME = os.getenv("ENOC_MONGO_USERNAME", "")
ENOC_MONGO_PASSWORD = os.getenv("ENOC_MONGO_PASSWORD", "")
ENOC_MONGO_DB_NAME  = os.getenv("ENOC_MONGO_DB_NAME", "")
ENOC_INTEGRATION_TIMEOUT = int(os.getenv("ENOC_INTEGRATION_TIMEOUT", "30"))

FUEL_TRACKING_SITE_MODEL = "core.Site"