import argparse
import base64
import configparser
import json
import mimetypes
import os
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Union

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
import httpx
import requests
import websocket
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel
from storage_backends import create_storage_backend, load_storage_settings, parse_bool
from workflow_parameters import apply_workflow_parameters, normalize_workflow_parameters
import asyncio
parser = argparse.ArgumentParser(description="Run the server with specified host and port.")
parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address to bind the server.")
parser.add_argument("--port", type=int, default=8187, help="Port number to bind the server.")
parser.add_argument("--public-base-url", type=str, default=None, help="Public base URL for image links.")
parser.add_argument("--object-storage-enabled", type=str, default=None, help="Enable external storage: true/false.")
parser.add_argument("--object-storage-type", type=str, default=None, help="Storage backend type: modal|minio|cos|local.")
parser.add_argument("--object-storage-base-url", type=str, default=None, help="Storage base URL / endpoint.")
parser.add_argument("--object-storage-public-base-url", type=str, default=None, help="Public storage URL.")
parser.add_argument("--object-storage-api-key", type=str, default=None, help="Storage API key / access key.")
parser.add_argument("--object-storage-secret-key", type=str, default=None, help="Storage secret key for MinIO.")
parser.add_argument("--object-storage-channel", type=str, default=None, help="Storage channel (Modal) / bucket (MinIO).")
parser.add_argument("--object-storage-custom-filename", type=str, default=None, help="Object storage custom filename.")
parser.add_argument(
    "--object-storage-provider",
    type=str,
    default=None,
    help="Storage provider strategy: auto|minio|cos|s3|modal|local.",
)
parser.add_argument(
    "--object-storage-region",
    type=str,
    default=None,
    help="Optional object storage region (for COS/S3 compatible backends).",
)
parser.add_argument(
    "--object-storage-default-profile",
    type=str,
    default=None,
    help="Default object storage profile id used when request does not specify storage_profile.",
)

args = parser.parse_args()
current_dir_path = os.path.dirname(os.path.realpath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(current_dir_path, "config.ini"))
if parse_bool(args.object_storage_enabled, default=False):
    configured_type = (
        (args.object_storage_type or "").strip()
    )
    configured_provider = (
        (args.object_storage_provider or "").strip()
    )
    configured_base_url = (
        (args.object_storage_base_url or "").strip()
    )
    if not configured_type and not configured_provider and not configured_base_url:
        parser.error(
            "--object-storage-type or --object-storage-provider or --object-storage-base-url is required when --object-storage-enabled=true"
        )
# 获取配置文件中的参数
fastapi_api_key = config.get("API_KEYS", "fastapi_api_key", fallback="")
server_address = "127.0.0.1:8188"
client_id = str(uuid.uuid4())
FASTAPI_BUILD_TAG = os.getenv("FASTAPI_BUILD_TAG", "unversioned").strip() or "unversioned"
FASTAPI_BUILD_COMMIT = os.getenv("FASTAPI_BUILD_COMMIT", "unknown").strip() or "unknown"


def _get_stream_heartbeat_seconds():
    raw_value = os.getenv("FASTAPI_STREAM_HEARTBEAT_SEC", "10").strip()
    try:
        heartbeat_seconds = float(raw_value)
    except ValueError:
        heartbeat_seconds = 10.0
    return max(1.0, heartbeat_seconds)


def _get_workflow_poll_timeout_seconds():
    raw_value = os.getenv("COMFYUI_WORKFLOW_TIMEOUT_SEC", "3600").strip()
    try:
        timeout_seconds = int(raw_value)
    except ValueError:
        timeout_seconds = 3600
    return max(30, min(timeout_seconds, 86400))


def _get_workflow_poll_interval_seconds():
    raw_value = os.getenv("COMFYUI_HISTORY_POLL_INTERVAL_SEC", "0.25").strip()
    try:
        poll_interval = float(raw_value)
    except ValueError:
        poll_interval = 0.25
    return max(0.1, min(poll_interval, 5.0))


def _get_comfyui_http_timeout_seconds():
    raw_value = os.getenv("COMFYUI_HTTP_TIMEOUT_SEC", "30").strip()
    try:
        timeout_seconds = float(raw_value)
    except ValueError:
        timeout_seconds = 30.0
    return max(1.0, min(timeout_seconds, 300.0))


STREAM_HEARTBEAT_SECONDS = _get_stream_heartbeat_seconds()
ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]
VIDEO_FILE_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".m4v",
    ".wmv",
    ".flv",
}
URL_REGEX = re.compile(r"https?://[^\s)>\"]+")


def _model_to_dict(model_obj):
    # Compatibility for both Pydantic v1 and v2.
    if hasattr(model_obj, "model_dump"):
        return model_obj.model_dump()
    if hasattr(model_obj, "dict"):
        return model_obj.dict()
    return model_obj


def _json_dumps_for_log(value):
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


_LOG_SENSITIVE_KEYS = {
    "authorization",
    "proxy_authorization",
    "cookie",
    "api_key",
    "apikey",
    "x_api_key",
    "access_token",
    "token",
    "secret",
    "secret_key",
    "password",
    "ms_token",
    "x_bogus",
    "x_gnarly",
}
_LOG_PRIVATE_KEYS = {
    "messages",
    "content",
    "file_content",
    "image_input",
    "image_url",
    "image_urls",
    "video_url",
    "video_urls",
    "media",
    "file_urls",
    "references",
    "system_prompt",
    "user_prompt",
    "positive_prompt",
    "negative_prompt",
}


