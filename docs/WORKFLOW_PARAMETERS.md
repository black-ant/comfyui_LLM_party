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
