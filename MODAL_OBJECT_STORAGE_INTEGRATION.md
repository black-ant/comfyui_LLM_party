# ComfyUI_LLM_party 接入 Modal Object Storage 方案（FastAPI 图片外链）

## 1. 结论

可行，而且是解决你当前 `127.0.0.1` 图片不可下载问题的正确方向。  
核心思路：**工作流产图后不再依赖本地 `output` 目录直链，而是上传到 Modal Object Storage，再返回外网可访问地址**。

---

## 2. 当前问题根因

在 `fast_api.py` 中，图片 URL 是这样拼的：

- `base_url = f"http://{args.host}:{args.port}"`
- `image_url = f"{base_url}/images/{filename}"`

部署在 Modal 后，这里通常还是 `127.0.0.1`，外部自然无法访问。

---

## 3. 目标行为

当工作流返回图片时，流程改为：

1. 拿到二进制图片数据（当前已有 `image_data`）。
2. 调用 Modal Object Storage 上传接口：
   - `POST /api/files/upload`
   - Header: `X-API-Key`, `X-Channel`
   - Form: `file`
   - 可选 Query: `custom_filename`
3. 从返回结果读取 `stored_filename`。
4. 生成下载 URL：`{OBJECT_STORAGE_BASE_URL}/api/files/{stored_filename}`
5. 把该 URL 返回给客户端（`image_urls` 或 markdown 链接）。

---

## 4. 关键前提（必须确认）

根据你给的 `API_GUIDE.md`，下载接口 `GET /api/files/{filename}` 需要 `X-API-Key`。  
这会带来一个现实问题：

- 浏览器直接打开图片链接、或 markdown 图片渲染时，通常**无法自动带自定义 Header**；
- 结果是链接看起来是公网地址，但仍可能 401。

建议二选一：

1. **优先推荐**：对象存储提供“公开读 channel”或“签名 URL（query token）”；  
2. 如果暂时做不到：在你自己的 FastAPI 增加一个代理下载端点（服务端带 `X-API-Key` 去拉，再转发给客户端）。

> 只有解决这一点，返回的外链才能真正“开箱可下载”。

---

## 5. 配置设计（建议）

在 `config.ini` 或环境变量新增：

- `object_storage_base_url`：例如 `https://your-app.modal.run`
- `object_storage_api_key`：对象存储 API key
- `object_storage_channel`：例如 `users` / `comfyui`
- `object_storage_enabled`：`true/false`
- `object_storage_custom_filename`：可选，默认自动命名

建议优先读取环境变量，便于 Modal 部署：

- `OBJECT_STORAGE_BASE_URL`
- `OBJECT_STORAGE_API_KEY`
- `OBJECT_STORAGE_CHANNEL`
- `OBJECT_STORAGE_ENABLED`

此外，已支持 **命令行参数覆盖**（适合你说的“`python xxx.py` 后面直接带变量”）：

- `--public-base-url`
- `--object-storage-enabled`
- `--object-storage-base-url`
- `--object-storage-api-key`
- `--object-storage-channel`
- `--object-storage-custom-filename`

说明：`--object-storage-public-base-url` 仅在“上传地址和对外访问地址不同”时才需要；  
如果两者相同，只传 `--object-storage-base-url` 即可。

优先级为：**命令行参数 > 环境变量 > config.ini**。

### Modal 推荐启动方式（示例）

```bash
python fast_api.py \
  --host 0.0.0.0 \
  --port 8187 \
  --public-base-url "https://your-fastapi.modal.run" \
  --object-storage-enabled true \
  --object-storage-base-url "https://your-storage.modal.run" \
  --object-storage-api-key "$OBJECT_STORAGE_API_KEY" \
  --object-storage-channel users
```

或 `fast_api_v2.py`：

```bash
python fast_api_v2.py \
  --port 8188 \
  --public-base-url "https://your-fastapi.modal.run" \
  --object-storage-enabled true \
  --object-storage-base-url "https://your-storage.modal.run" \
  --object-storage-api-key "$OBJECT_STORAGE_API_KEY" \
  --object-storage-channel users
```

---

## 6. 与现有代码的集成点

主要改动点：

1. `fast_api.py` 的图片返回分支（现在是写本地文件并拼 `/images/...`）
2. `fast_api_v2.py` 的图片分支（同理）

新增一个上传函数（建议复用 `requests`）：

```python
def upload_to_object_storage(image_bytes: bytes, filename: str | None = None) -> str:
    # POST {base}/api/files/upload
    # headers: X-API-Key, X-Channel
    # files: {"file": (..., image_bytes, "image/png")}
    # return: f"{base}/api/files/{stored_filename}"
```

然后在图片处理循环里：

- 有对象存储配置：上传并返回外链；
- 没配置：保留当前本地保存逻辑（向后兼容）。

---

## 7. 返回格式建议

为兼容不同调用方，建议同时返回：

- `image_urls`: `["https://.../api/files/xxx.png"]`
- `content`: 追加 markdown（旧逻辑兼容）

这样 OpenAI 兼容客户端、你自己的前端、以及脚本调用都能用。

---

## 8. 异常与兜底策略

上传失败时建议：

1. 记录错误日志（状态码 + body）
2. 回退到本地保存（不影响主流程）
3. 在响应里附加提示，如 `image_upload_error`

常见错误码处理（来自 API 文档）：

- `401`：key 错误/缺失
- `403`：只读模式
- `409`：文件名冲突（建议自动重试不同文件名）
- `413`：文件过大

---

## 9. 验收清单

1. 调用 `/v1/chat/completions` 触发图片工作流；
2. 响应中的 `image_urls` 不包含 `127.0.0.1`；
3. 外网可直接下载（或可在带鉴权条件下下载）；
4. 对象存储后台能看到上传记录（channel、size、md5）；
5. 对象存储不可用时，接口仍返回结果（走本地兜底）。

---

## 10. 实施顺序（推荐）

1. 先确认“下载鉴权策略”（公开读 / 签名 URL / 代理下载）；
2. 在 `fast_api.py` 完成接入并验证；
3. 同步到 `fast_api_v2.py`；
4. 再做配置文档与部署模板（Modal secrets / env）。