def _redact_log_value(value, key=""):
    normalized_key = str(key).strip().lower().replace("-", "_")
    if normalized_key in _LOG_SENSITIVE_KEYS:
        return "<redacted>"
    if normalized_key in _LOG_PRIVATE_KEYS:
        if isinstance(value, dict):
            return {"redacted": True, "keys": sorted(str(item) for item in value)}
        if isinstance(value, (list, tuple)):
            return {"redacted": True, "count": len(value)}
        return {"redacted": True, "chars": len(str(value or ""))}
    if isinstance(value, dict):
        return {str(item_key): _redact_log_value(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 20:
            return {
                "items": [_redact_log_value(item) for item in value[:20]],
                "truncated_count": len(value) - 20,
            }
        return [_redact_log_value(item) for item in value]
    if isinstance(value, str) and len(value) > 512:
        return f"<truncated:{len(value)} chars>"
    return value


def _is_json_serializable(value):
    try:
        json.dumps(value)
        return True
    except Exception:
        return False


def _log_request(stage: str, request_id: str, payload):
    print(f"[FASTAPI][{request_id}][{stage}] {_json_dumps_for_log(_redact_log_value(payload))}")


def _exception_log_payload(exc: Exception):
    payload = _exception_summary_payload(exc)
    payload["traceback_tail"] = traceback.format_exc().splitlines()[-20:]
    return payload


def _exception_summary_payload(exc: Exception):
    payload = {
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    detail = getattr(exc, "detail", None)
    if detail is not None:
        payload["detail"] = detail
        payload["error"] = detail if isinstance(detail, str) else _json_dumps_for_log(detail)
    if isinstance(exc, urllib.error.HTTPError):
        payload["status_code"] = exc.code
        try:
            response_body = exc.read(8192)
        except Exception as read_error:
            payload["response_read_error"] = str(read_error)
        else:
            if response_body:
                payload["response_body"] = _safe_preview(
                    response_body.decode("utf-8", errors="replace"),
                    4096,
                )
    return payload


class ComfyUIWorkflowError(RuntimeError):
    pass


def _history_error_details(history_entry):
    if not isinstance(history_entry, dict):
        return None

    status = history_entry.get("status")
    if not isinstance(status, dict):
        return None

    status_text = str(status.get("status_str", "")).strip().lower()
    messages = status.get("messages")
    details = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, (list, tuple)) or not message:
                continue
            message_type = str(message[0]).strip()
            if message_type not in {"execution_error", "execution_interrupted"}:
                continue
            payload = message[1] if len(message) > 1 else None
            details.append({"type": message_type, "detail": payload})

    if status_text not in {"error", "failed", "failure", "interrupted"} and not details:
        return None
    return {
        "status": status_text or "unknown",
        "completed": bool(status.get("completed", False)),
        "details": details[-10:],
    }


def _raise_history_error(history_entry, request_id="", prompt_id=""):
    details = _history_error_details(history_entry)
    if not details:
        return
    if prompt_id:
        details["prompt_id"] = prompt_id
    _log_request("workflow_execution_error", request_id, details)
    raise ComfyUIWorkflowError("ComfyUI workflow failed: " + _safe_preview(_json_dumps_for_log(details), 4096))


def _safe_preview(value: str, max_len: int = 160):
    raw = str(value or "")
    if len(raw) <= max_len:
        return raw
    return raw[:max_len] + "...(truncated)"


def _log_prompt_texts(
    request_id: str,
    workflow_path: str,
    system_prompt: str,
    user_prompt: str,
    positive_prompt: str,
    negative_prompt: str,
):
    print(f"[FASTAPI][{request_id}][PROMPT][workflow] {workflow_path}")
    for name, value in (
        ("system", system_prompt),
        ("user", user_prompt),
        ("positive", positive_prompt),
        ("negative", negative_prompt),
    ):
        print(f"[FASTAPI][{request_id}][PROMPT][{name}] chars={len(str(value or ''))}")

def resolve_public_base_url(request: Optional[Request] = None):
    cli_public_base_url = (args.public_base_url or "").strip()
    env_public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    conf_public_base_url = config.get("API_KEYS", "public_base_url", fallback="").strip()
    public_base_url = cli_public_base_url or env_public_base_url or conf_public_base_url
    if public_base_url:
        return public_base_url.rstrip("/")

    if request is not None:
        proto = request.headers.get("x-forwarded-proto")
        host = request.headers.get("x-forwarded-host")
        if host:
            return f"{(proto or request.url.scheme)}://{host}".rstrip("/")
        return str(request.base_url).rstrip("/")

    return f"http://{args.host}:{args.port}"


def _pick_first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        cleaned = str(value).strip()
        if cleaned:
            return cleaned
    return ""


def _mask_secret(value: str):
    raw = (value or "").strip()
    if not raw:
        return "<empty>"
    if len(raw) <= 6:
        return "*" * len(raw)
    return f"{raw[:3]}***({len(raw)})"


def _normalize_storage_profile(raw_profile: Dict[str, Any]):
    if not isinstance(raw_profile, dict):
        return {}

    def pick(*keys):
        for key in keys:
            if key not in raw_profile:
                continue
            value = raw_profile.get(key)
            if value is None:
                continue
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
        return ""

    normalized = {
        "type": pick("object_storage_type", "type"),
        "enabled": pick("object_storage_enabled", "enabled"),
        "provider": pick("object_storage_provider", "provider"),
        "region": pick("object_storage_region", "region"),
        "base_url": pick("object_storage_base_url", "base_url"),
        "public_base_url": pick("object_storage_public_base_url", "public_base_url"),
        "api_key": pick("object_storage_api_key", "api_key"),
        "secret_key": pick("object_storage_secret_key", "secret_key"),
        "channel": pick("object_storage_channel", "channel"),
        "custom_filename": pick("object_storage_custom_filename", "custom_filename"),
        "secure": pick("object_storage_secure", "secure"),
    }
    return {k: v for k, v in normalized.items() if v != ""}


def _load_storage_profiles_from_config(runtime_config: configparser.ConfigParser):
    profiles = {}
    if runtime_config is None:
        return profiles
    for section in runtime_config.sections():
        if not section.upper().startswith("OBJECT_STORAGE_PROFILE."):
            continue
        profile_id = section.split(".", 1)[1].strip()
        if not profile_id:
            continue
        profiles[profile_id] = _normalize_storage_profile(dict(runtime_config[section]))
    return profiles


def _load_storage_profiles_from_env():
    profiles_json_raw = os.getenv("OBJECT_STORAGE_PROFILES_JSON", "").strip()
    if not profiles_json_raw:
        return {}, ""
    try:
        parsed = json.loads(profiles_json_raw)
    except Exception as parse_err:
        print(f"[FASTAPI][storage_profiles] OBJECT_STORAGE_PROFILES_JSON parse failed: {parse_err}")
        return {}, ""

    default_profile = ""
    profile_payload = {}
    if isinstance(parsed, dict):
        if isinstance(parsed.get("profiles"), dict):
            profile_payload = parsed.get("profiles", {})
            default_profile = _pick_first_non_empty(parsed.get("default_profile"), parsed.get("default"))
        else:
            profile_payload = {k: v for k, v in parsed.items() if isinstance(v, dict)}

    normalized_profiles = {}
    for profile_id, profile_data in profile_payload.items():
        profile_name = str(profile_id).strip()
        if not profile_name:
            continue
        normalized = _normalize_storage_profile(profile_data)
        if normalized:
            normalized_profiles[profile_name] = normalized
    return normalized_profiles, default_profile


def resolve_storage_profile(
    request_data,
    request: Optional[Request],
    runtime_config: configparser.ConfigParser,
    request_id: str = "",
):
    header_profile = ""
    if request is not None:
        header_profile = _pick_first_non_empty(
            request.headers.get("x-storage-profile"),
            request.headers.get("x-object-storage-profile"),
        )

    request_profile = _pick_first_non_empty(getattr(request_data, "storage_profile", ""))
    env_default_profile = _pick_first_non_empty(
        args.object_storage_default_profile,
        os.getenv("OBJECT_STORAGE_CONFIG_ID"),
        os.getenv("OBJECT_STORAGE_DEFAULT_PROFILE"),
    )
    conf_default_profile = ""
    if runtime_config is not None and runtime_config.has_section("API_KEYS"):
        conf_default_profile = runtime_config.get("API_KEYS", "object_storage_default_profile", fallback="").strip()

    env_profiles, env_profile_default = _load_storage_profiles_from_env()
    config_profiles = _load_storage_profiles_from_config(runtime_config)
    # Environment profiles override config profiles with the same id.
    profiles = {**config_profiles, **env_profiles}

    selected_profile = _pick_first_non_empty(
        request_profile,
        header_profile,
        env_default_profile,
        env_profile_default,
        conf_default_profile,
    )

    overrides = {}
    if selected_profile:
        overrides = profiles.get(selected_profile, {})
        _log_request(
            "storage_profile_selected",
            request_id,
            {
                "requested_profile": selected_profile,
                "from_request_body": request_profile,
                "from_header": header_profile,
                "resolved": bool(overrides),
                "available_profiles": sorted(profiles.keys()),
            },
        )
    return selected_profile, overrides


def resolve_storage_settings(
    runtime_config: configparser.ConfigParser,
    request_data,
    request: Optional[Request],
    request_id: str = "",
):
    storage_settings = load_storage_settings(
        runtime_config,
        cli_type=args.object_storage_type,
        cli_enabled=args.object_storage_enabled,
        cli_provider=args.object_storage_provider,
        cli_region=args.object_storage_region,
        cli_base_url=args.object_storage_base_url,
        cli_public_base_url=args.object_storage_public_base_url,
        cli_api_key=args.object_storage_api_key,
        cli_secret_key=args.object_storage_secret_key,
        cli_channel=args.object_storage_channel,
        cli_custom_filename=args.object_storage_custom_filename,
        cli_secure=None,
        cli_only=True,
    )
    profile_id = ""

    _log_request(
        "storage_settings_resolved",
        request_id,
        {
            "profile_id": profile_id,
            "storage_type": storage_settings.storage_type,
            "provider": getattr(storage_settings, "provider", ""),
            "region": getattr(storage_settings, "region", ""),
            "enabled": storage_settings.enabled,
            "channel": storage_settings.channel,
            "base_url": storage_settings.base_url,
            "public_base_url": storage_settings.public_base_url,
            "api_key": _mask_secret(storage_settings.api_key),
            "secret_key": _mask_secret(storage_settings.secret_key),
            "secure": storage_settings.secure,
            "custom_filename": storage_settings.custom_filename,
        },
    )
    return storage_settings, profile_id


def guess_media_content_type(filename: str = "", format_hint: str = "", media_kind: str = ""):
    format_clean = (format_hint or "").strip()
    if "/" in format_clean:
        return format_clean.split(";", 1)[0].strip().lower()
    guessed_type, _ = mimetypes.guess_type(filename or "")
    if guessed_type:
        return guessed_type
    if media_kind == "image":
        return "image/png"
    if media_kind == "video":
        return "video/mp4"
    return "application/octet-stream"


def infer_media_kind(filename: str = "", format_hint: str = "", default_kind: str = "image"):
    content_type = guess_media_content_type(
        filename=filename,
        format_hint=format_hint,
        media_kind=default_kind,
    )
    ext = os.path.splitext((filename or "").lower())[1]
    if content_type.startswith("video/") or ext in VIDEO_FILE_EXTENSIONS:
        return "video", content_type
    if content_type.startswith("image/"):
        return "image", content_type
    if default_kind == "video":
        return "video", content_type
    return "image", content_type


def extract_urls_from_text(text: str):
    raw_text = str(text or "")
    matches = URL_REGEX.findall(raw_text)
    deduped = []
    seen = set()
    for item in matches:
        candidate = item.rstrip(".,;:!?")
        if candidate and candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def is_probable_video_url(url: str):
    candidate = str(url or "").strip()
    if not candidate:
        return False
    parsed = urllib.parse.urlparse(candidate)
    path = (parsed.path or "").lower()
    ext = os.path.splitext(path)[1]
    if ext in VIDEO_FILE_EXTENSIONS:
        return True
    # Fallback for common video-style links without file extension.
    lowered = candidate.lower()
    return any(marker in lowered for marker in ("/video", "video=", "media_type=video"))


def is_probable_image_url(url: str):
    candidate = str(url or "").strip()
    if not candidate:
        return False
    parsed = urllib.parse.urlparse(candidate)
    path = (parsed.path or "").lower()
    ext = os.path.splitext(path)[1]
    return ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def build_local_filename(counter: int, filename_hint: str, content_type: str, media_kind: str):
    _, ext = os.path.splitext(filename_hint or "")
    if not ext:
        ext = mimetypes.guess_extension(content_type or "") or ""
    if not ext:
        ext = ".png" if media_kind == "image" else ".mp4" if media_kind == "video" else ".bin"
    timestamp = int(time.time() * 1000)
    return f"{timestamp}_{counter}{ext}"


def _probe_video_asset(file_bytes: bytes):
    if not file_bytes:
        return {}

    try:
        import av

        with av.open(BytesIO(file_bytes), mode="r") as container:
            video_stream = next(
                (stream for stream in container.streams if stream.type == "video"),
                None,
            )
            if video_stream is None:
                return {"video_probe_error": "no video stream"}

            metadata = {
                "video_duration_seconds": None,
                "video_frame_count": int(video_stream.frames or 0),
                "video_fps": None,
            }
            if container.duration is not None:
                metadata["video_duration_seconds"] = round(
                    float(container.duration / av.time_base),
                    6,
                )
            if video_stream.average_rate:
                metadata["video_fps"] = round(float(video_stream.average_rate), 6)
            if metadata["video_frame_count"] <= 0:
                metadata["video_frame_count"] = sum(
                    1 for _ in container.decode(video_stream)
                )
            if metadata["video_duration_seconds"] is None and metadata["video_fps"]:
                metadata["video_duration_seconds"] = round(
                    metadata["video_frame_count"] / metadata["video_fps"],
                    6,
                )
            return metadata
    except Exception as probe_error:
        return {
            "video_probe_error": f"{type(probe_error).__name__}: {probe_error}",
        }


def queue_prompt(prompt, workflow_client_id: Optional[str] = None, timeout: Optional[float] = None):
    p = {"prompt": prompt, "client_id": workflow_client_id or client_id}
    data = json.dumps(p).encode("utf-8")
    req = urllib.request.Request("http://{}/prompt".format(server_address), data=data)
    with urllib.request.urlopen(req, timeout=timeout or _get_comfyui_http_timeout_seconds()) as response:
        return json.loads(response.read())


def get_asset_bytes(filename, subfolder, folder_type, timeout: Optional[float] = None):
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(
        "http://{}/view?{}".format(server_address, url_values),
        timeout=timeout or _get_comfyui_http_timeout_seconds(),
    ) as response:
        return response.read()


def get_image(filename, subfolder, folder_type):
    return get_asset_bytes(filename, subfolder, folder_type)


def get_history(prompt_id, timeout: Optional[float] = None):
    with urllib.request.urlopen(
        "http://{}/history/{}".format(server_address, prompt_id),
        timeout=timeout or _get_comfyui_http_timeout_seconds(),
    ) as response:
        return json.loads(response.read())


def _emit_comfyui_progress(
    state: Dict[str, Any],
    prompt_id: str,
    callback: ProgressCallback,
    *,
    phase: str,
    value: Optional[float] = None,
    maximum: Optional[float] = None,
    node: Optional[str] = None,
    queue_remaining: Optional[int] = None,
    force: bool = False,
):
    if callback is None:
        return

    if value is not None:
        try:
            state["value"] = max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    if maximum is not None:
        try:
            parsed_maximum = float(maximum)
            if parsed_maximum > 0:
                state["max"] = parsed_maximum
        except (TypeError, ValueError):
            pass
    if node is not None:
        state["node"] = str(node)

    total_nodes = max(int(state.get("total_nodes", 1)), 1)
    completed_nodes = state["completed_nodes"]
    current_max = max(float(state.get("max", 1.0)), 1.0)
    current_value = min(max(float(state.get("value", 0.0)), 0.0), current_max)
    current_fraction = current_value / current_max
    if phase == "queued":
        percent = 0
    elif phase == "completed":
        percent = 100
    else:
        percent = round(((len(completed_nodes) + current_fraction) / total_nodes) * 100)
        percent = max(0, min(99, percent))

    if not force and percent == state.get("last_percent"):
        return
    state["last_percent"] = percent

    payload = {
        "percent": percent,
        "value": state.get("value", 0.0),
        "max": state.get("max", 1.0),
        "prompt_id": prompt_id,
        "node": state.get("node"),
        "phase": phase,
        "completed_nodes": len(completed_nodes),
        "total_nodes": total_nodes,
    }
    if queue_remaining is not None:
        payload["queue_remaining"] = queue_remaining
    callback(payload)


def _consume_comfyui_progress_message(
    raw_message: Any,
    prompt_id: str,
    state: Dict[str, Any],
    callback: ProgressCallback,
):
    if not isinstance(raw_message, str) or not raw_message:
        return
    try:
        message = json.loads(raw_message)
    except (TypeError, ValueError):
        return

    event_type = message.get("type")
    data = message.get("data")
    if not isinstance(data, dict):
        return
    event_prompt_id = data.get("prompt_id")
    if event_prompt_id and event_prompt_id != prompt_id:
        return

    if event_type == "status":
        status = data.get("status") if isinstance(data.get("status"), dict) else {}
        exec_info = status.get("exec_info") if isinstance(status.get("exec_info"), dict) else {}
        queue_remaining = exec_info.get("queue_remaining")
        if isinstance(queue_remaining, (int, float)):
            _emit_comfyui_progress(
                state,
                prompt_id,
                callback,
                phase="queued",
                queue_remaining=int(queue_remaining),
                force=True,
            )
        return

    if event_type == "execution_start":
        _emit_comfyui_progress(state, prompt_id, callback, phase="starting", force=True)
        return

    if event_type == "execution_cached":
        cached_nodes = data.get("nodes")
        if isinstance(cached_nodes, list):
            state["completed_nodes"].update(str(node_id) for node_id in cached_nodes)
        _emit_comfyui_progress(state, prompt_id, callback, phase="running", force=True)
        return

    if event_type == "progress":
        _emit_comfyui_progress(
            state,
            prompt_id,
            callback,
            phase="sampling",
            value=data.get("value"),
            maximum=data.get("max"),
            node=data.get("node"),
        )
        return

    if event_type == "progress_state":
        nodes = data.get("nodes")
        if isinstance(nodes, dict):
            for node_id, node_state in nodes.items():
                if not isinstance(node_state, dict):
                    continue
                state_name = str(node_state.get("state", ""))
                value = node_state.get("value", 0)
                maximum = node_state.get("max", 1)
                if state_name == "finished" or (
                    isinstance(value, (int, float))
                    and isinstance(maximum, (int, float))
                    and maximum > 0
                    and value >= maximum
                ):
                    state["completed_nodes"].add(str(node_id))
                elif state_name == "running":
                    state["node"] = str(node_id)
                    state["value"] = value
                    state["max"] = maximum
        _emit_comfyui_progress(state, prompt_id, callback, phase="running")
        return

    if event_type == "executed":
        node_id = data.get("node")
        if node_id is not None:
            state["completed_nodes"].add(str(node_id))
        _emit_comfyui_progress(state, prompt_id, callback, phase="running", force=True)
        return

    if event_type == "executing":
        node_id = data.get("node")
        if node_id is None:
            _emit_comfyui_progress(state, prompt_id, callback, phase="finishing", force=True)
        else:
            state["node"] = str(node_id)
            state["value"] = 0.0
            state["max"] = 1.0
            _emit_comfyui_progress(state, prompt_id, callback, phase="node", force=True)


def get_all(prompt, request_id: str = "", progress_callback: ProgressCallback = None):
    workflow_client_id = str(uuid.uuid4())
    progress_socket = None
    poll_started_at = time.monotonic()
    poll_timeout_seconds = _get_workflow_poll_timeout_seconds()
    poll_interval_seconds = _get_workflow_poll_interval_seconds()
    http_timeout_seconds = _get_comfyui_http_timeout_seconds()
    poll_failures = 0
    try:
        progress_socket = websocket.create_connection(
            "ws://{}/ws?clientId={}".format(server_address, workflow_client_id),
            timeout=5,
        )
        progress_socket.settimeout(0.2)
    except Exception as ws_error:
        _log_request("progress_socket_error", request_id, {"error": str(ws_error)})
        progress_socket = None

    try:
        prompt_id = queue_prompt(
            prompt,
            workflow_client_id,
            timeout=http_timeout_seconds,
        )["prompt_id"]
        _log_request(
            "workflow_prompt_queued",
            request_id,
            {
                "prompt_id": prompt_id,
                "node_count": len(prompt) if isinstance(prompt, dict) else None,
            },
        )
    except Exception:
        if progress_socket is not None:
            progress_socket.close()
        raise

    progress_state = {
        "completed_nodes": set(),
        "total_nodes": max(len(prompt), 1) if isinstance(prompt, dict) else 1,
        "value": 0.0,
        "max": 1.0,
        "node": None,
        "last_percent": None,
    }
    _emit_comfyui_progress(progress_state, prompt_id, progress_callback, phase="queued", force=True)
    output_images = {}
    output_media = {}
    output_text = ""
    expected_asset_count = 0
    expected_video_assets = 0
    fetched_asset_count = 0
    fetched_video_assets = 0
    asset_fetch_failures = []
    asset_summaries = []

    try:
        while True:
            if progress_socket is not None:
                try:
                    while True:
                        raw_message = progress_socket.recv()
                        if not raw_message:
                            progress_socket.close()
                            progress_socket = None
                            break
                        _consume_comfyui_progress_message(
                            raw_message,
                            prompt_id,
                            progress_state,
                            progress_callback,
                        )
                except websocket.WebSocketTimeoutException:
                    pass
                except Exception as ws_error:
                    _log_request("progress_socket_closed", request_id, {"error": str(ws_error)})
                    progress_socket.close()
                    progress_socket = None

            try:
                history = get_history(prompt_id, timeout=http_timeout_seconds)[prompt_id]
                _raise_history_error(history, request_id, prompt_id)
                _emit_comfyui_progress(
                    progress_state,
                    prompt_id,
                    progress_callback,
                    phase="completed",
                    force=True,
                )
                break
            except ComfyUIWorkflowError:
                raise
            except Exception as history_error:
                poll_failures += 1
                elapsed_ms = int((time.monotonic() - poll_started_at) * 1000)
                if poll_failures == 1 or poll_failures % 50 == 0:
                    history_error_payload = _exception_summary_payload(history_error)
                    history_error_payload.update(
                        {
                            "attempt": poll_failures,
                            "elapsed_ms": elapsed_ms,
                        }
                    )
                    _log_request(
                        "history_poll_error",
                        request_id,
                        history_error_payload,
                    )
                if time.monotonic() - poll_started_at >= poll_timeout_seconds:
                    timeout_error = TimeoutError(
                        f"ComfyUI workflow history timeout after {poll_timeout_seconds}s "
                        f"(prompt_id={prompt_id}, last_error={history_error})"
                    )
                    _log_request(
                        "workflow_timeout",
                        request_id,
                        {
                            "prompt_id": prompt_id,
                            "timeout_seconds": poll_timeout_seconds,
                            "attempt": poll_failures,
                            "last_error": str(history_error),
                        },
                    )
                    raise timeout_error
                time.sleep(poll_interval_seconds)
                continue
    finally:
        if progress_socket is not None:
            progress_socket.close()

    for node_id, node_output in history.get("outputs", {}).items():
        if not isinstance(node_output, dict):
            continue
        node_output_keys = list(node_output.keys())
        _log_request(
            "history_node_output_keys",
            request_id,
            {"node_id": node_id, "keys": node_output_keys},
        )

        media_entries = []
        image_assets = []
        seen_assets = set()
        if "images" in node_output and isinstance(node_output["images"], list):
            for image in node_output["images"]:
                if not isinstance(image, dict):
                    continue
                filename = image.get("filename")
                if not filename:
                    continue
                subfolder = image.get("subfolder", "")
                folder_type = image.get("type", "output")
                asset_key = (filename, subfolder, folder_type)
                if asset_key in seen_assets:
                    continue
                seen_assets.add(asset_key)

                media_kind, content_type = infer_media_kind(
                    filename=filename,
                    format_hint=image.get("format", ""),
                    default_kind="image",
                )
                expected_asset_count += 1
                if media_kind == "video":
                    expected_video_assets += 1

                try:
                    asset_data = get_asset_bytes(
                        filename,
                        subfolder,
                        folder_type,
                        timeout=http_timeout_seconds,
                    )
                except Exception as image_err:
                    failure = {
                        "node_id": node_id,
                        "filename": filename,
                        "subfolder": subfolder,
                        "folder_type": folder_type,
                        "media_kind": media_kind,
                    }
                    failure.update(_exception_summary_payload(image_err))
                    asset_fetch_failures.append(failure)
                    _log_request("history_asset_fetch_error", request_id, failure)
                    continue

                fetched_asset_count += 1
                if media_kind == "video":
                    fetched_video_assets += 1
                asset_summaries.append(
                    {
                        "node_id": node_id,
                        "filename": filename,
                        "media_kind": media_kind,
                        "content_type": content_type,
                        "size_bytes": len(asset_data),
                        **(_probe_video_asset(asset_data) if media_kind == "video" else {}),
                    }
                )
                if media_kind == "video":
                    media_entries.append(
                        {
                            "bytes": asset_data,
                            "filename": filename,
                            "content_type": content_type,
                            "media_kind": "video",
                        }
                    )
                else:
                    image_assets.append(asset_data)
            if image_assets:
                output_images[node_id] = image_assets

        for media_key in ("videos", "gifs"):
            media_items = node_output.get(media_key)
            if not isinstance(media_items, list):
                continue
            for media_item in media_items:
                if not isinstance(media_item, dict):
                    continue
                filename = media_item.get("filename")
                if not filename:
                    continue
                subfolder = media_item.get("subfolder", "")
                folder_type = media_item.get("type", "output")
                asset_key = (filename, subfolder, folder_type)
                if asset_key in seen_assets:
                    continue
                seen_assets.add(asset_key)
                media_kind, content_type = infer_media_kind(
                    filename=filename,
                    format_hint=media_item.get("format", ""),
                    default_kind="video",
                )
                expected_asset_count += 1
                if media_kind == "video":
                    expected_video_assets += 1
                try:
                    media_data = get_asset_bytes(
                        filename,
                        subfolder,
                        folder_type,
                        timeout=http_timeout_seconds,
                    )
                except Exception as media_err:
                    failure = {
                        "node_id": node_id,
                        "filename": filename,
                        "subfolder": subfolder,
                        "folder_type": folder_type,
                        "media_kind": media_kind,
                    }
                    failure.update(_exception_summary_payload(media_err))
                    asset_fetch_failures.append(failure)
                    _log_request("history_asset_fetch_error", request_id, failure)
                    continue

                fetched_asset_count += 1
                if media_kind == "video":
                    fetched_video_assets += 1
                asset_summaries.append(
                    {
                        "node_id": node_id,
                        "filename": filename,
                        "media_kind": media_kind,
                        "content_type": content_type,
                        "size_bytes": len(media_data),
                        **(_probe_video_asset(media_data) if media_kind == "video" else {}),
                    }
                )
                media_entries.append(
                    {
                        "bytes": media_data,
                        "filename": filename,
                        "content_type": content_type,
                        "media_kind": media_kind,
                    }
                )
        if media_entries:
            output_media[node_id] = media_entries

        if "response" in node_output:
            response_payload = node_output["response"]
            if isinstance(response_payload, list) and response_payload:
                first_item = response_payload[0]
                if isinstance(first_item, dict):
                    output_text = str(first_item.get("content", ""))
                else:
                    output_text = str(first_item)
            elif isinstance(response_payload, str):
                output_text = response_payload

    _log_request(
        "history_output_summary",
        request_id,
        {
            "prompt_id": prompt_id,
            "history_status": history.get("status") if isinstance(history, dict) else None,
            "image_nodes": list(output_images.keys()),
            "media_nodes": list(output_media.keys()),
            "expected_asset_count": expected_asset_count,
            "expected_video_assets": expected_video_assets,
            "fetched_asset_count": fetched_asset_count,
            "fetched_video_assets": fetched_video_assets,
            "assets": asset_summaries,
            "asset_fetch_failures": asset_fetch_failures,
        },
    )

    if expected_video_assets > 0 and fetched_video_assets == 0:
        failure_payload = {
            "prompt_id": prompt_id,
            "reason": "video_assets_unavailable",
            "expected_video_assets": expected_video_assets,
            "fetched_video_assets": fetched_video_assets,
            "failures": asset_fetch_failures,
        }
        _log_request("workflow_output_failed", request_id, failure_payload)
        raise ComfyUIWorkflowError(
            "ComfyUI workflow completed but video outputs could not be fetched: "
            + _safe_preview(_json_dumps_for_log(failure_payload), 4096)
        )

    if expected_asset_count > 0 and fetched_asset_count == 0:
        failure_payload = {
            "prompt_id": prompt_id,
            "reason": "all_output_assets_unavailable",
            "expected_asset_count": expected_asset_count,
            "fetched_asset_count": fetched_asset_count,
            "failures": asset_fetch_failures,
        }
        _log_request("workflow_output_failed", request_id, failure_payload)
        raise ComfyUIWorkflowError(
            "ComfyUI workflow completed but output assets could not be fetched: "
            + _safe_preview(_json_dumps_for_log(failure_payload), 4096)
        )

    if not output_images and not output_media and not str(output_text or "").strip():
        failure_payload = {
            "prompt_id": prompt_id,
            "reason": "no_usable_outputs",
            "history_status": history.get("status") if isinstance(history, dict) else None,
        }
        _log_request("workflow_output_failed", request_id, failure_payload)
        raise ComfyUIWorkflowError(
            "ComfyUI workflow completed without usable outputs: "
            + _safe_preview(_json_dumps_for_log(failure_payload), 4096)
        )

    return output_images, output_media, output_text


def validate_api_workflow(prompt, workflow_path):
    if not isinstance(prompt, dict):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid workflow format: {workflow_path} is not an API workflow object",
        )

    has_start_workflow = False
    for node_id, node_data in prompt.items():
        if not isinstance(node_data, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid workflow format: node '{node_id}' is not an object. "
                    "Please export workflow in API format."
                ),
            )
        if node_data.get("class_type") == "start_workflow":
            has_start_workflow = True
            if not isinstance(node_data.get("inputs"), dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid workflow format: node '{node_id}' missing 'inputs' object",
                )

    if not has_start_workflow:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid workflow format: {workflow_path} has no start_workflow node. "
                "Please include Start Workflow and export as API."
            ),
        )


