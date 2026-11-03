"""
Django settings for server project.
"""

import environ
import dj_database_url
import os
from pathlib import Path
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent

# Initialize Environment Variables
env = environ.Env()
env_file = os.path.join(BASE_DIR, '.env')
if os.path.exists(env_file):
    environ.Env.read_env(env_file)

# ==========================================
# 1. SECURITY SETTINGS
# ==========================================

SECRET_KEY = env('SECRET_KEY')
# Off unless DEBUG=True is set (in your local .env): debug pages show code and settings to anyone.
DEBUG = env.bool('DEBUG', default=False)

# Render provides RENDER_EXTERNAL_HOSTNAME; add others (a custom domain) with ALLOWED_HOSTS=a.com,b.com
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1', '.onrender.com'])
if os.environ.get('RENDER_EXTERNAL_HOSTNAME'):
    ALLOWED_HOSTS.append(os.environ['RENDER_EXTERNAL_HOSTNAME'])


# ==========================================
# 2. APPLICATION DEFINITION
# ==========================================

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third Party
    'rest_framework',
    'corsheaders',
    'django_filters',

    # My Apps
    'gradely',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'server.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'server.wsgi.application'


# ==========================================
# 3. DATABASE (Supabase)
# ==========================================

DATABASES = {
    'default': dj_database_url.config(
        default=env('DATABASE_URI'),
        conn_max_age=600,
        # Supabase requires SSL; set DB_SSL_REQUIRE=False for a local SQLite/Postgres.
        ssl_require=env.bool('DB_SSL_REQUIRE', default=True)
    )
}


# ==========================================
# 4. PASSWORD VALIDATION
# ==========================================

AUTH_PASSWORD_VALIDATORS = [
    { 'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator', },
    { 'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', },
    { 'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator', },
    { 'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator', },
]


# ==========================================
# 5. INTERNATIONALIZATION
# ==========================================

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# ==========================================
# 6. STATIC FILES (Basic Config)
# ==========================================

STATIC_URL = 'static/'
# We keep STATIC_ROOT because deployment scripts often run 'collectstatic' automatically.
# This prevents the build from crashing, even if we don't serve the files.
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')


# ==========================================
# 7. AUTH & API SETTINGS
# ==========================================

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'gradely.User'

REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # The frontend only uses JWT; Basic/Session auth would be extra ways in.
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    # Per-IP limits for the views that set throttle_scope (login, password change, sign-up)
    'DEFAULT_THROTTLE_RATES': {
        'login': '10/min',
        'signup': '20/hour',
    },
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60), 
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS': False,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'UPDATE_LAST_LOGIN': True,  # shown to admins on the Faculty page
}

# ==========================================
# 8. CORS
# ==========================================

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://gradely-frontend-lovat.vercel.app", 
]

CORS_ALLOW_CREDENTIALS = True

# Let the frontend read how many graded images made it into a results ZIP (QuizViewSet.export_results)
CORS_EXPOSE_HEADERS = ["Content-Disposition", "X-Images-Included", "X-Images-Missing"]

# ==========================================
# 9. GRADELY SCANNER API
# ==========================================
# The FastAPI service that renders answer sheets and reads scanned photos.

# Graded answer-sheet images (uploaded by the scanner) are deleted from Supabase Storage
# after this many days to save space; scores are kept. 0 keeps them forever.
SUPABASE_URL = env('SUPABASE_URL', default='').strip()
SUPABASE_SERVICE_KEY = env('SUPABASE_SERVICE_KEY', default='').strip()
SUPABASE_BUCKET = env('SUPABASE_BUCKET', default='answer-sheets')
SCANNED_IMAGE_RETENTION_DAYS = env.int('SCANNED_IMAGE_RETENTION_DAYS', default=3)

# Share of a quiz's items a student needs to count as passing (admin quiz overview).
PASSING_PERCENT = env.int('PASSING_PERCENT', default=75)

SCANNER_API_URL = env('SCANNER_API_URL', default='https://azcreas-gradely-scanner-api.hf.space').rstrip('/')
SCANNER_API_KEY = env('SCANNER_API_KEY', default='').strip()  # tolerate pasted whitespace/newlines
SCANNER_TIMEOUT = env.int('SCANNER_TIMEOUT', default=120)  # seconds per request

# Photos of answer sheets are uploaded through Django to the scanner.
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024