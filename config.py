"""
Configuration. Every secret / deployment-specific value comes from the
environment (see .env.example). Nothing sensitive is hard-coded.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_DEV_KEY = "dev-secret-key-change-me"


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", _DEV_KEY)
    DATABASE_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "database" / "attendance.db"))
    SCHEMA_PATH = str(BASE_DIR / "database" / "schema.sql")

    FACE_DATA_DIR = os.environ.get("FACE_DATA_DIR", str(BASE_DIR / "storage" / "face_data"))
    BACKUP_DIR = os.environ.get("BACKUP_DIR", str(BASE_DIR / "storage" / "backups"))

    FACE_SAMPLES_REQUIRED = int(os.environ.get("FACE_SAMPLES_REQUIRED", 20))
    FACE_RECOGNITION_THRESHOLD = float(os.environ.get("FACE_RECOGNITION_THRESHOLD", 65.0))

    # ---- Logo / media storage -------------------------------------------
    # STORAGE_BACKEND: "s3"    -> any S3-compatible bucket (AWS S3, Cloudflare R2,
    #                             Backblaze B2, Railway Bucket ...). Persistent.
    #                  "local" -> files under LOCAL_STORAGE_DIR. Persistent ONLY if
    #                             that directory is a mounted volume (Railway Volume).
    #                  ""      -> auto: "s3" when S3_BUCKET is set, otherwise
    #                             uploads are DISABLED in production (never silent).
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "").strip().lower()
    LOCAL_STORAGE_DIR = os.environ.get("LOCAL_STORAGE_DIR", "")
    S3_BUCKET = os.environ.get("S3_BUCKET", "")
    S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
    S3_REGION = os.environ.get("S3_REGION", "auto")
    S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
    S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
    MAX_LOGO_BYTES = int(os.environ.get("MAX_LOGO_BYTES", 2 * 1024 * 1024))
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 6 * 1024 * 1024))
    DEV_LOCAL_STORAGE_FALLBACK = False  # dev only: non-persistent local folder

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    WTF_CSRF_TIME_LIMIT = None


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    DEV_LOCAL_STORAGE_FALLBACK = True
    LOCAL_STORAGE_FALLBACK_DIR = str(BASE_DIR / "storage" / "branding")


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True

    def __init__(self):
        if self.SECRET_KEY == _DEV_KEY:
            raise RuntimeError("SECRET_KEY must be set via environment variable in production.")


class TestingConfig(BaseConfig):
    DEBUG = True
    TESTING = True
    DATABASE_PATH = ":memory:"
    DEV_LOCAL_STORAGE_FALLBACK = True
    LOCAL_STORAGE_FALLBACK_DIR = str(BASE_DIR / "storage" / "branding")


CONFIG_MAP = {"development": DevelopmentConfig, "production": ProductionConfig, "testing": TestingConfig}


def get_config():
    return CONFIG_MAP.get(os.environ.get("FLASK_ENV", "development"), DevelopmentConfig)
