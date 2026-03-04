import mimetypes
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import requests


VALID_STORAGE_TYPES = {"modal", "minio", "local", "cos", "auto"}
VALID_STORAGE_PROVIDERS = {"auto", "modal", "minio", "s3", "cos", "local"}
STORAGE_TYPE_ALIASES = {
    "tencent_cos": "cos",
    "tencent-cos": "cos",
    "aws_s3": "s3",
    "amazon_s3": "s3",
}


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


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


def _normalize_storage_type(value: str):
    raw = _clean(value).lower()
    if not raw:
        return ""
    return STORAGE_TYPE_ALIASES.get(raw, raw)


def _normalize_provider(provider: str):
    raw = _clean(provider).lower()
    if not raw:
        return "auto"
    aliases = {
        "tencent_cos": "cos",
        "tencent-cos": "cos",
        "qcloud_cos": "cos",
        "aws_s3": "s3",
        "amazon_s3": "s3",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in VALID_STORAGE_PROVIDERS:
        return "auto"
    return normalized


def _split_access_and_secret(value: str):
    raw = _clean(value)
    if not raw:
        return "", ""
    for sep in (":", "|", ","):
        if sep in raw:
            access_key, secret_key = raw.split(sep, 1)
            return access_key.strip(), secret_key.strip()
    return raw, ""


def _parse_object_storage_endpoint(endpoint_url: str):
    raw = _clean(endpoint_url)
    if not raw:
        return "", False, ""
    normalized = raw if "://" in raw else f"http://{raw}"
    parsed = urllib.parse.urlparse(normalized)
    endpoint = (parsed.netloc or parsed.path).strip().strip("/")
    path_hint = parsed.path.strip("/").split("/", 1)[0] if parsed.path and parsed.path.strip("/") else ""
    if "/" in endpoint:
        endpoint = endpoint.split("/", 1)[0]
    return endpoint, parsed.scheme == "https", path_hint


def _detect_object_storage_provider(endpoint: str, explicit_provider: str = "auto", storage_type_hint: str = ""):
    hint = _normalize_storage_type(storage_type_hint)
    if hint == "cos":
        return "cos"
    provider = _normalize_provider(explicit_provider)
    if provider != "auto":
        return provider
    host = _clean(endpoint).lower()
    if not host:
        return "minio"
    if ".myqcloud.com" in host or host.startswith("cos.") or ".cos." in host:
        return "cos"
    if "amazonaws.com" in host or host.startswith("s3.") or ".s3." in host:
        return "s3"
    return "minio"


def _is_probable_vendor_endpoint(host: str, provider: str):
    host_lower = _clean(host).lower()
    if not host_lower:
        return False
    if provider == "cos":
        return host_lower.startswith("cos.") or ".cos." in host_lower or host_lower.endswith(".myqcloud.com")
    if provider == "s3":
        return host_lower.startswith("s3.") or ".s3." in host_lower or "amazonaws.com" in host_lower
    return False


def _ensure_bucket_in_public_base_url(
    public_base_url: str,
    endpoint: str,
    secure: bool,
    bucket: str,
    provider: str,
):
    normalized_base = _clean(public_base_url).rstrip("/")
    normalized_bucket = _clean(bucket)
    normalized_provider = _normalize_provider(provider)
    if not normalized_base or not normalized_bucket or normalized_provider not in {"cos", "s3"}:
        return normalized_base

    default_scheme = "https" if secure else "http"
    parse_target = normalized_base if "://" in normalized_base else f"{default_scheme}://{normalized_base.lstrip('/')}"
    parsed = urllib.parse.urlparse(parse_target)
    host = (parsed.netloc or "").strip()
    if not host:
        return normalized_base

    bucket_lower = normalized_bucket.lower()
    if host.lower().startswith(f"{bucket_lower}."):
        return normalized_base

    path_segments = [segment for segment in parsed.path.split("/") if segment]
    if path_segments and path_segments[0].lower() == bucket_lower:
        return normalized_base

    endpoint_host, _, _ = _parse_object_storage_endpoint(endpoint)
    host_is_endpoint = bool(endpoint_host) and host.lower() == endpoint_host.lower()
    if not host_is_endpoint and not _is_probable_vendor_endpoint(host, normalized_provider):
        return normalized_base

    rebuilt = urllib.parse.urlunparse(
        (parsed.scheme or default_scheme, f"{normalized_bucket}.{host}", "", "", "", "")
    )
    return rebuilt.rstrip("/")


def _build_object_storage_public_base_url(
    public_base_url: str,
    endpoint: str,
    secure: bool,
    bucket: str,
    provider: str,
):
    base_url = _clean(public_base_url).rstrip("/")
    provider = _normalize_provider(provider)
    if base_url:
        return _ensure_bucket_in_public_base_url(
            public_base_url=base_url,
            endpoint=endpoint,
            secure=secure,
            bucket=bucket,
            provider=provider,
        )
    if not endpoint:
        return ""

    scheme = "https" if secure else "http"
    host = endpoint.rstrip("/")
    if provider in {"cos", "s3"}:
        bucket_lower = bucket.lower()
        if bucket and not host.lower().startswith(f"{bucket_lower}."):
            host = f"{bucket}.{host}"
        return f"{scheme}://{host}".rstrip("/")
    if bucket:
        return f"{scheme}://{host}/{bucket}".rstrip("/")
    return f"{scheme}://{host}".rstrip("/")


def _infer_extension(original_filename: str = "", content_type: str = "", fallback: str = ".bin"):
    filename = os.path.basename(_clean(original_filename))
    _, ext = os.path.splitext(filename)
    if ext:
        return ext.lower()
    mime = _clean(content_type).split(";", 1)[0].strip().lower()
    if mime:
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return guessed
    return fallback


def _build_object_name(custom_filename: str, counter: int, model_name: str, default_ext: str = ".png"):
    timestamp_ms = int(time.time() * 1000)
    custom_name = _clean(custom_filename)
    default_ext = default_ext if default_ext.startswith(".") else f".{default_ext}"
    default_ext = default_ext.lower()
    if custom_name:
        custom_name = os.path.basename(custom_name)
        base, ext = os.path.splitext(custom_name)
        safe_base = re.sub(r"[^a-zA-Z0-9_.-]", "_", base) or "file"
        normalized_ext = ext.lower()
        # Keep custom basename, but force the extension to match actual media type.
        if not normalized_ext or normalized_ext != default_ext:
            normalized_ext = default_ext
        return f"{safe_base}_{timestamp_ms}_{counter}{normalized_ext}"
    safe_model_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", model_name) or "workflow"
    return f"{safe_model_name}_{timestamp_ms}_{counter}{default_ext}"


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
    provider: str = "auto"
    region: str = ""


class StorageBackend:
    name = "storage"

    def upload(
        self,
        file_bytes: bytes,
        counter: int,
        model_name: str,
        content_type: str = "application/octet-stream",
        original_filename: str = "",
    ) -> str:
        raise NotImplementedError


class LocalStorageBackend(StorageBackend):
    name = "local"

    def __init__(self, output_dir: str, public_base_url: str, custom_filename: str = ""):
        self.output_dir = output_dir
        self.public_base_url = public_base_url.rstrip("/")
        self.custom_filename = custom_filename
        os.makedirs(self.output_dir, exist_ok=True)

    def upload(
        self,
        file_bytes: bytes,
        counter: int,
        model_name: str,
        content_type: str = "application/octet-stream",
        original_filename: str = "",
    ) -> str:
        ext = _infer_extension(original_filename, content_type, fallback=".bin")
        object_name = _build_object_name(self.custom_filename, counter, model_name, default_ext=ext)
        local_file_path = os.path.join(self.output_dir, object_name)
        with open(local_file_path, "wb") as f:
            f.write(file_bytes)
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

    def upload(
        self,
        file_bytes: bytes,
        counter: int,
        model_name: str,
        content_type: str = "application/octet-stream",
        original_filename: str = "",
    ) -> str:
        ext = _infer_extension(original_filename, content_type, fallback=".bin")
        filename = _build_object_name(self.custom_filename, counter, model_name, default_ext=ext)
        upload_url = f"{self.base_url}/api/files/upload"
        headers = {"X-API-Key": self.api_key, "X-Channel": self.channel}
        files = {"file": (filename, file_bytes, content_type or "application/octet-stream")}
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
        backend_name: str = "minio",
    ):
        self.name = backend_name
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.secure = secure
        self.public_base_url = public_base_url.rstrip("/")
        self.custom_filename = custom_filename

    def upload(
        self,
        file_bytes: bytes,
        counter: int,
        model_name: str,
        content_type: str = "application/octet-stream",
        original_filename: str = "",
    ) -> str:
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError("Missing dependency: minio. Please install it first.") from exc

        ext = _infer_extension(original_filename, content_type, fallback=".bin")
        object_name = _build_object_name(self.custom_filename, counter, model_name, default_ext=ext)
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
        data_stream = BytesIO(file_bytes)
        client.put_object(
            self.bucket,
            object_name,
            data_stream,
            length=len(file_bytes),
            content_type=content_type or "application/octet-stream",
        )
        quoted_name = urllib.parse.quote(object_name, safe="/")
        return f"{self.public_base_url}/{quoted_name}"


