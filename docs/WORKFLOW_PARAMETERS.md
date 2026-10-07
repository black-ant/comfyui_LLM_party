# 工作流参数透传

`/v1/chat/completions` 支持通过 `workflow_params` 将生成参数注入 API 格式工作流。

```json
{
  "model": "draw",
  "messages": [{"role": "user", "content": "一只猫"}],
  "stream": true,
  "workflow_params": {
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "fps": 24,
    "duration": 6,
    "seed": 42
  }
}
```

## 默认匹配

服务会将参数匹配到工作流节点中同名的 `inputs`，例如 `seed`、`steps`、`cfg`、`fps`、`duration`、`width` 和 `height`。

`aspect_ratio` 配合 `resolution` 时会自动推导宽高，并按 8 的倍数对齐：

- `16:9 + 720p` → `1280x720`
- `9:16 + 720p` → `720x1280`
- `1:1 + 1024` → `1024x1024`

`aspect_ratio` 接受标准比例字符串（例如 `16:9`、`16/9`、`16x9`）、带 ComfyUI 描述的比例标签（例如 `16:9 (Widescreen)`）以及数字比例（例如 `1.7777778`）。注入工作流时会根据目标输入原有形态自动保持兼容：标签输入使用 ComfyUI 标签，普通字符串输入使用 `16:9`，数字输入使用浮点比例；无法识别的自定义值会原样透传。

如果工作流有多个同名输入，默认会全部更新。需要精确控制时使用 `node_overrides`：

```json
{
  "workflow_params": {
    "node_overrides": {
      "8": {"width": 1280, "height": 720},
      "3": {"seed": 42}
    }
  }
}
```

也可以在工作流 JSON 顶层添加 `__llm_party__` 绑定。该配置只在 Party 侧使用，提交给 ComfyUI 前会被移除：

```json
{
  "__llm_party__": {
    "parameter_bindings": {
      "width": {"node": "8", "input": "target_width"},
      "height": {"node": "8", "input": "target_height"}
    }
  }
}
```

`ant-ai-2api` 的 ComfyUI 视频适配器会透传 `duration`、`resolution`、`fps`、`aspect_ratio` 和 `Params` 到 `workflow_params`。

工作流文件本身是参数来源的唯一事实：部署只同步 `comfyui_LLM_party/workflow_api/` 中实际存在的文件，不按模型名称套用统一工作流配置。当前仓库内的 H3 和 Wan 示例分别按自己的节点连接和默认值执行。

对于预置的 Wan 首尾帧视频工作流，处理器会自动将 `duration`（秒）转换为 `WanFirstLastFrameToVideo.length`（帧数）：

```text
length = round(duration * fps) + 1
```

`fps` 优先使用请求参数；请求未传入时，读取预置 `CreateVideo.fps`。

对于 MiniMax H3 工作流，`duration` 会转换为固定 24fps 下符合模型 `17k+5` 帧网格的 `length`。例如 `duration=4` 会先使用 107 帧完成模型生成，原始时长约为 4.46 秒；Party 随后会在 `SaveVideo`、`VideoTrim`、`VideoCrop` 或末端视频导出节点前自动插入 ComfyUI 核心的 `Video Slice`，将最终视频裁剪为请求的 4.00 秒。运行环境需要包含该核心节点及其视频依赖。

H3 的 `length` 即使已经连接到工作流中的时长/数学节点，也会被请求参数覆盖，避免旧版工作流中固定的 5 秒默认值绕过 API 参数。

如果 H3 工作流没有可识别的末端视频输出节点，或其 `video` 输入不是连线，Party 会在 `workflow_parameters_applied` 日志的 `derived.minimax_h3_trim_missing_reason` 中明确记录，不能据此声称最终输出已经严格裁剪。

运行时可通过环境变量调整诊断行为：`COMFYUI_WORKFLOW_TIMEOUT_SEC` 控制整个工作流等待上限（默认 3600 秒），`COMFYUI_HTTP_TIMEOUT_SEC` 控制 ComfyUI 的单次 HTTP 请求超时（默认 30 秒），`COMFYUI_HISTORY_POLL_INTERVAL_SEC` 控制 history 轮询间隔（默认 0.25 秒）。`history_output_summary.assets` 会记录已拉回视频的实际 `video_duration_seconds`、`video_frame_count` 和 `video_fps`；如果探测失败，会记录 `video_probe_error`。部署时建议同时设置 `FASTAPI_BUILD_TAG` 和 `FASTAPI_BUILD_COMMIT`，启动及每次请求日志会带上实际部署版本。
