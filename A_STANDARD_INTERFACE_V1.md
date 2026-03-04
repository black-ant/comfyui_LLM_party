# A 项目标准接口（唯一标准，B 必须对齐）

版本：`v1`  
适用链路：`B(ant-ai-2api) -> A(comfyui_LLM_party) -> ComfyUI`

## 1. 目标
定义 **B 调用 A** 的唯一协议。  
B 必须按本协议请求/解析，不再使用其他格式。

## 2. 接口列表
- `POST /v1/chat/completions`
- `GET /v1/models`

## 3. `POST /v1/chat/completions` 请求标准

### 3.1 请求头
- `Content-Type: application/json`
- `Authorization: Bearer <token>`

### 3.2 请求体（视频场景）
```json
{
  "model": "video_wan2_complement",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "变装，我需要像钢铁侠一样，一个个变装"},
        {"type": "image_url", "image_url": {"url": "https://example.com/input1.jpg"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/input2.jpg"}}
      ]
    }
  ],
  "stream": false,
  "max_tokens": 150,
  "storage_profile": "prod"
}
```

### 3.3 强约束（MUST）
1. `stream` 在视频调用时必须为 `false`。  
2. `messages` 必须包含 `role=user` 的输入消息。  
3. 多模态图片统一使用对象格式：`image_url.url`。  
4. `model` 必须对应 A 的 `workflow_api/<model>.json`。

## 4. `POST /v1/chat/completions` 响应标准

### 4.1 成功响应（HTTP 200）
```json
{
  "id": "0",
  "object": "text_completion",
  "created": 1772599999,
  "model": "video_wan2_complement",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "[video:xxx.mp4](https://cos.example.com/xxx.mp4)"
      },
      "finish_reason": "stop",
      "logprobs": null
    }
  ],
  "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 10},
  "media": [
    {
      "url": "https://cos.example.com/xxx.mp4",
      "filename": "xxx.mp4",
      "media_type": "video",
      "content_type": "video/mp4"
    }
  ],
  "video_url": "https://cos.example.com/xxx.mp4",
  "video_urls": ["https://cos.example.com/xxx.mp4"],
  "image_url": "",
  "image_urls": [],
  "file_urls": [],
  "storage_profile": "prod"
}
```

### 4.2 强约束（MUST）
1. 视频成功时，`video_url` 必须为非空绝对 URL。  
2. `video_urls` 必须包含 `video_url`。  
3. `choices[0].message.content` 仅作可读文本，不作为主解析字段。  
4. B 的主解析字段必须是 `video_url`（不是 markdown 文本）。

## 5. 错误响应
- A 失败时返回 FastAPI 标准错误（HTTP 4xx/5xx，包含 `detail`）。
- B 不应将“无视频 URL”归类为成功。

## 6. `GET /v1/models` 标准
返回 OpenAI 风格 list：
```json
{
  "object": "list",
  "data": [
    {"id": "video_wan2_complement", "object": "model", "created": 1772590000, "owned_by": "comfyui-LLM-party", "permission": []}
  ]
}
```

## 7. 当前 A 代码实现位置（用于核对）
- `fast_api.py:760` `CompletionRequest`（含 `storage_profile`）
- `fast_api.py:865` `POST /v1/chat/completions`
- `fast_api.py:779` `GET /v1/models`
- `fast_api.py:1318` `video_url/video_urls` 返回字段
