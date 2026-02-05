import argparse
import base64
import configparser
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from io import BytesIO
from typing import Any, List, Optional

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
import httpx
import numpy as np
import requests
import torch
import websocket
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel
import asyncio
parser = argparse.ArgumentParser(description="Run the server with specified host and port.")
parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address to bind the server.")
parser.add_argument("--port", type=int, default=8187, help="Port number to bind the server.")
parser.add_argument("--public-base-url", type=str, default=None, help="Public base URL for image links.")
parser.add_argument("--object-storage-enabled", type=str, default=None, help="Enable MinIO object storage: true/false.")
parser.add_argument("--object-storage-base-url", type=str, default=None, help="MinIO endpoint URL (kept for compatibility).")
parser.add_argument("--object-storage-public-base-url", type=str, default=None, help="Public MinIO base URL.")
parser.add_argument("--object-storage-api-key", type=str, default=None, help="MinIO access key (or access_key:secret_key).")
parser.add_argument("--object-storage-secret-key", type=str, default=None, help="MinIO secret key.")
parser.add_argument("--object-storage-channel", type=str, default=None, help="MinIO bucket name (kept for compatibility).")
parser.add_argument("--object-storage-custom-filename", type=str, default=None, help="Object storage custom filename.")

args = parser.parse_args()
current_dir_path = os.path.dirname(os.path.realpath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(current_dir_path, "config.ini"))
# 获取配置文件中的参数
fastapi_api_key = config.get("API_KEYS", "fastapi_api_key", fallback="")
server_address = "127.0.0.1:8188"
client_id = str(uuid.uuid4())

def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def split_access_and_secret(value: str):
    raw = (value or "").strip()
    if not raw:
        return "", ""
    for sep in (":", "|", ","):
        if sep in raw:
            access_key, secret_key = raw.split(sep, 1)
            return access_key.strip(), secret_key.strip()
    return raw, ""


def parse_minio_endpoint(endpoint_url: str):
    raw = (endpoint_url or "").strip()
    if not raw:
        return "", False
    normalized = raw if "://" in raw else f"http://{raw}"
    parsed = urllib.parse.urlparse(normalized)
    endpoint = (parsed.netloc or parsed.path).strip().strip("/")
    if "/" in endpoint:
        endpoint = endpoint.split("/", 1)[0]
    return endpoint, parsed.scheme == "https"


def build_minio_public_base_url(public_base_url: str, endpoint: str, secure: bool, bucket: str):
    base_url = (public_base_url or "").strip().rstrip("/")
    if not base_url and not endpoint:
        return ""
    if not base_url:
        scheme = "https" if secure else "http"
        base_url = f"{scheme}://{endpoint}".rstrip("/")
    if bucket and not base_url.endswith(f"/{bucket}"):
        base_url = f"{base_url}/{bucket}"
    return base_url.rstrip("/")

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

def load_object_storage_config():
    cli_enabled = args.object_storage_enabled
    cli_endpoint_url = (args.object_storage_base_url or "").strip()
    cli_api_key = (args.object_storage_api_key or "").strip()
    cli_secret_key = (args.object_storage_secret_key or "").strip()
    cli_bucket = (args.object_storage_channel or "").strip()
    cli_custom_filename = (args.object_storage_custom_filename or "").strip()
    cli_public_base_url = (args.object_storage_public_base_url or "").strip()

    enabled = parse_bool(
        cli_enabled
        or os.getenv("OBJECT_STORAGE_ENABLED")
        or config.get("API_KEYS", "object_storage_enabled", fallback=""),
        default=False,
    )
    endpoint_url = (
        cli_endpoint_url
        or os.getenv("MINIO_ENDPOINT", "").strip()
        or os.getenv("OBJECT_STORAGE_BASE_URL", "").strip()
        or config.get("API_KEYS", "object_storage_base_url", fallback="").strip()
    )
    api_key = (
        cli_api_key
        or os.getenv("MINIO_ACCESS_KEY", "").strip()
        or os.getenv("OBJECT_STORAGE_API_KEY", "").strip()
        or config.get("API_KEYS", "object_storage_api_key", fallback="").strip()
    )
    secret_key = (
        cli_secret_key
        or os.getenv("MINIO_SECRET_KEY", "").strip()
        or os.getenv("OBJECT_STORAGE_SECRET_KEY", "").strip()
        or config.get("API_KEYS", "object_storage_secret_key", fallback="").strip()
    )
    bucket = (
        cli_bucket
        or os.getenv("MINIO_BUCKET", "").strip()
        or os.getenv("OBJECT_STORAGE_CHANNEL", "").strip()
        or config.get("API_KEYS", "object_storage_channel", fallback="default").strip()
        or "default"
    )
    custom_filename = (
        cli_custom_filename
        or os.getenv("OBJECT_STORAGE_CUSTOM_FILENAME", "").strip()
        or config.get("API_KEYS", "object_storage_custom_filename", fallback="").strip()
    )
    public_base_url = (
        cli_public_base_url
        or os.getenv("MINIO_PUBLIC_BASE_URL", "").strip()
        or os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL", "").strip()
        or config.get("API_KEYS", "object_storage_public_base_url", fallback="").strip()
    )
    endpoint, endpoint_is_https = parse_minio_endpoint(endpoint_url)
    if not secret_key:
        api_key, parsed_secret = split_access_and_secret(api_key)
        secret_key = parsed_secret
    secure = parse_bool(
        os.getenv("MINIO_SECURE", "").strip()
        or config.get("API_KEYS", "object_storage_secure", fallback=""),
        default=endpoint_is_https,
    )
    resolved_public_base_url = build_minio_public_base_url(public_base_url, endpoint, secure, bucket)

    return {
        "enabled": enabled and bool(endpoint) and bool(api_key) and bool(secret_key) and bool(bucket),
        "endpoint": endpoint,
        "access_key": api_key,
        "secret_key": secret_key,
        "bucket": bucket,
        "secure": secure,
        "custom_filename": custom_filename,
        "public_base_url": resolved_public_base_url,
    }