class S3CompatibleStorageBackend(StorageBackend):
    name = "s3"

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
        public_base_url: str,
        custom_filename: str = "",
        region: str = "",
        addressing_style: str = "auto",
        backend_name: str = "s3",
    ):
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.secure = secure
        self.public_base_url = public_base_url.rstrip("/")
        self.custom_filename = custom_filename
        self.region = region
        self.addressing_style = addressing_style
        self.name = backend_name

    def upload(
        self,
        file_bytes: bytes,
        counter: int,
        model_name: str,
        content_type: str = "application/octet-stream",
        original_filename: str = "",
    ) -> str:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:
            raise RuntimeError("Missing dependency: boto3. Please install it first.") from exc

        endpoint_url = self.endpoint
        if "://" not in endpoint_url:
            scheme = "https" if self.secure else "http"
            endpoint_url = f"{scheme}://{endpoint_url}"

        session = boto3.session.Session()
        s3_config = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": self.addressing_style},
        )
        client = session.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region or None,
            config=s3_config,
        )

        ext = _infer_extension(original_filename, content_type, fallback=".bin")
        object_name = _build_object_name(self.custom_filename, counter, model_name, default_ext=ext)
        client.put_object(
            Bucket=self.bucket,
            Key=object_name,
            Body=file_bytes,
            ContentType=content_type or "application/octet-stream",
        )
        quoted_name = urllib.parse.quote(object_name, safe="/")
        return f"{self.public_base_url}/{quoted_name}"


