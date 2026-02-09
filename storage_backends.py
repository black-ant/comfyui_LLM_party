import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import requests


VALID_STORAGE_TYPES = {"modal", "minio", "local"}


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def _pick_first(*values):
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return ""


def _read_config(config, key, default=""):
    if config is None:
        return default
    return config.get("API_KEYS", key, fallback=default)


def _split_access_and_secret(value: str):
    raw = _clean(value)
    if not raw:
        return "", ""
    for sep in (":", "|", ","):
        if sep in raw:
            access_key, secret_key = raw.split(sep, 1)
            return access_key.strip(), secret_key.strip()
    return raw, ""


def _parse_minio_endpoint(endpoint_url: str):
    raw = _clean(endpoint_url)
    if not raw:
        return "", False
    normalized = raw if "://" in raw else f"http://{raw}"
    parsed = urllib.parse.urlparse(normalized)
    endpoint = (parsed.netloc or parsed.path).strip().strip("/")
    if "/" in endpoint:
        endpoint = endpoint.split("/", 1)[0]
    return endpoint, parsed.scheme == "https"


def _build_minio_public_base_url(public_base_url: str, endpoint: str, secure: bool, bucket: str):
    base_url = _clean(public_base_url).rstrip("/")
    if not base_url and not endpoint:
        return ""
    if not base_url:
        scheme = "https" if secure else "http"
        base_url = f"{scheme}://{endpoint}".rstrip("/")
    if bucket and not base_url.endswith(f"/{bucket}"):
        base_url = f"{base_url}/{bucket}"
    return base_url.rstrip("/")


def _build_object_name(custom_filename: str, counter: int, model_name: str):
    timestamp_ms = int(time.time() * 1000)
    custom_name = _clean(custom_filename)
    if custom_name:
        custom_name = os.path.basename(custom_name)
        base, ext = os.path.splitext(custom_name)
        safe_base = re.sub(r"[^a-zA-Z0-9_.-]", "_", base) or "image"
        return f"{safe_base}_{timestamp_ms}_{counter}{ext or '.png'}"
    safe_model_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", model_name) or "workflow"
    return f"{safe_model_name}_{timestamp_ms}_{counter}.png"


@dataclass
class StorageSettings:
    enabled: bool = False
    storage_type: str = "none"
    base_url: str = ""
    public_base_url: str = ""
    api_key: str = ""
    secret_key: str = ""
    channel: str = ""
    custom_filename: str = ""
    secure: bool = False


class StorageBackend:
    name = "storage"

    def upload(self, image_bytes: bytes, counter: int, model_name: str) -> str:
        raise NotImplementedError


class LocalStorageBackend(StorageBackend):
    name = "local"

    def __init__(self, output_dir: str, public_base_url: str, custom_filename: str = ""):
        self.output_dir = output_dir
        self.public_base_url = public_base_url.rstrip("/")
        self.custom_filename = custom_filename
        os.makedirs(self.output_dir, exist_ok=True)

    def upload(self, image_bytes: bytes, counter: int, model_name: str) -> str:
        object_name = _build_object_name(self.custom_filename, counter, model_name)
        local_file_path = os.path.join(self.output_dir, object_name)
        with open(local_file_path, "wb") as f:
            f.write(image_bytes)
        quoted_name = urllib.parse.quote(object_name, safe="/")
        return f"{self.public_base_url}/images/{quoted_name}"