def upload_to_object_storage(image_bytes, counter, storage_config, model_name):
    try:
        from minio import Minio
    except ImportError as exc:
        raise RuntimeError("Missing dependency: minio. Please install it first.") from exc

    custom_filename = storage_config["custom_filename"]
    if custom_filename:
        base, ext = os.path.splitext(custom_filename)
        object_name = f"{base}_{counter}{ext or '.png'}"
    else:
        safe_model_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", model_name) or "workflow"
        object_name = f"{safe_model_name}_{int(time.time())}_{counter}.png"

    client = Minio(
        storage_config["endpoint"],
        access_key=storage_config["access_key"],
        secret_key=storage_config["secret_key"],
        secure=storage_config["secure"],
    )

    bucket = storage_config["bucket"]
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except Exception:
        pass

    data_stream = BytesIO(image_bytes)
    client.put_object(
        bucket,
        object_name,
        data_stream,
        length=len(image_bytes),
        content_type="image/png",
    )
    quoted_name = urllib.parse.quote(object_name, safe="/")
    return f"{storage_config['public_base_url']}/{quoted_name}"


def queue_prompt(prompt):
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode("utf-8")
    req = urllib.request.Request("http://{}/prompt".format(server_address), data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_image(filename, subfolder, folder_type):
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen("http://{}/view?{}".format(server_address, url_values)) as response:
        return response.read()


def get_history(prompt_id):
    with urllib.request.urlopen("http://{}/history/{}".format(server_address, prompt_id)) as response:
        return json.loads(response.read())


def get_all(prompt):
    prompt_id = queue_prompt(prompt)["prompt_id"]
    output_images = {}
    output_text = ""

    while True:
        try:
            history = get_history(prompt_id)[prompt_id]
            break
        except Exception:
            time.sleep(0.1)
            continue

    for o in history["outputs"]:
        for node_id in history["outputs"]:
            node_output = history["outputs"][node_id]
            if "images" in node_output:
                images_output = []
                for image in node_output["images"]:
                    image_data = get_image(image["filename"], image["subfolder"], image["type"])
                    images_output.append(image_data)
                output_images[node_id] = images_output
            if "response" in node_output:
                output_text = node_output["response"][0]["content"]

    return output_images, output_text


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
):
    global current_dir_path
    workflow_path = workflow_path
    WF_path = os.path.join(current_dir_path, "workflow_api", workflow_path)
    # 判断 WF_path 是否存在
    if not os.path.exists(WF_path):
        raise HTTPException(status_code=404, detail="Workflow file not found")
    with open(WF_path, "r", encoding="utf-8") as f:
        prompt_text = f.read()

    prompt = json.loads(prompt_text)

    for p in prompt:
        # 如果p的class_type是start_workflow
        if prompt[p]["class_type"] == "start_workflow":
            if file_content != "":
                prompt[p]["inputs"]["file_content"] = file_content
            if image_input is not None and image_input != []:
                prompt[p]["inputs"]["image_input"] = image_input
            prompt[p]["inputs"]["file_path"] = file_path
            prompt[p]["inputs"]["img_path1"] = img_path
            prompt[p]["inputs"]["system_prompt"] = system_prompt
            prompt[p]["inputs"]["user_prompt"] = user_prompt
            prompt[p]["inputs"]["positive_prompt"] = positive_prompt
            prompt[p]["inputs"]["negative_prompt"] = negative_prompt
            prompt[p]["inputs"]["model_name"] = model_name
            prompt[p]["inputs"]["user_history"] = user_history

    images, res = get_all(prompt)
    return images, res


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

class Message(BaseModel):
    role: str
    content: Any


class CompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    max_tokens: int = 150
    stream: bool = False


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
        response_chunk = {
            "id": "chatcmpl-" + str(uuid.uuid4()),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{
                "delta": {
                    "role": "assistant" if i == 0 else None,
                    "content": chunk
                },
                "index": 0,
                "finish_reason": "stop" if i == len(chunks) - 1 else None
            }]
        }
        yield f"data: {json.dumps(response_chunk)}\n\n"
        await asyncio.sleep(0.1)  # Add small delay between chunks
    
    yield "data: [DONE]\n\n"

