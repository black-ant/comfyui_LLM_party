# 两套系统改造边界说明（按你的真实链路）

## 1. 链路定义（修正）
当前真实流程是：

`系统B(ant-ai-2api, 最终发起方) -> 系统A(comfyui_LLM_party, 中转插件) -> ComfyUI(不可改)`

对应仓库：
- **系统A**：`d:\code\open_source\comfyui_LLM_party`
- **系统B**：`D:\code\open_source\ant-ai-2api`
- **ComfyUI**：第三方执行端，**不做改造**

---

## 2. 你现在该怎么判断“改哪边”

1. **B 对外契约问题**（给前端/调用方的格式）  
改 **系统B**

2. **A 到 ComfyUI 的适配问题**（怎么从 ComfyUI 取到媒体、上传、拼结果）  
改 **系统A**

3. **ComfyUI 自身行为**  
不改，A/B 兜底兼容

---

## 3. 你提到的问题，逐条归属

| 问题 | 应该改哪里 | 原因 |
|---|---|---|
| `stream=true` 在视频分类不可用 | **系统B** | B 的 `VideoForwarder.SupportsStreaming=false`，由 B 直接拒绝最合理 |
| `image_url` 主要按 `image_url.url` 取值 | **系统B 主改，系统A兜底** | B 是入口解析层；A 加兼容避免直连时踩坑 |
| `storage_profile` 在 B 未打通 | **系统B** | 字段要从 B 入参透传到 A 请求体 |
| `max_tokens` 目前只收不下发 | **系统B** | B 决定是否透传给 A；A 可忽略但不应丢字段 |
| 非流式响应没有 `media/video_url/...` | **系统B 主导协议** | B 可继续只返 `choices.content`，也可升级统一结构化字段 |
| `/v1/models` 是平台包装不是 OpenAI JSON | **系统B** | B 的网关接口规范问题 |

---

## 4. 系统A（当前仓库）已做与应做

### 已做
1. 视频识别增强，避免视频被当成 png。
2. 存储自定义名为 `image.png` 时，视频扩展名仍修正为 `.mp4`。
3. 支持 `image_url` 两种输入：
   - 字符串：`"image_url":"https://..."`
   - 对象：`"image_url":{"url":"https://..."}`
4. 非流式可返回结构化字段（`media/video_url/video_urls/...`），同时保留 `choices[0].message.content`。

### A 侧建议保持
1. **向后兼容优先**：永远保留 `choices[0].message.content`（避免 B 老解析失效）。
2. 新字段只“增不删”，让 B 按节奏升级。

---

## 5. 系统B（ant-ai-2api）最小必改清单

以下是“只改 B 就能先跑稳”的最小集合：

1. **明确拒绝视频流式**
   - 文件：`backend/internal/gateway/service/forward_service.go`
   - 文件：`backend/internal/gateway/service/forwarder/video_forwarder.go`
   - 目标：`comfyui-video + stream=true` 返回固定错误码/文案，避免模糊 `FORWARDING_FAILED`

2. **入口图片格式兼容**
   - 文件：`backend/internal/gateway/service/forwarder/message_extractor.go`
   - 目标：同时解析
     - `image_url: {"url":"..."}`
     - `image_url: "..."`（字符串）

3. **视频 URL 提取增强（兼容 A 新旧返回）**
   - 文件：`backend/internal/provider/comfyui_video/comfyui_video_adapter.go`
   - 文件：`backend/internal/gateway/service/forwarder/video_forwarder.go`
   - 提取顺序建议：
     1) `video_url`
     2) `video_urls[0]`
     3) `media[]` 里 `media_type=video`
     4) `choices[0].message.content` 文本中的 URL（最后兜底）

4. **`storage_profile` 透传（可选，但建议）**
   - 文件：`backend/internal/gateway/handler/chat.go`
   - 文件：`backend/internal/provider/comfyui_video/comfyui_video_types.go`
   - 文件：`backend/internal/provider/comfyui_video/comfyui_video_client.go`
   - 目标：B 请求体带上 `storage_profile` 给 A

5. **`/v1/models` 文档与实现统一**
   - 文件：`backend/internal/gateway/handler/model.go`
   - 文件：`backend/pkg/response/response.go`
   - 目标：明确是“平台包装格式”还是“OpenAI 风格”，二选一并统一文档

---

## 6. 当你说“那些返回我不知道怎么改”时，直接用这条策略

采用 **双轨兼容策略**：

1. **短期（最快上线）**
   - B 继续使用旧逻辑：从 `choices[0].message.content` 解析视频 URL
   - A 保持旧字段不变（已满足）

2. **中期（稳定化）**
   - B 增加对 `video_url/video_urls/media` 的解析
   - 解析不到再回退到 `choices.content`

3. **长期（收敛）**
   - B 对外统一返回结构化视频字段
   - 文档只保留一种官方格式

---

## 7. 一句话结论

- **B 是最终发起方和对外契约层，所以“你不知道怎么改返回”的问题，核心要改 B。**
- **A 负责尽量把 ComfyUI 结果整理得更可解析，并保证兼容。**
- **ComfyUI 不可改，就靠 A/B 做兼容层。**