class COSStorageBackend(S3CompatibleStorageBackend):
    name = "cos"

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
        public_base_url: str,
        custom_filename: str = "",
        region: str = "",
    ):
        super().__init__(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            secure=secure,
            public_base_url=public_base_url,
            custom_filename=custom_filename,
            region=region,
            addressing_style="virtual",
            backend_name="cos",
        )


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
    cli_provider=None,
    cli_region=None,
    cli_secure=None,
):
    storage_type_raw = _normalize_storage_type(
        _pick_first(
            cli_type,
            os.getenv("OBJECT_STORAGE_TYPE"),
            _read_config(config, "object_storage_type"),
        )
    )
    provider = _normalize_provider(
        _pick_first(
            cli_provider,
            os.getenv("OBJECT_STORAGE_PROVIDER"),
            _read_config(config, "object_storage_provider"),
            "auto",
        )
    )

    enabled_raw = cli_enabled
    if enabled_raw is None:
        enabled_raw = _pick_first(
            os.getenv("OBJECT_STORAGE_ENABLED"),
            _read_config(config, "object_storage_enabled"),
        )
    enabled_flag = parse_bool(enabled_raw, default=False)
    explicit_type = bool(storage_type_raw)
    explicit_provider = provider != "auto"
    enabled = enabled_flag or explicit_type or explicit_provider
    custom_filename = _pick_first(
        cli_custom_filename,
        os.getenv("OBJECT_STORAGE_CUSTOM_FILENAME"),
        _read_config(config, "object_storage_custom_filename"),
    )

    base_url_hint = _pick_first(
        cli_base_url,
        os.getenv("MINIO_ENDPOINT"),
        os.getenv("COS_ENDPOINT"),
        os.getenv("OBJECT_STORAGE_BASE_URL"),
        _read_config(config, "object_storage_base_url"),
        os.getenv("MODAL_OBJECT_STORAGE_BASE_URL"),
    )
    if storage_type_raw in {"", "auto"}:
        if provider in {"local", "modal"}:
            resolved_type = provider
        elif provider in {"minio", "s3", "cos"} or _clean(base_url_hint):
            resolved_type = "minio"
        else:
            resolved_type = "local"
    else:
        resolved_type = storage_type_raw

    if not enabled:
        return StorageSettings(
            enabled=False,
            storage_type="none",
            custom_filename=custom_filename,
            provider=provider,
        )
    if resolved_type not in VALID_STORAGE_TYPES:
        raise RuntimeError(
            f"Unsupported object storage type '{resolved_type}'. Use one of: auto, modal, minio, cos, local."
        )

    if resolved_type == "local":
        return StorageSettings(
            enabled=True,
            storage_type="local",
            custom_filename=custom_filename,
            provider="local",
        )

    if resolved_type == "modal":
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
            provider="modal",
        )

    base_url = _pick_first(
        cli_base_url,
        os.getenv("MINIO_ENDPOINT"),
        os.getenv("COS_ENDPOINT"),
        os.getenv("OBJECT_STORAGE_BASE_URL"),
        _read_config(config, "object_storage_base_url"),
    )
    api_key = _pick_first(
        cli_api_key,
        os.getenv("MINIO_ACCESS_KEY"),
        os.getenv("COS_SECRET_ID"),
        os.getenv("OBJECT_STORAGE_API_KEY"),
        _read_config(config, "object_storage_api_key"),
    )
    secret_key = _pick_first(
        cli_secret_key,
        os.getenv("MINIO_SECRET_KEY"),
        os.getenv("COS_SECRET_KEY"),
        os.getenv("OBJECT_STORAGE_SECRET_KEY"),
        _read_config(config, "object_storage_secret_key"),
    )
    if not secret_key:
        api_key, secret_key = _split_access_and_secret(api_key)

    endpoint, endpoint_is_https, path_bucket_hint = _parse_object_storage_endpoint(base_url)
    resolved_provider = _detect_object_storage_provider(
        endpoint,
        explicit_provider=provider,
        storage_type_hint=resolved_type,
    )
    channel = _pick_first(
        cli_channel,
        os.getenv("MINIO_BUCKET"),
        os.getenv("COS_BUCKET"),
        os.getenv("OBJECT_STORAGE_CHANNEL"),
        _read_config(config, "object_storage_channel", "default"),
        path_bucket_hint,
        "default",
    )
    secure_default = endpoint_is_https or resolved_provider in {"cos", "s3"}
    secure = parse_bool(
        _pick_first(
            cli_secure,
            os.getenv("MINIO_SECURE"),
            os.getenv("OBJECT_STORAGE_SECURE"),
            _read_config(config, "object_storage_secure"),
        ),
        default=secure_default,
    )
    region = _pick_first(
        cli_region,
        os.getenv("OBJECT_STORAGE_REGION"),
        os.getenv("MINIO_REGION"),
        os.getenv("AWS_REGION"),
        os.getenv("COS_REGION"),
        _read_config(config, "object_storage_region"),
    )
    public_base_url = _build_object_storage_public_base_url(
        _pick_first(
            cli_public_base_url,
            os.getenv("MINIO_PUBLIC_BASE_URL"),
            os.getenv("COS_PUBLIC_BASE_URL"),
            os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL"),
            _read_config(config, "object_storage_public_base_url"),
        ),
        endpoint=endpoint,
        secure=secure,
        bucket=channel,
        provider=resolved_provider,
    )
    return StorageSettings(
        enabled=True,
        storage_type="cos" if resolved_type == "cos" else "minio",
        base_url=endpoint,
        public_base_url=public_base_url,
        api_key=api_key,
        secret_key=secret_key,
        channel=channel,
        custom_filename=custom_filename,
        secure=secure,
        provider=resolved_provider,
        region=region,
    )


