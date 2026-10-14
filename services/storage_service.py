"""
Persistent file storage for school logos.

Backends (chosen by environment variables — see config.py / README):
  s3     S3-compatible object storage (AWS S3, Cloudflare R2, Backblaze B2,
         Railway Bucket ...). Survives restarts and redeploys.
  local  A directory (LOCAL_STORAGE_DIR). Only persistent if it is a mounted
         volume. Railway's container filesystem is ephemeral.

If nothing persistent is configured in production, `status()` reports
configured=False with setup instructions and uploads are refused. The app never
pretends an upload is permanent.
"""

import os
from dataclasses import dataclass

SETUP_HELP = (
    "Logo uploads need persistent storage. Set STORAGE_BACKEND=s3 with S3_BUCKET, "
    "S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY (and S3_ENDPOINT_URL / S3_REGION for "
    "R2, B2 or a Railway Bucket), or set STORAGE_BACKEND=local with LOCAL_STORAGE_DIR "
    "pointing at a mounted Railway Volume. See README > Persistent logo storage."
)


class StorageNotConfigured(RuntimeError):
    pass


class StorageError(RuntimeError):
    pass


@dataclass
class StorageStatus:
    configured: bool
    backend: str          # "s3" | "local" | "dev-local" | "none"
    persistent: bool
    message: str


class LocalBackend:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key):
        path = os.path.abspath(os.path.join(self.root, key))
        if not path.startswith(self.root + os.sep):
            raise StorageError("Invalid storage key.")
        return path

    def save(self, key, data, content_type):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def read(self, key):
        try:
            with open(self._path(key), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


class S3Backend:
    def __init__(self, cfg):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover
            raise StorageNotConfigured("boto3 is not installed (pip install boto3).") from exc
        self.bucket = cfg["S3_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.get("S3_ENDPOINT_URL") or None,
            region_name=cfg.get("S3_REGION") or "auto",
            aws_access_key_id=cfg.get("S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=cfg.get("S3_SECRET_ACCESS_KEY") or None,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def save(self, key, data, content_type):
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"Storage upload failed: {exc}") from exc

    def read(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"Storage read failed: {exc}") from exc

    def delete(self, key):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"Storage delete failed: {exc}") from exc


_override = None  # tests inject a backend here


def set_backend_override(backend):
    global _override
    _override = backend


def _kind(cfg):
    kind = (cfg.get("STORAGE_BACKEND") or "").lower()
    if kind:
        return kind
    return "s3" if cfg.get("S3_BUCKET") else ""


def status(cfg) -> StorageStatus:
    if _override is not None:
        return StorageStatus(True, "override", True, "Custom storage backend.")
    kind = _kind(cfg)
    if kind == "s3":
        if not cfg.get("S3_BUCKET"):
            return StorageStatus(False, "none", False, "STORAGE_BACKEND=s3 but S3_BUCKET is not set. " + SETUP_HELP)
        return StorageStatus(True, "s3", True, "Logos are stored in object storage and survive redeploys.")
    if kind == "local":
        if not cfg.get("LOCAL_STORAGE_DIR"):
            return StorageStatus(False, "none", False, "STORAGE_BACKEND=local but LOCAL_STORAGE_DIR is not set. " + SETUP_HELP)
        return StorageStatus(True, "local", True,
                             "Logos are stored in LOCAL_STORAGE_DIR. This is only permanent if that "
                             "directory is a mounted volume.")
    if cfg.get("DEV_LOCAL_STORAGE_FALLBACK"):
        return StorageStatus(True, "dev-local", False,
                             "Development mode: logos are saved to a local folder that is NOT "
                             "persistent on Railway. Configure S3 or a Volume for production.")
    return StorageStatus(False, "none", False, SETUP_HELP)


def get_backend(cfg):
    if _override is not None:
        return _override
    st = status(cfg)
    if not st.configured:
        raise StorageNotConfigured(st.message)
    if st.backend == "s3":
        return S3Backend(cfg)
    if st.backend == "local":
        return LocalBackend(cfg["LOCAL_STORAGE_DIR"])
    return LocalBackend(cfg["LOCAL_STORAGE_FALLBACK_DIR"])
