# FastAPI 调用方示例（ComfyUI LLM Party）

## 1) 接口地址
- `POST /v1/chat/completions`
- `GET /v1/models`

## 2) 鉴权
- Header 需要带：`Authorization: Bearer <token>`
- 兼容 Header：`x-api-key: <token>`（可选）

## 3) 非流式请求（推荐先接这个）

### 请求示例
```bash
curl -X POST "http://127.0.0.1:8187/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 123456" \
  -d '{
    "model": "video_wan2_complement",
    "stream": false,
    "max_tokens": 150,
    "storage_profile": "prod",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "变装，我需要像钢铁侠一样，一个个变装"},
          {"type": "image_url", "image_url": "https://example.com/input1.jpg"},
          {"type": "image_url", "image_url": "https://example.com/input2.jpg"}
        ]
      }
    ]
  }'
```

### 返回示例（视频成功）
```json
{
  "id": "0",
  "object": "text_completion",
  "created": 1772599999,
  "model": "video_wan2_complement",
  "system_fingerprint": "fp_0",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "\n[video:video_1772599999000_1.mp4](https://bucket-appid.cos.ap-singapore.myqcloud.com/video_1772599999000_1.mp4)\n"
      },
      "index": 0,
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "media": [
    {
      "url": "https://bucket-appid.cos.ap-singapore.myqcloud.com/video_1772599999000_1.mp4",
      "filename": "video_1772599999000_1.mp4",
      "media_type": "video",
      "content_type": "video/mp4"
    }
  ],
  "video_url": "https://bucket-appid.cos.ap-singapore.myqcloud.com/video_1772599999000_1.mp4",
  "video_urls": [
    "https://bucket-appid.cos.ap-singapore.myqcloud.com/video_1772599999000_1.mp4"
  ],
  "image_url": "",
  "image_urls": [],
  "file_urls": [],
  "storage_profile": "prod",
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 10,
    "total_tokens": 10
  }
}
```

### 调用方建议解析顺序
1. 先读 `video_url`（单视频最方便）。
2. 再读 `video_urls`（多视频场景）。
3. 如仍为空，最后回退解析 `choices[0].message.content` 里的 markdown 链接。

## 4) 流式请求（SSE）

### 请求示例
```bash
curl -N -X POST "http://127.0.0.1:8187/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 123456" \
  -d '{
    "model": "video_wan2_complement",
    "stream": true,
    "messages": [
      {
        "role": "user",
        "content": [{"type": "text", "text": "生成一个短视频"}]
      }
    ]
  }'
```

### SSE 返回示例
```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1772599999,"model":"video_wan2_complement","choices":[{"delta":{},"index":0,"finish_reason":null}]}

: keep-alive

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1772600001,"model":"video_wan2_complement","choices":[{"delta":{"role":"assistant","content":"[video:...](https://...mp4)"},"index":0,"finish_reason":"stop"}]}

data: [DONE]
```

## 5) 入参字段说明（POST /v1/chat/completions）
- `model` `string` 必填：对应 `workflow_api/<model>.json`
- `messages` `array` 必填
- `messages[].role`：`system|user|assistant`
- `messages[].content`：可为字符串，或多模态数组
- 多模态项：
  - 文本：`{"type":"text","text":"..."}`
  - 图片（字符串）：`{"type":"image_url","image_url":"https://... 或 data:image/..."}`
  - 图片（对象）：`{"type":"image_url","image_url":{"url":"https://... 或 data:image/..."}}`
- `stream` `boolean`：是否流式（默认 `false`）
- `max_tokens` `int`：保留字段
- `storage_profile` `string`：可选，指定对象存储配置档

## 6) 出参字段说明（非流式）
- `choices[0].message.content`：文本与 markdown 链接
- `media[]`：结构化媒体列表，字段 `url/filename/media_type/content_type`
- `video_url`：首个视频 URL（推荐调用方直接用这个）
- `video_urls`：全部视频 URL
- `image_url`：首张图片 URL
- `image_urls`：全部图片 URL
- `file_urls`：其它文件 URL

## 7) 兼容与约束
- 若工作流最终没有产出视频节点，`video_url/video_urls` 会为空。
- 若配置了 `object_storage_custom_filename=image.png`，系统会保留文件名前缀，但扩展名会自动修正为真实媒体类型（例如 `.mp4`）。
- 可先调用 `GET /v1/models` 获取当前可用模型名。