def api(
    file_content="",
    image_input=None,
    file_path="",
    img_path="",
    system_prompt="你是一个强大的智能助手",
    user_prompt="",
    positive_prompt="",
    negative_prompt="",
    model_name="",
    workflow_path="测试画画api.json",
    user_history="",
    request_id="",
    img_path2="",
    progress_callback: ProgressCallback = None,
    workflow_params=None,
):
    global current_dir_path
    workflow_params = workflow_params or {}
    workflow_path = workflow_path
    WF_path = os.path.join(current_dir_path, "workflow_api", workflow_path)
    _log_request(
        "api_input",
        request_id,
        {
            "workflow_path": workflow_path,
            "workflow_file": WF_path,
            "file_content": file_content,
            "file_path": file_path,
            "img_path": img_path,
            "img_path2": img_path2,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "positive_prompt": positive_prompt,
            "negative_prompt": negative_prompt,
            "model_name": model_name,
            "user_history": user_history,
            "image_input_count": len(image_input) if isinstance(image_input, list) else 0,
        },
    )
    _log_prompt_texts(
        request_id=request_id,
        workflow_path=workflow_path,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        positive_prompt=positive_prompt,
        negative_prompt=negative_prompt,
    )
    # 判断 WF_path 是否存在
    if not os.path.exists(WF_path):
        raise HTTPException(status_code=404, detail="Workflow file not found")
    with open(WF_path, "r", encoding="utf-8") as f:
        prompt_text = f.read()

    try:
        prompt = json.loads(prompt_text)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid workflow JSON: {e.msg}") from e

    workflow_config = {}
    if isinstance(prompt, dict):
        raw_workflow_config = prompt.pop("__llm_party__", None)
        if isinstance(raw_workflow_config, dict):
            workflow_config = raw_workflow_config

    parameter_report = apply_workflow_parameters(prompt, workflow_params, workflow_config)
    if parameter_report["requested"]:
        _log_request(
            "workflow_parameters_applied",
            request_id,
            {
                "values": normalize_workflow_parameters(workflow_params),
                "report": parameter_report,
            },
        )

    validate_api_workflow(prompt, workflow_path)

    for p, node_data in prompt.items():
        # 如果p的class_type是start_workflow
        if node_data.get("class_type") == "start_workflow":
            inputs = node_data["inputs"]
            image_input_serializable = False
            if file_content != "":
                inputs["file_content"] = file_content
            if image_input is not None and image_input != []:
                if _is_json_serializable(image_input):
                    inputs["image_input"] = image_input
                    image_input_serializable = True
                else:
                    _log_request(
                        "image_input_skipped_non_serializable",
                        request_id,
                        {"type": str(type(image_input))},
                    )
            inputs["file_path"] = file_path
            inputs["img_path1"] = img_path
            inputs["img_path2"] = img_path2
            inputs["system_prompt"] = system_prompt
            inputs["user_prompt"] = user_prompt
            inputs["positive_prompt"] = positive_prompt
            inputs["negative_prompt"] = negative_prompt
            inputs["model_name"] = model_name
            inputs["user_history"] = user_history
            _log_request(
                "start_workflow_injected",
                request_id,
                {
                    "node_id": p,
                    "inputs": inputs,
                },
            )
            _log_request(
                "start_workflow_injected_summary",
                request_id,
                {
                    "node_id": p,
                    "image_input_count": len(image_input) if isinstance(image_input, list) else 0,
                    "image_input_serializable": image_input_serializable,
                    "img_path1": inputs.get("img_path1", ""),
                    "img_path2": inputs.get("img_path2", ""),
                    "build_tag": FASTAPI_BUILD_TAG,
                    "build_commit": FASTAPI_BUILD_COMMIT,
                },
            )

    workflow_started_at = time.monotonic()
    _log_request(
        "workflow_execution_started",
        request_id,
        {
            "workflow_path": workflow_path,
            "duration": workflow_params.get("duration"),
            "fps": workflow_params.get("fps"),
            "resolution": workflow_params.get("resolution"),
            "aspect_ratio": workflow_params.get("aspect_ratio"),
            "workflow_timeout_seconds": _get_workflow_poll_timeout_seconds(),
            "history_poll_interval_seconds": _get_workflow_poll_interval_seconds(),
            "comfyui_http_timeout_seconds": _get_comfyui_http_timeout_seconds(),
        },
    )
    try:
        images, media, res = get_all(
            prompt,
            request_id=request_id,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        failure_payload = _exception_log_payload(exc)
        failure_payload["workflow_path"] = workflow_path
        failure_payload["elapsed_ms"] = int((time.monotonic() - workflow_started_at) * 1000)
        _log_request("workflow_execution_failed", request_id, failure_payload)
        raise
    _log_request(
        "workflow_execution_completed",
        request_id,
        {
            "workflow_path": workflow_path,
            "elapsed_ms": int((time.monotonic() - workflow_started_at) * 1000),
            "image_nodes": list(images.keys()) if isinstance(images, dict) else images,
            "media_nodes": list(media.keys()) if isinstance(media, dict) else media,
        },
    )
    _log_request(
        "api_output",
        request_id,
        {
            "image_nodes": list(images.keys()) if isinstance(images, dict) else images,
            "media_nodes": list(media.keys()) if isinstance(media, dict) else media,
            "response_text": res,
        },
    )
    return images, media, res


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
output_dir = os.path.join(current_dir_path, "output")
os.makedirs(output_dir, exist_ok=True)
app.mount("/images", StaticFiles(directory=output_dir), name="images")


@app.on_event("startup")
async def _startup_version_log():
    _log_request(
        "startup_version",
        "boot",
        {
            "build_tag": FASTAPI_BUILD_TAG,
            "build_commit": FASTAPI_BUILD_COMMIT,
            "cwd": current_dir_path,
            "server_address": server_address,
            "bind_host": args.host,
            "bind_port": args.port,
        },
    )

class Message(BaseModel):
    role: str
    content: Any


class CompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    max_tokens: int = 150
    stream: bool = False
    storage_profile: Optional[str] = None
    duration: Optional[int] = None
    size: Optional[str] = None
    resolution: Optional[Union[float, str]] = None
    fps: Optional[int] = None
    aspect_ratio: Optional[Union[str, float, int]] = None
    ratio: Optional[Union[str, float, int]] = None
    width: Optional[int] = None
    height: Optional[int] = None
    video_config: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None
    workflow_params: Optional[Dict[str, Any]] = None


def _build_workflow_params(request_data: CompletionRequest) -> Dict[str, Any]:
    params = normalize_workflow_parameters(
        request_data.video_config,
        request_data.params,
        request_data.workflow_params,
    )
    for key in ("duration", "size", "resolution", "fps", "aspect_ratio", "ratio", "width", "height"):
        value = getattr(request_data, key, None)
        if value is not None:
            params[key] = value
    return params


VALID_API_KEY = fastapi_api_key

security = HTTPBearer()

async def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    不进行实际的API密钥验证，允许任何API密钥通过。
    """
    # 这里可以选择性地添加一些日志记录或其他处理逻辑
    return credentials.credentials

@app.get("/v1/models")
async def get_models():
    try:
        current_dir_path = os.path.dirname(os.path.abspath(__file__))
        workflow_api_path = os.path.join(current_dir_path, "workflow_api")

        model_names = [
            os.path.splitext(file)[0]
            for file in os.listdir(workflow_api_path)
            if file.endswith(".json")
        ]

        response = {
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "comfyui-LLM-party",
                    "permission": []
                }
                for model_name in model_names
            ],
            "object": "list"
        }

        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def stream_response(response_text: str, model_name: str):
    chunks = []
    current_chunk = []
    # 如果response_text包含中文字符
    if re.search(r'[\u4e00-\u9fa5]', response_text):
        # 定义一个包含中文标点符号的正则表达式模式
        punctuation_pattern = r'[，。；：！？\s]'

        # 使用正则表达式进行分割，但是保留标点符号
        words = re.split(punctuation_pattern, response_text)
        words = [word + punct for word, punct in zip(words, re.findall(punctuation_pattern, response_text) + ['']) if word]
    else:
        # 定义一个包含英文标点符号的正则表达式模式
        punctuation_pattern = r'[.,;:!?\s]'
        # 使用正则表达式进行分割，但是保留标点符号
        words = re.split(punctuation_pattern, response_text)
        words = [word + punct for word, punct in zip(words, re.findall(punctuation_pattern, response_text) + ['']) if word]
    for word in words:
        current_chunk.append(word)
        if len(current_chunk) >= 1:  # Send 3 words at a time
            chunks.append(" ".join(current_chunk))
            current_chunk = []
    
    if current_chunk:  # Add any remaining words
        chunks.append(" ".join(current_chunk))

    for i, chunk in enumerate(chunks):
        delta = {"content": chunk}
        if i == 0:
            delta["role"] = "assistant"
        yield _build_stream_sse_frame(
            model_name=model_name,
            delta=delta,
            finish_reason="stop" if i == len(chunks) - 1 else None,
        )
        await asyncio.sleep(0.1)  # Add small delay between chunks
    
    yield "data: [DONE]\n\n"


def _build_stream_sse_frame(
    model_name: str,
    delta: Optional[Dict[str, Any]] = None,
    finish_reason: Optional[str] = None,
    progress: Optional[Dict[str, Any]] = None,
):
    payload = {
        "id": "chatcmpl-" + str(uuid.uuid4()),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "delta": delta or {},
                "index": 0,
                "finish_reason": finish_reason,
            }
        ],
    }
    if progress is not None:
        payload["progress"] = progress
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

@app.post("/v1/chat/completions")
async def create_completion(request_data: CompletionRequest, request: Request, dependency=Depends(verify_api_key)):
    request_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    _log_request(
        "incoming",
        request_id,
        {
            "build_tag": FASTAPI_BUILD_TAG,
            "build_commit": FASTAPI_BUILD_COMMIT,
            "method": request.method,
            "url": str(request.url),
            "client": f"{request.client.host}:{request.client.port}" if request.client else None,
            "headers": dict(request.headers),
            "request_data": _model_to_dict(request_data),
        },
    )
    try:
        if request_data.stream:
            return StreamingResponse(
                stream_completion_with_heartbeat(request_data, request, request_id=request_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            response = await process_request(request_data, request, request_id=request_id)
        
        _log_request("completion_response", request_id, response)
        return response
    except HTTPException as exc:
        _log_request(
            "http_exception",
            request_id,
            {"status_code": exc.status_code, "detail": exc.detail},
        )
        response_headers = dict(exc.headers or {})
        response_headers.setdefault("X-Request-ID", request_id)
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=response_headers,
        ) from exc
    except Exception as e:
        _log_request("unhandled_exception", request_id, _exception_log_payload(e))
        raise HTTPException(
            status_code=500,
            detail=f"{e} [request_id={request_id}]",
            headers={"X-Request-ID": request_id},
        ) from e


def _extract_content_from_completion(response):
    if not isinstance(response, dict):
        return str(response or "")
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            return str(message.get("content", ""))
    return str(response)


async def stream_completion_with_heartbeat(request_data: CompletionRequest, request: Request, request_id: str):
    progress_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_progress(progress: Dict[str, Any]):
        loop.call_soon_threadsafe(progress_queue.put_nowait, progress)

    task = asyncio.create_task(
        process_request(
            request_data,
            request,
            request_id=request_id,
            progress_callback=on_progress,
        )
    )
    progress_waiter = asyncio.create_task(progress_queue.get())
    # Send an immediate empty chunk so intermediaries receive bytes quickly.
    yield _build_stream_sse_frame(model_name=request_data.model, delta={})

    try:
        while True:
            done, _ = await asyncio.wait(
                {task, progress_waiter},
                timeout=STREAM_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if progress_waiter in done:
                progress = progress_waiter.result()
                yield _build_stream_sse_frame(
                    model_name=request_data.model,
                    delta={},
                    progress=progress,
                )
                progress_waiter = asyncio.create_task(progress_queue.get())

            if task in done:
                await asyncio.sleep(0)
                if progress_waiter.done() and not progress_waiter.cancelled():
                    yield _build_stream_sse_frame(
                        model_name=request_data.model,
                        delta={},
                        progress=progress_waiter.result(),
                    )
                elif not progress_waiter.done():
                    progress_waiter.cancel()
                while True:
                    try:
                        pending_progress = progress_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    yield _build_stream_sse_frame(
                        model_name=request_data.model,
                        delta={},
                        progress=pending_progress,
                    )
                try:
                    response = task.result()
                except Exception as exc:
                    _log_request("stream_error", request_id, _exception_log_payload(exc))
                    error_message = getattr(exc, "detail", None)
                    if error_message is None or not str(error_message).strip():
                        error_message = str(exc)
                    error_payload = {
                        "id": "chatcmpl-" + str(uuid.uuid4()),
                        "object": "error",
                        "created": int(time.time()),
                        "model": request_data.model,
                        "error": {
                            "message": str(error_message),
                            "type": type(exc).__name__,
                            "request_id": request_id,
                        },
                    }
                    yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                break

            if not done:
                if await request.is_disconnected():
                    task.cancel()
                    return
                # Keep both comment-frame and data-frame heartbeats for proxy compatibility.
                yield ": keep-alive\n\n"
                yield _build_stream_sse_frame(model_name=request_data.model, delta={})
    finally:
        if not progress_waiter.done():
            progress_waiter.cancel()

    _log_request("completion_response", request_id, response)
    content = _extract_content_from_completion(response)
    async for chunk in stream_response(content, request_data.model):
        yield chunk

async def process_request(
    request_data: CompletionRequest,
    request: Optional[Request] = None,
    request_id: str = "",
    progress_callback: ProgressCallback = None,
):
    model_name = (request_data.model or "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="model is required")
    if not request_data.messages:
        raise HTTPException(status_code=400, detail="messages is required")

    _log_request("model_name", request_id, {"model_name": model_name})
    raw_images_bytes = []
    image_sources = []
    system_prompt = ""
    user_prompt = ""
    user_histories = []
    for message in request_data.messages:
        user_histories.append({"role": message.role, "content": message.content})
    _log_request("messages_raw", request_id, {"messages": user_histories})
    msg = request_data.messages[-1]
    if isinstance(msg.content, str):
        if msg.role == "system":
            system_prompt = msg.content
        elif msg.role == "user":
            user_prompt = msg.content
    elif isinstance(msg.content, list):
        for content in msg.content:
            if isinstance(content, dict) and "type" in content:
                if content["type"] == "text":
                    user_prompt = content["text"]
                elif content["type"] in {
                    "image_url",
                    "first_frame",
                    "first_frame_image",
                    "last_frame",
                    "last_frame_image",
                    "end_frame",
                    "end_frame_image",
                }:
                    image_field = content.get("image_url")
                    image_url = ""
                    if isinstance(image_field, str):
                        image_url = image_field
                    elif isinstance(image_field, dict):
                        image_url = str(image_field.get("url") or "").strip()
                    elif isinstance(content.get("url"), str):
                        image_url = str(content.get("url") or "").strip()

                    if image_url:
                        if image_url.startswith("data:image/") and ";base64," in image_url:
                            base64_data = image_url.split(";base64,", 1)[1]
                            raw_images_bytes.append(base64.b64decode(base64_data))
                            image_sources.append({"source_type": "data_uri"})
                        # 如果是本地文件路径
                        elif os.path.isfile(image_url):
                            with open(image_url, "rb") as image_file:
                                raw_images_bytes.append(image_file.read())
                            image_sources.append(
                                {
                                    "source_type": "local_file",
                                    "path": _safe_preview(image_url),
                                }
                            )
                        else:
                            # allowed_domains包含你所有的可信域名
                            # allowed_domains = ["trusteddomain.com", "anothertrusteddomain.com"]
                            # parsed_url = urllib.parse.urlparse(content["image_url"])
                            # if parsed_url.netloc not in allowed_domains:
                            #     raise HTTPException(status_code=400, detail="Image URL domain is not allowed.")
                            async with httpx.AsyncClient() as client:
                                response = await client.get(image_url)
                                if response.status_code == 200:
                                    raw_images_bytes.append(response.content)
                                    parsed = urllib.parse.urlparse(image_url)
                                    image_sources.append(
                                        {
                                            "source_type": "remote_url",
                                            "url_host": parsed.netloc,
                                            "url_path": _safe_preview(parsed.path),
                                        }
                                    )
                                else:
                                    raise HTTPException(status_code=400, detail="Image could not be retrieved.")
                    else:
                        raise HTTPException(
                            status_code=400,
                            detail="image_url/first_frame/last_frame must be a string URL or object with image_url.url",
                        )

    _log_request(
        "messages_parsed",
        request_id,
        {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "image_count": len(raw_images_bytes),
            "image_sources": image_sources,
        },
    )

    image_path_list = []
    input_dir = os.path.join(current_dir_path, "input")
    os.makedirs(input_dir, exist_ok=True)
    for idx, raw_image_bytes in enumerate(raw_images_bytes):
        image_bytes = BytesIO(raw_image_bytes)
        img = Image.open(image_bytes)
        img = ImageOps.exif_transpose(img)

        if img.mode == "I":
            img = img.point(lambda i: i * (1 / 256)).convert("L")

        img = img.convert("RGB")
        input_filename = f"api_input_{int(time.time() * 1000)}_{request_id}_{idx + 1}.png"
        input_path = os.path.join(input_dir, input_filename)
        img.save(input_path, format="PNG")
        image_path_list.append(input_path)

    img_path1 = image_path_list[0] if len(image_path_list) > 0 else ""
    img_path2 = image_path_list[1] if len(image_path_list) > 1 else ""
    _log_request(
        "image_pipeline",
        request_id,
        {
            "saved_input_count": len(image_path_list),
            "img_path1": img_path1,
            "img_path2": img_path2,
            "all_paths": image_path_list,
        },
    )

    workflow_path = model_name + ".json"
    user_histories = json.dumps(user_histories, ensure_ascii=False)
    _log_request(
        "before_api_call",
        request_id,
        {
            "workflow_path": workflow_path,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "positive_prompt": "",
            "negative_prompt": "",
            "model_name": "",
            "user_histories": user_histories,
            "image_count": len(image_path_list),
            "img_path1": img_path1,
            "img_path2": img_path2,
            "workflow_param_keys": sorted(_build_workflow_params(request_data).keys()),
        },
    )
    _log_prompt_texts(
        request_id=request_id,
        workflow_path=workflow_path,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        positive_prompt="",
        negative_prompt="",
    )
    images, media_outputs, response = await asyncio.to_thread(
        api,
        file_content="",
        image_input=image_path_list,
        file_path="",
        img_path=img_path1,
        img_path2=img_path2,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        positive_prompt="",
        negative_prompt="",
        model_name="",
        workflow_path=workflow_path,
        user_history=user_histories,
        request_id=request_id,
        progress_callback=progress_callback,
        workflow_params=_build_workflow_params(request_data),
    )

    has_images = isinstance(images, dict) and any(images.values())
    has_media_outputs = isinstance(media_outputs, dict) and any(media_outputs.values())

    if response is None:
        response = ""

    if not has_images and not has_media_outputs:
        parsed_urls = extract_urls_from_text(response)
        parsed_video_urls = [url for url in parsed_urls if is_probable_video_url(url)]
        parsed_image_urls = [url for url in parsed_urls if is_probable_image_url(url)]
        parsed_file_urls = [
            url for url in parsed_urls if url not in parsed_video_urls and url not in parsed_image_urls
        ]
        response_data = {
            "id": "0",
            "object": "text_completion",
            "created": int(time.time()),
            "model": model_name,
            "system_fingerprint": "fp_0",
            "choices": [
                {
                    "message": {"role": "assistant", "content": response},
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "media": [],
            "video_url": parsed_video_urls[0] if parsed_video_urls else "",
            "video_urls": parsed_video_urls,
            "image_url": parsed_image_urls[0] if parsed_image_urls else "",
            "image_urls": parsed_image_urls,
            "file_urls": parsed_file_urls,
            "storage_profile": "",
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 10},
        }
    else:
        media_entries = []
        config_path = os.path.join(current_dir_path, "config.ini")
        runtime_config = configparser.ConfigParser()
        runtime_config.read(config_path, encoding="utf-8")
        api_keys = {}
        if "API_KEYS" in runtime_config:
            api_keys = runtime_config["API_KEYS"]

        imgbb_key = (api_keys.get("imgbb_api") or "").strip()
        public_base_url = resolve_public_base_url(request)
        output_dir = os.path.join(current_dir_path, "output")
        os.makedirs(output_dir, exist_ok=True)
        storage_backend = None
        storage_settings = None
        storage_profile_id = ""
        try:
            storage_settings, storage_profile_id = resolve_storage_settings(
                runtime_config=runtime_config,
                request_data=request_data,
                request=request,
                request_id=request_id,
            )
            storage_backend = create_storage_backend(
                storage_settings,
                output_dir=output_dir,
                public_base_url=public_base_url,
            )
        except Exception as backend_err:
            error_msg = f"Storage backend init failed: {backend_err}"
            print(error_msg)
            raise HTTPException(status_code=500, detail=error_msg) from backend_err
        if storage_settings is not None and storage_settings.enabled and storage_backend is None:
            error_msg = "Storage backend init failed: enabled=true but backend is not available."
            print(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)

        counter = 0
        def save_media(file_bytes: bytes, filename_hint: str, content_type: str, media_kind: str):
            nonlocal counter
            counter += 1
            safe_filename_hint = (filename_hint or "").strip() or f"file_{counter}"
            safe_content_type = (content_type or "").strip() or "application/octet-stream"
            normalized_kind = media_kind if media_kind in {"image", "video"} else "file"

            if storage_backend is not None:
                try:
                    media_url = storage_backend.upload(
                        file_bytes=file_bytes,
                        counter=counter,
                        model_name=model_name,
                        content_type=safe_content_type,
                        original_filename=safe_filename_hint,
                    )
                    parsed_name = os.path.basename(urllib.parse.urlparse(media_url).path)
                    uploaded_name = urllib.parse.unquote(parsed_name) or safe_filename_hint
                    media_entries.append(
                        {
                            "url": media_url,
                            "filename": uploaded_name,
                            "media_type": normalized_kind,
                            "content_type": safe_content_type,
                        }
                    )
                    return
                except Exception as upload_err:
                    error_msg = f"{storage_backend.name} upload failed: {upload_err}"
                    print(error_msg)
                    raise HTTPException(status_code=500, detail=error_msg) from upload_err

            is_image = normalized_kind == "image" or safe_content_type.startswith("image/")
            if is_image and imgbb_key:
                try:
                    img_base64 = base64.b64encode(file_bytes).decode("utf-8")
                    url = "https://api.imgbb.com/1/upload"
                    payload = {"key": imgbb_key, "image": img_base64}
                    response0 = requests.post(url, data=payload, timeout=120)
                    if response0.status_code == 200:
                        result = response0.json()
                        img_url = result["data"]["url"]
                        parsed_name = os.path.basename(urllib.parse.urlparse(img_url).path)
                        uploaded_name = urllib.parse.unquote(parsed_name) or safe_filename_hint
                        media_entries.append(
                            {
                                "url": img_url,
                                "filename": uploaded_name,
                                "media_type": "image",
                                "content_type": safe_content_type,
                            }
                        )
                        return
                    print(f"imgbb upload failed: status={response0.status_code}, body={response0.text}")
                except Exception as imgbb_err:
                    print(f"imgbb upload exception, fallback to local file: {imgbb_err}")

            local_filename = build_local_filename(
                counter=counter,
                filename_hint=safe_filename_hint,
                content_type=safe_content_type,
                media_kind=normalized_kind,
            )
            file_path = os.path.join(output_dir, local_filename)
            with open(file_path, "wb") as f:
                f.write(file_bytes)
            media_url = f"{public_base_url}/images/{urllib.parse.quote(local_filename)}"
            media_entries.append(
                {
                    "url": media_url,
                    "filename": local_filename,
                    "media_type": normalized_kind,
                    "content_type": safe_content_type,
                }
            )

        for node_id in (media_outputs or {}):
            for idx, media_item in enumerate(media_outputs[node_id], start=1):
                if not isinstance(media_item, dict):
                    continue
                file_bytes = media_item.get("bytes")
                if not file_bytes:
                    continue
                filename_hint = str(media_item.get("filename") or f"media_{node_id}_{idx}").strip()
                media_kind = str(media_item.get("media_kind") or "video").strip().lower()
                content_type = guess_media_content_type(
                    filename=filename_hint,
                    format_hint=str(media_item.get("content_type") or ""),
                    media_kind=media_kind,
                )
                if media_kind not in {"image", "video"}:
                    media_kind = "image" if content_type.startswith("image/") else "video"
                save_media(file_bytes, filename_hint, content_type, media_kind)

        for node_id in (images or {}):
            for idx, image_data in enumerate(images[node_id], start=1):
                filename_hint = f"image_{node_id}_{idx}.png"
                save_media(image_data, filename_hint, "image/png", "image")

        for media_entry in media_entries:
            media_url = media_entry["url"]
            media_filename = media_entry["filename"]
            media_type = media_entry.get("media_type", "file")
            if media_type == "image":
                response_line = f"![{media_filename}]({media_url})"
            elif media_type == "video":
                response_line = f"[video:{media_filename}]({media_url})"
            else:
                response_line = f"[{media_filename}]({media_url})"
            response += "\n" + response_line + "\n"

        print(response)
        video_urls = [entry["url"] for entry in media_entries if entry.get("media_type") == "video"]
        image_urls = [entry["url"] for entry in media_entries if entry.get("media_type") == "image"]
        file_urls = [
            entry["url"]
            for entry in media_entries
            if entry.get("media_type") not in {"image", "video"}
        ]
        # Ensure schema-stable URL fields even when media entries are incomplete.
        if not video_urls or not image_urls:
            parsed_urls = extract_urls_from_text(response)
            if not video_urls:
                video_urls = [url for url in parsed_urls if is_probable_video_url(url)]
            if not image_urls:
                image_urls = [url for url in parsed_urls if is_probable_image_url(url)]
            if not file_urls:
                file_urls = [
                    url for url in parsed_urls if url not in video_urls and url not in image_urls
                ]
        response_data = {
            "id": "0",
            "object": "text_completion",
            "created": int(time.time()),
            "model": model_name,
            "system_fingerprint": "fp_0",
            "choices": [
                {
                    "message": {"role": "assistant", "content": response},
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "media": media_entries,
            "video_url": video_urls[0] if video_urls else "",
            "video_urls": video_urls,
            "image_url": image_urls[0] if image_urls else "",
            "image_urls": image_urls,
            "file_urls": file_urls,
            "storage_profile": storage_profile_id,
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 10},
        }
    _log_request("response_payload_full", request_id, response_data)
    return response_data


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