class ModalStorageBackend(StorageBackend):
    name = "modal"

    def __init__(self, base_url: str, api_key: str, channel: str, public_base_url: str, custom_filename: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.channel = channel
        self.public_base_url = public_base_url.rstrip("/")
        self.custom_filename = custom_filename

    def upload(self, image_bytes: bytes, counter: int, model_name: str) -> str:
        filename = _build_object_name(self.custom_filename, counter, model_name)
        upload_url = f"{self.base_url}/api/files/upload"
        headers = {"X-API-Key": self.api_key, "X-Channel": self.channel}
        files = {"file": (filename, image_bytes, "image/png")}
        params = {"custom_filename": filename}
        response = requests.post(upload_url, headers=headers, files=files, params=params, timeout=120)
        if response.status_code != 200:
            raise RuntimeError(f"Modal upload failed: {response.status_code} {response.text}")
        data = response.json()
        stored_filename = data.get("stored_filename") or data.get("filename")
        if not stored_filename:
            raise RuntimeError("Modal response missing stored filename")
        quoted_name = urllib.parse.quote(stored_filename, safe="/")
        return f"{self.public_base_url}/api/files/{quoted_name}"


class MinioStorageBackend(StorageBackend):
    name = "minio"

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
        public_base_url: str,
        custom_filename: str = "",
    ):
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.secure = secure
        self.public_base_url = public_base_url.rstrip("/")
        self.custom_filename = custom_filename

    def upload(self, image_bytes: bytes, counter: int, model_name: str) -> str:
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError("Missing dependency: minio. Please install it first.") from exc

        object_name = _build_object_name(self.custom_filename, counter, model_name)
        client = Minio(
            self.endpoint,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=self.secure,
        )
        try:
            if not client.bucket_exists(self.bucket):
                client.make_bucket(self.bucket)
        except Exception:
            pass
        data_stream = BytesIO(image_bytes)
        client.put_object(
            self.bucket,
            object_name,
            data_stream,
            length=len(image_bytes),
            content_type="image/png",
        )
        quoted_name = urllib.parse.quote(object_name, safe="/")
        return f"{self.public_base_url}/{quoted_name}"


def load_storage_settings(
    config,
    cli_type=None,
    cli_enabled=None,
    cli_base_url=None,
    cli_public_base_url=None,
    cli_api_key=None,
    cli_secret_key=None,
    cli_channel=None,
    cli_custom_filename=None,
):
    storage_type_raw = _pick_first(
        cli_type,
        os.getenv("OBJECT_STORAGE_TYPE"),
        _read_config(config, "object_storage_type"),
    ).lower()

    enabled_raw = cli_enabled
    if enabled_raw is None:
        enabled_raw = _pick_first(
            os.getenv("OBJECT_STORAGE_ENABLED"),
            _read_config(config, "object_storage_enabled"),
        )
    enabled_flag = parse_bool(enabled_raw, default=False)
    explicit_type = bool(storage_type_raw)
    enabled = enabled_flag or explicit_type
    custom_filename = _pick_first(
        cli_custom_filename,
        os.getenv("OBJECT_STORAGE_CUSTOM_FILENAME"),
        _read_config(config, "object_storage_custom_filename"),
    )

    if not enabled:
        return StorageSettings(enabled=False, storage_type="none", custom_filename=custom_filename)
    if not storage_type_raw:
        raise RuntimeError(
            "Object storage is enabled but type is missing. Pass --object-storage-type modal|minio|local."
        )
    if storage_type_raw not in VALID_STORAGE_TYPES:
        raise RuntimeError(
            f"Unsupported object storage type '{storage_type_raw}'. Use one of: modal, minio, local."
        )

    if storage_type_raw == "local":
        return StorageSettings(enabled=True, storage_type="local", custom_filename=custom_filename)

    if storage_type_raw == "modal":
        base_url = _pick_first(
            cli_base_url,
            os.getenv("MODAL_OBJECT_STORAGE_BASE_URL"),
            os.getenv("OBJECT_STORAGE_BASE_URL"),
            _read_config(config, "object_storage_base_url"),
        )
        api_key = _pick_first(
            cli_api_key,
            os.getenv("MODAL_OBJECT_STORAGE_API_KEY"),
            os.getenv("OBJECT_STORAGE_API_KEY"),
            _read_config(config, "object_storage_api_key"),
        )
        channel = _pick_first(
            cli_channel,
            os.getenv("MODAL_OBJECT_STORAGE_CHANNEL"),
            os.getenv("OBJECT_STORAGE_CHANNEL"),
            _read_config(config, "object_storage_channel", "default"),
            "default",
        )
        public_base_url = _pick_first(
            cli_public_base_url,
            os.getenv("MODAL_OBJECT_STORAGE_PUBLIC_BASE_URL"),
            os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL"),
            _read_config(config, "object_storage_public_base_url"),
            base_url,
        )
        return StorageSettings(
            enabled=True,
            storage_type="modal",
            base_url=base_url.rstrip("/"),
            public_base_url=public_base_url.rstrip("/"),
            api_key=api_key,
            channel=channel,
            custom_filename=custom_filename,
        )

    base_url = _pick_first(
        cli_base_url,
        os.getenv("MINIO_ENDPOINT"),
        os.getenv("OBJECT_STORAGE_BASE_URL"),
        _read_config(config, "object_storage_base_url"),
    )
    api_key = _pick_first(
        cli_api_key,
        os.getenv("MINIO_ACCESS_KEY"),
        os.getenv("OBJECT_STORAGE_API_KEY"),
        _read_config(config, "object_storage_api_key"),
    )
    secret_key = _pick_first(
        cli_secret_key,
        os.getenv("MINIO_SECRET_KEY"),
        os.getenv("OBJECT_STORAGE_SECRET_KEY"),
        _read_config(config, "object_storage_secret_key"),
    )
    if not secret_key:
        api_key, secret_key = _split_access_and_secret(api_key)
    channel = _pick_first(
        cli_channel,
        os.getenv("MINIO_BUCKET"),
        os.getenv("OBJECT_STORAGE_CHANNEL"),
        _read_config(config, "object_storage_channel", "default"),
        "default",
    )
    endpoint, endpoint_is_https = _parse_minio_endpoint(base_url)
    secure = parse_bool(
        _pick_first(
            os.getenv("MINIO_SECURE"),
            _read_config(config, "object_storage_secure"),
        ),
        default=endpoint_is_https,
    )
    public_base_url = _build_minio_public_base_url(
        _pick_first(
            cli_public_base_url,
            os.getenv("MINIO_PUBLIC_BASE_URL"),
            os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL"),
            _read_config(config, "object_storage_public_base_url"),
        ),
        endpoint=endpoint,
        secure=secure,
        bucket=channel,
    )
    return StorageSettings(
        enabled=True,
        storage_type="minio",
        base_url=endpoint,
        public_base_url=public_base_url,
        api_key=api_key,
        secret_key=secret_key,
        channel=channel,
        custom_filename=custom_filename,
        secure=secure,
    )


