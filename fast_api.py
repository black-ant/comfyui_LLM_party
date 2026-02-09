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
from storage_backends import create_storage_backend, load_storage_settings, parse_bool
import asyncio
parser = argparse.ArgumentParser(description="Run the server with specified host and port.")
parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address to bind the server.")
parser.add_argument("--port", type=int, default=8187, help="Port number to bind the server.")
parser.add_argument("--public-base-url", type=str, default=None, help="Public base URL for image links.")
parser.add_argument("--object-storage-enabled", type=str, default=None, help="Enable external storage: true/false.")
parser.add_argument("--object-storage-type", type=str, default=None, help="Storage backend type: modal|minio|local.")
parser.add_argument("--object-storage-base-url", type=str, default=None, help="Storage base URL / endpoint.")
parser.add_argument("--object-storage-public-base-url", type=str, default=None, help="Public storage URL.")
parser.add_argument("--object-storage-api-key", type=str, default=None, help="Storage API key / access key.")
parser.add_argument("--object-storage-secret-key", type=str, default=None, help="Storage secret key for MinIO.")
parser.add_argument("--object-storage-channel", type=str, default=None, help="Storage channel (Modal) / bucket (MinIO).")
parser.add_argument("--object-storage-custom-filename", type=str, default=None, help="Object storage custom filename.")

args = parser.parse_args()
if parse_bool(args.object_storage_enabled, default=False) and not (args.object_storage_type or "").strip():
    parser.error("--object-storage-type is required when --object-storage-enabled=true")

current_dir_path = os.path.dirname(os.path.realpath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(current_dir_path, "config.ini"))
# 获取配置文件中的参数
fastapi_api_key = config.get("API_KEYS", "fastapi_api_key", fallback="")
server_address = "127.0.0.1:8188"
client_id = str(uuid.uuid4())


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


def _log_request(stage: str, request_id: str, payload):
    print(f"[FASTAPI][{request_id}][{stage}] {_json_dumps_for_log(payload)}")

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
                response_payload = node_output["response"]
                if isinstance(response_payload, list) and response_payload:
                    first_item = response_payload[0]
                    if isinstance(first_item, dict):
                        output_text = str(first_item.get("content", ""))
                    else:
                        output_text = str(first_item)
                elif isinstance(response_payload, str):
                    output_text = response_payload

    return output_images, output_text


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
):
    global current_dir_path
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
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "positive_prompt": positive_prompt,
            "negative_prompt": negative_prompt,
            "model_name": model_name,
            "user_history": user_history,
            "image_input_count": len(image_input) if isinstance(image_input, list) else 0,
        },
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

    validate_api_workflow(prompt, workflow_path)

    for p, node_data in prompt.items():
        # 如果p的class_type是start_workflow
        if node_data.get("class_type") == "start_workflow":
            inputs = node_data["inputs"]
            if file_content != "":
                inputs["file_content"] = file_content
            if image_input is not None and image_input != []:
                inputs["image_input"] = image_input
            inputs["file_path"] = file_path
            inputs["img_path1"] = img_path
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

    images, res = get_all(prompt)
    _log_request(
        "api_output",
        request_id,
        {
            "image_nodes": list(images.keys()) if isinstance(images, dict) else images,
            "response_text": res,
        },
    )
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
    request_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    _log_request(
        "incoming",
        request_id,
        {
            "method": request.method,
            "url": str(request.url),
            "client": f"{request.client.host}:{request.client.port}" if request.client else None,
            "headers": dict(request.headers),
            "request_data": _model_to_dict(request_data),
        },
    )
    try:
        if request_data.stream:
            response = await process_request(request_data, request, request_id=request_id)
            if isinstance(response, dict) and "choices" in response:
                content = response["choices"][0]["message"]["content"]
                return StreamingResponse(
                    stream_response(content, request_data.model),
                    media_type="text/event-stream"
                )
        else:
            response = await process_request(request_data, request, request_id=request_id)
        
        _log_request("completion_response", request_id, response)
        return response
    except HTTPException:
        _log_request("http_exception", request_id, {"detail": "HTTPException raised"})
        raise
    except Exception as e:
        _log_request("unhandled_exception", request_id, {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))

async def process_request(request_data: CompletionRequest, request: Optional[Request] = None, request_id: str = ""):
    model_name = (request_data.model or "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="model is required")
    if not request_data.messages:
        raise HTTPException(status_code=400, detail="messages is required")

    _log_request("model_name", request_id, {"model_name": model_name})
    base64_encoded_list = []
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

    _log_request(
        "messages_parsed",
        request_id,
        {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "image_base64_count": len(base64_encoded_list),
        },
    )

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
            "image_tensor_count": len(img_out),
        },
    )
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
        request_id,
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
        runtime_config = configparser.ConfigParser()
        runtime_config.read(config_path, encoding="utf-8")
        api_keys = {}
        if "API_KEYS" in runtime_config:
            api_keys = runtime_config["API_KEYS"]

        imgbb_key = api_keys.get("imgbb_api")
        print(imgbb_key)
        public_base_url = resolve_public_base_url(request)
        output_dir = os.path.join(current_dir_path, "output")
        os.makedirs(output_dir, exist_ok=True)
        storage_backend = None
        try:
            storage_settings = load_storage_settings(
                runtime_config,
                cli_type=args.object_storage_type,
                cli_enabled=args.object_storage_enabled,
                cli_base_url=args.object_storage_base_url,
                cli_public_base_url=args.object_storage_public_base_url,
                cli_api_key=args.object_storage_api_key,
                cli_secret_key=args.object_storage_secret_key,
                cli_channel=args.object_storage_channel,
                cli_custom_filename=args.object_storage_custom_filename,
            )
            storage_backend = create_storage_backend(
                storage_settings,
                output_dir=output_dir,
                public_base_url=public_base_url,
            )
        except Exception as backend_err:
            print(f"Storage backend init failed, fallback to local/imgbb: {backend_err}")

        counter = 0
        for node_id in images:
            for image_data in images[node_id]:
                counter += 1
                img_base64 = base64.b64encode(image_data).decode("utf-8")

                if storage_backend is not None:
                    try:
                        image_url = storage_backend.upload(image_data, counter, model_name)
                        base64_images.append(image_url)
                        continue
                    except Exception as upload_err:
                        print(f"{storage_backend.name} upload failed, fallback to local/imgbb: {upload_err}")

                if imgbb_key is None or imgbb_key == "":
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
