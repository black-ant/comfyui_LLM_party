# B 项目对齐改造文档（ant-ai-2api -> A v1）

目标：让 `ant-ai-2api` **严格对齐** [A_STANDARD_INTERFACE_V1.md](d:/code/open_source/comfyui_LLM_party/A_STANDARD_INTERFACE_V1.md)，只保留一套标准。

## 1. 必须改造项（按文件）

### 1.1 入口请求字段补齐
文件：`backend/internal/gateway/handler/chat.go`

必须改：
1. `ChatCompletionRequest` 新增：
   - `StorageProfile string \`json:"storage_profile,omitempty"\``
2. `extractChatExtra` 增加 `storage_profile` 提取：
   - 读取 `storage_profile`（必要时兼容 `storageProfile`）
   - 写入 `extra["storage_profile"]`
3. 保留 `max_tokens`（已存在）并继续透传。

现状依据：
- `ChatCompletionRequest` 含 `max_tokens`/`stream`，但无 `storage_profile`。
- `extractChatExtra` 未提取 `storage_profile`。

---

### 1.2 多模态图片解析统一为标准
文件：`backend/internal/gateway/service/forwarder/message_extractor.go`

必须改：
1. `case "image_url"` 支持两种输入：
   - 标准：`image_url: {"url":"..."}`
   - 兼容：`image_url: "..."`（防脏数据）
2. `first_frame` / `last_frame` 同步支持字符串与对象。

现状依据：
- 当前仅支持 `image_url` 为对象，字符串会丢失。

---

### 1.3 B -> A 请求体结构升级（核心）
文件：`backend/internal/provider/comfyui_video/comfyui_video_types.go`

必须改：
1. `ComfyUIVideoAPIRequest` 增加字段：
   - `MaxTokens int \`json:"max_tokens,omitempty"\``
   - `StorageProfile string \`json:"storage_profile,omitempty"\``

文件：`backend/internal/provider/comfyui_video/comfyui_video_adapter.go`

必须改：
1. `GenerateVideo` 发给 A 的请求改为：
   - `stream: false`（硬约束）
   - 写入 `max_tokens`
   - 写入 `storage_profile`
2. 不再依赖 SSE 聚合解析作为主路径（保留兜底可以，但主路径必须是 JSON 非流式）。

现状依据：
- 目前 `GenerateVideo` 固定 `Stream: true`。
- 请求结构仅 `model/messages/stream`。

---

### 1.4 响应解析改为唯一标准字段
文件：`backend/internal/provider/comfyui_video/comfyui_video_response_parser.go`

必须改：
1. 对 A 响应的提取顺序固定为：
   - `video_url`
   - `video_urls[0]`
   - `media[]` 中 `media_type=video` 的 `url`
2. 禁止把 markdown 文本 URL 作为主路径；仅当上述字段都空时，才回退 `choices[0].message.content`。

文件：`backend/internal/gateway/service/forwarder/video_forwarder.go`

必须改：
1. 对“无视频 URL”错误文案明确化：
   - 保留 task_id 信息
   - 不要返回泛化失败信息掩盖根因
2. `convertVideoOutputToChatOutput` 构建 content 时，确保来源是上一步标准字段提取结果。

现状依据：
- 现在最终错误是 `provider returned no video URL`，但没有把标准字段解析放首位。

---

### 1.5 外部 stream 能力约束明确化
文件：`backend/internal/gateway/service/forward_service.go`
文件：`backend/internal/gateway/service/forwarder/video_forwarder.go`

必须改：
1. 对视频分类请求，`stream=true` 直接返回明确业务错误码（而不是模糊内部错误）。
2. 错误信息固定，例如：
   - `STREAMING_NOT_SUPPORTED_FOR_VIDEO`

现状依据：
- `VideoForwarder.SupportsStreaming()` 返回 `false`，但上层错误归一后仍容易混淆。

---

### 1.6 `/v1/models` 文档与实现一致
文件：`backend/internal/gateway/handler/model.go`
文件：`backend/pkg/response/response.go`

必须改：
1. 若对外宣称 OpenAI 风格，则 `/v1/models` 必须直接返回：
   - `{"object":"list","data":[...]}`
2. 若继续平台包装，则文档必须明确：
   - `{"code":0,"message":"success","data":{"object":"list","data":[...]}}`
3. 二选一，不允许文档/实现双标准。

现状依据：
- 当前 handler 内部数据是 list，但外层由 `response.Success` 包装为 `code/message/data`。

## 2. 单一标准下的 B->A 请求示例（最终）

```json
{
  "model": "video_wan2_complement",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "生成一个变装视频"},
        {"type": "image_url", "image_url": {"url": "https://example.com/1.jpg"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/2.jpg"}}
      ]
    }
  ],
  "stream": false,
  "max_tokens": 150,
  "storage_profile": "prod"
}
```

## 3. B 侧解析规则（最终）

视频任务成功判定条件：
1. `video_url` 非空，且是绝对 URL。  
2. 若 `video_url` 为空，则判失败（不要再以 markdown 文本为主成功依据）。

## 4. 验收标准（全部通过才算完成）

1. B 发给 A 的请求体中包含：
   - `stream=false`
   - `max_tokens`
   - `storage_profile`（有值时）
2. B 能解析 A 返回中的 `video_url`，并稳定返回给调用方。
3. 同一请求不再出现：`parse response: no video URL returned by provider`（除非 A 真无视频产出）。
4. 视频分类下 `stream=true` 外部请求得到明确“能力不支持”错误码。
5. `/v1/models` 文档与实际响应完全一致。

## 5. 当前代码定位（便于开发）
- `chat.go`：`57`, `60`, `63`, `315`
- `message_extractor.go`：`58`, `59`
- `comfyui_video_types.go`：`37-40`
- `comfyui_video_adapter.go`：`57`, `70`, `109`, `174`, `213`
- `video_forwarder.go`：`241`, `251`, `701`, `726`, `752`
- `forward_service.go`：`275`, `305`, `306`