def create_storage_backend(settings: StorageSettings, output_dir: str, public_base_url: str) -> Optional[StorageBackend]:
    if not settings.enabled:
        return None

    if settings.storage_type == "local":
        if not _clean(public_base_url):
            raise RuntimeError("Local storage requires a resolvable public base URL.")
        return LocalStorageBackend(output_dir, public_base_url, settings.custom_filename)

    if settings.storage_type == "modal":
        if not settings.base_url:
            raise RuntimeError("Modal storage requires object_storage_base_url.")
        if not settings.api_key:
            raise RuntimeError("Modal storage requires object_storage_api_key.")
        if not settings.channel:
            raise RuntimeError("Modal storage requires object_storage_channel.")
        if not settings.public_base_url:
            raise RuntimeError("Modal storage requires object_storage_public_base_url.")
        return ModalStorageBackend(
            base_url=settings.base_url,
            api_key=settings.api_key,
            channel=settings.channel,
            public_base_url=settings.public_base_url,
            custom_filename=settings.custom_filename,
        )

    if settings.storage_type == "minio":
        if not settings.base_url:
            raise RuntimeError("MinIO storage requires endpoint in object_storage_base_url.")
        if not settings.api_key:
            raise RuntimeError("MinIO storage requires access key in object_storage_api_key.")
        if not settings.secret_key:
            raise RuntimeError("MinIO storage requires secret key in object_storage_secret_key.")
        if not settings.channel:
            raise RuntimeError("MinIO storage requires bucket in object_storage_channel.")
        if not settings.public_base_url:
            raise RuntimeError("MinIO storage requires object_storage_public_base_url or resolvable endpoint.")
        return MinioStorageBackend(
            endpoint=settings.base_url,
            access_key=settings.api_key,
            secret_key=settings.secret_key,
            bucket=settings.channel,
            secure=settings.secure,
            public_base_url=settings.public_base_url,
            custom_filename=settings.custom_filename,
        )

    return None
