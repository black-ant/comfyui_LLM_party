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

对于预置的 Wan 首尾帧视频工作流，处理器会自动将 `duration`（秒）转换为 `WanFirstLastFrameToVideo.length`（帧数）：

```text
length = round(duration * fps) + 1
```

`fps` 优先使用请求参数；请求未传入时，读取预置 `CreateVideo.fps`。