@app.post("/v1/chat/completions")
async def create_completion(request_data: CompletionRequest, request: Request, dependency=Depends(verify_api_key)):
    try:
        if request_data.stream:
            response = await process_request(request_data, request)
            if isinstance(response, dict) and "choices" in response:
                content = response["choices"][0]["message"]["content"]
                return StreamingResponse(
                    stream_response(content, request_data.model),
                    media_type="text/event-stream"
                )
        else:
            response = await process_request(request_data, request)
        
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def process_request(request_data: CompletionRequest, request: Optional[Request] = None):
    model_name = request_data.model
    print(model_name)
    base64_encoded_list = []
    system_prompt = ""
    user_histories = []
    for message in request_data.messages:
        user_histories.append({"role": message.role, "content": message.content})
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
                elif content["type"] == "image_url":
                    if isinstance(content["image_url"], str):
                        if content["image_url"].startswith("data:image/png;base64,"):
                            base64_data = content["image_url"].split("data:image/png;base64,")[1]
                            base64_encoded_list.append(base64_data)
                        # 如果是本地文件路径
                        elif os.path.isfile(content["image_url"]):
                            with open(content["image_url"], "rb") as image_file:
                                base64_encoded = base64.b64encode(image_file.read()).decode("utf-8")
                        else:
                            # allowed_domains包含你所有的可信域名
                            # allowed_domains = ["trusteddomain.com", "anothertrusteddomain.com"]
                            # parsed_url = urllib.parse.urlparse(content["image_url"])
                            # if parsed_url.netloc not in allowed_domains:
                            #     raise HTTPException(status_code=400, detail="Image URL domain is not allowed.")
                            async with httpx.AsyncClient() as client:
                                response = await client.get(content["image_url"])
                                if response.status_code == 200:
                                    image_bytes = BytesIO(response.content)
                                    base64_encoded = base64.b64encode(image_bytes.read()).decode("utf-8")
                                    base64_encoded_list.append(base64_encoded)
                                else:
                                    raise HTTPException(status_code=400, detail="Image could not be retrieved.")

    img_out = []
    for base64_encoded in base64_encoded_list:
        image_bytes = BytesIO(base64.b64decode(base64_encoded))
        img = Image.open(image_bytes)
        img = ImageOps.exif_transpose(img)

        if img.mode == "I":
            img = img.point(lambda i: i * (1 / 256)).convert("L")

        img = img.convert("RGB")
        image_np = np.array(img).astype(np.float32) / 255.0
        image_tensor = torch.from_numpy(image_np).permute(2, 0, 1).unsqueeze(0)
        img_out.append(image_tensor)

    workflow_path = model_name + ".json"
    user_histories = json.dumps(user_histories, ensure_ascii=False)
    images, response = api(
        "",
        img_out,
        "",
        "",
        system_prompt,
        user_prompt,
        "",
        "",
        "",
        workflow_path,
        user_histories,
    )
    
    if images is None or images == []:
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
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 10},
        }
    else:
        base64_images = []
        config_path = os.path.join(current_dir_path, "config.ini")
        print(config_path)
        config = configparser.ConfigParser()
        config.read(config_path, encoding="utf-8")
        api_keys = {}
        if "API_KEYS" in config:
            api_keys = config["API_KEYS"]

        imgbb_key = api_keys.get("imgbb_api")
        print(imgbb_key)
        object_storage_config = load_object_storage_config()
        public_base_url = resolve_public_base_url(request)
        counter = 0
        for node_id in images:
            for image_data in images[node_id]:
                counter += 1
                img_base64 = base64.b64encode(image_data).decode("utf-8")

                if object_storage_config["enabled"]:
                    try:
                        image_url = upload_to_object_storage(image_data, counter, object_storage_config, model_name)
                        base64_images.append(image_url)
                        continue
                    except Exception as upload_err:
                        print(f"MinIO upload failed, fallback to local output: {upload_err}")

                if imgbb_key is None or imgbb_key == "":
                    # 把img_base64保存到当前目录下的output文件夹
                    output_dir = os.path.join(current_dir_path, "output")
                    os.makedirs(output_dir, exist_ok=True)
                    timestamp = int(time.time())
                    filename = f"{timestamp}_{counter}.png"
                    file_path = os.path.join(output_dir, filename)
                    with open(file_path, "wb") as f:
                        f.write(base64.b64decode(img_base64))
                    image_url = f"{public_base_url}/images/{filename}"
                    base64_images.append(image_url)
                else:
                    url = "https://api.imgbb.com/1/upload"
                    payload = {"key": imgbb_key, "image": img_base64}
                    response0 = requests.post(url, data=payload)
                    if response0.status_code == 200:
                        result = response0.json()
                        img_url = result["data"]["url"]
                    else:
                        return "Error: " + response0.text
                    print(img_url)
                    base64_images.append(img_url)
            
        if response is None:
            response = ""
            
        for img in base64_images:
            response_url = f"![image]({img})"
            response += "\n" + response_url + "\n"
            
        print(response)
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
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 10},
        }
    return response_data


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