def create_storage_backend(settings: StorageSettings, output_dir: str, public_base_url: str) -> Optional[StorageBackend]:
    if not settings.enabled:
        return None

    if settings.storage_type == "local":
        effective_public_base_url = _clean(public_base_url).rstrip("/")
        if not effective_public_base_url:
            raise RuntimeError("Local storage requires a resolvable public base URL.")
        return LocalStorageBackend(output_dir, effective_public_base_url, settings.custom_filename)

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

    if settings.storage_type in {"minio", "cos"}:
        if not settings.base_url:
            raise RuntimeError("Object storage requires endpoint in object_storage_base_url.")
        if not settings.api_key:
            raise RuntimeError("Object storage requires access key in object_storage_api_key.")
        if not settings.secret_key:
            raise RuntimeError("Object storage requires secret key in object_storage_secret_key.")
        if not settings.channel:
            raise RuntimeError("Object storage requires bucket in object_storage_channel.")
        if not settings.public_base_url:
            raise RuntimeError("Object storage requires object_storage_public_base_url or resolvable endpoint.")

        provider = _detect_object_storage_provider(
            settings.base_url,
            explicit_provider=settings.provider,
            storage_type_hint=settings.storage_type,
        )
        if provider == "cos":
            return COSStorageBackend(
                endpoint=settings.base_url,
                access_key=settings.api_key,
                secret_key=settings.secret_key,
                bucket=settings.channel,
                secure=settings.secure,
                public_base_url=settings.public_base_url,
                custom_filename=settings.custom_filename,
                region=settings.region,
            )
        if provider == "s3":
            return S3CompatibleStorageBackend(
                endpoint=settings.base_url,
                access_key=settings.api_key,
                secret_key=settings.secret_key,
                bucket=settings.channel,
                secure=settings.secure,
                public_base_url=settings.public_base_url,
                custom_filename=settings.custom_filename,
                region=settings.region,
                addressing_style="virtual",
                backend_name="s3",
            )
        return MinioStorageBackend(
            endpoint=settings.base_url,
            access_key=settings.api_key,
            secret_key=settings.secret_key,
            bucket=settings.channel,
            secure=settings.secure,
            public_base_url=settings.public_base_url,
            custom_filename=settings.custom_filename,
            backend_name=settings.storage_type or "minio",
        )

    return None
