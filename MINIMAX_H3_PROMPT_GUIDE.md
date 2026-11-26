# MiniMax H3 参考转视频（ref2v）提示词与调用指南

适用工作流：`MiniMax-CK-20261125.json`（`model` 字段填 `MiniMax-CK-20261125`）
参考实现：`comfy_extras/nodes_minimax_h3.py`、`comfy/text_encoders/minimax.py`

本文档解决一个问题：**调用方把图/视频/音频传上来了，但生成的视频"不听话"。**

---

## 0. 一句话结论

H3 的参考素材**不会隐式生效**。图片、视频、音频在模型眼里只是三个"带编号的附件"，
prompt 里必须用 `<Picture i>` / `<Video k>` / `<Audio j>` 这些**字面标签**去点名，
模型才知道"哪张图是主角、哪段视频是动作来源、哪段音频是配乐"。

**不点名 = 模型只会拿第一张图当主角，其余素材基本当背景噪声。**

---

## 1. 这个工作流能吃什么

| 素材 | 上限 | 落到 Start Workflow 的槽位 | prompt 里怎么写 |
|---|---|---|---|
| 参考图 | 9 张 | `img_path1` ~ `img_path9` | `<Picture 1>` ~ `<Picture 9>` |
| 参考视频 | 3 段 | `video_path1` ~ `video_path3` | `<Video 1>` ~ `<Video 3>` |
| 视频自带音轨 | 3 条 | `video_audio_path1` ~ `3`（可覆盖） | 占用 `<Audio n>` 编号 |
| 独立参考音频 | 3 条 | `audio_path1` ~ `audio_path3` | `<Audio n>` |

关键规则：

1. **编号按类型各自从 1 开始**，按请求里出现的顺序排。图是 `<Picture 1,2,3...>`，视频是 `<Video 1,2,3>`。
2. **编号只数"真正传进来的"素材**。比如只传了 2 张图，就只存在 `<Picture 1>`、`<Picture 2>`，不要写 `<Picture 3>`；写了也没有对应素材，模型会自由发挥。
3. **音频编号是共用的**：`<Video 1>` 自带的音轨会先占用一个 `<Audio n>` 编号，独立音频接着往后排。所以只有视频时，它的音轨可能是 `<Audio 1>`。
4. 参考视频按 **24 fps** 解码，最长 15 秒，并且会被截断到本次生成时长；少于 5 帧直接报错。
5. 音频不进文本编码器（Qwen），只作为音频隐变量参与生成——**音频不能靠文字描述替代**，必须真传文件。

---

## 2. 铁律：prompt 是唯一的指令来源

本工作流里，MiniMax H3 节点的 `prompt` 输入接的是 Start Workflow 的 **`user_prompt`**（输出槽 20）。

因此：

| 字段 | 是否生效 | 说明 |
|---|---|---|
| `messages[].content` 里的 `{"type":"text"}` | ✅ 生效 | 这就是 H3 的 prompt，唯一可用的指令通道 |
| `system_prompt` | ❌ 不生效 | 不会传给 H3 |
| `positive_prompt` | ❌ 不生效 | 服务端固定为空串 |
| `negative_prompt` | ❌ 不生效 | 服务端固定为空串，**不要指望它排除元素** |

推论：
- 想"去掉水印 / 不要文字 / 不要变形"，得在 prompt 正文里用正向描述约束，不能靠 negative。
- 想控制风格，把风格词写进 prompt 正文。

---

## 3. prompt 的正确结构

H3 看到的完整序列是（按请求顺序）：

```text
<Picture 1>: <图片1的视觉特征>
<Picture 2>: <图片2的视觉特征>
<Audio 1>:                       <- 只有占位，音频不进文本
<Video 1>: <0.0 seconds><视觉块><0.5 seconds><视觉块>...
<你的 prompt 正文>
```

注意：**参考素材排在正文前面**，正文必须"回指"它们才有意义。

推荐的正文结构（四段式，缺一段效果就明显下降）：

```text
【主体绑定】<Picture 1> 中的 X 是唯一主角，必须保持 <具体特征：脸型/五官/发型/服装/配色> 不变。
【素材分工】<Picture 2> 只提供 Y 的参考（结构/材质/涂装/场景）；<Video 1> 只提供动作节奏与运镜。
【动作与镜头】按时间顺序写：先……，然后……，最后……。镜头……（推/拉/摇/跟），景别……
【风格与声音】画面风格……，光线……，声音……（环境音/无对白/配乐类型）。
```

---

## 4. 直接可用的模板

### 模板 A：角色一致性 + 多图分工（最常见）

```text
<Picture 1> 中的小宝宝是画面唯一主角，必须严格保留其脸型、五官、发型和肤色；
<Picture 2> 作为机甲与面罩的结构、材质和涂装参考，不要改变 <Picture 1> 的脸。
场景：机甲在半空对战，火花四溅；镜头从全景推进到面罩特写，面罩缓缓打开，露出 <Picture 1> 中的宝宝脸部。
风格：电影级写实，冷色调金属高光，浅景深；环境音为引擎轰鸣与金属碰撞，无对白。
```

### 模板 B：参考视频驱动动作（图 + 视频）

```text
动作、节奏与运镜严格参考 <Video 1>（包括起始姿态、运动轨迹、镜头运动）；
人物外观以 <Picture 1> 为准：把 <Video 1> 中的角色替换为 <Picture 1> 的人物，保持其五官与服装不变。
背景保持与 <Video 1> 一致；风格：真实电影感，手持轻微晃动。
```

### 模板 C：双图 = 变身前后

```text
<Picture 1> 是变身前的形态，<Picture 2> 是变身后的形态；
过程：主体从 <Picture 1> 的形态开始，装甲逐块展开合体，最终变成 <Picture 2> 的形态，最后面罩打开露出内部的脸。
镜头：从中景固定机位开始，随合体过程缓慢推近到面部特写。
```

### 模板 D：音频/口型对齐

```text
主体为 <Picture 1> 中的人物，正对镜头说话；
口型与节奏严格对齐 <Audio 1> 的语音（共 X 秒），说完后微笑并轻微点头。
镜头固定，浅景深，室内暖光；除 <Audio 1> 的人声外只保留轻微环境底噪。
```

---

## 5. 反例（这些写法一定出问题）

| 反例 | 后果 |
|---|---|
| `参考图1的宝宝，参考图2的机甲` | ❌ 没有 `<Picture i>` 标签。模型不认"图1/图2"这种中文说法，通常只吃第一张，第二张基本无效 |
| `机甲对战，然后打开面罩`（8 个字） | ❌ 没有任何素材指代，模型完全自由发挥，参考素材形同虚设 |
| 要求写在 `system` 或 `negative_prompt` | ❌ 字段不生效 |
| `不要出现文字`只写在 negative | ❌ negative 恒为空，写了没用 |
| 传 2 张图但只写了 `<Picture 1>` | ❌ 第二张图仍是"无主素材"，观感上就是"第二张没进去" |
| 传了 3 张图却写 `<Picture 1>` `<Picture 4>` | ❌ 跳号，`<Picture 4>` 没有对应素材 |
| prompt 里写 `<Video 1>` 但没传视频 | ❌ 视频槽是空的，模型不知道在跟什么 |

---

## 6. 参数怎么传

`workflow_params` 里的键会写进 Start Workflow 节点：

```json
"workflow_params": {
  "aspect_ratio": "9:16",
  "duration": 5,
  "resolution": "720p",
  "steps": 20
}
```

### 6.1 尺寸（优先级从高到低）

1. `size`：显式宽高，形如 `"640x1152"`
2. `resolution`：`"480p" / "720p" / "1080p" / "2k" / "4k"`（按短边），或 `0.1 ~ 100` 的**百万像素数**（如 `0.7` = 0.7MP）
3. `width` + `height` 控件
4. `aspect_ratio`：`"9:16" / "16:9" / "1:1"` 等，与上面的短边/像素数联合换算

最终宽高会**向上对齐到 32 的倍数**。

⚠️ 陷阱：`resolution` 和 `width/height` 混用会让结果尺寸跳变。
- `resolution: 0.7` + `aspect_ratio: 9:16` → **640×1120**
- 不给 `resolution`，只给 `width:640 height:1152` + `aspect_ratio: 9:16` → 按短边 640 反推 → **640×1152**

所以要么只用 `resolution`，要么只用 `width/height`，不要两个都给。

### 6.2 时长

- 建议 **5 ~ 15 秒**（模型训练区间约 124 ~ 362 帧）。
- 实际帧数 `length = round(duration × 24)` 再向上补齐到 `17k + 5`：
  - `5s` → 120 → **124 帧**（≈5.17s）
  - `4s` → 96 → **107 帧**（≈4.46s）
- 工作流末尾有 `Video Slice` 节点，会把结果**严格裁到你要求的整秒数**（`strict_duration: true`），所以 `5s` 最终出 5.0 秒，不用担心多出 0.17 秒。
- 超过 15 秒会超出训练区间，画质与一致性都会掉。

### 6.3 采样步数

- `steps: 15 ~ 20` 是常用区间；开 Lightning LoRA（工作流里 `146` 节点设为 `true`）时会被压到 4 步。
- 步数越大越慢，一致性略好。

---

## 7. 参考图质量要求

- **比例尽量贴近生成比例**（9:16 就传 9:16 的图），减少缩放损失。
- **每张图要有明确且不同的用途**。两张图内容高度重叠时，模型会把它们平均掉，看起来就像"只有一张生效"。
- `ref_image_size` 开关（节点 136，默认 `match`）：
  - `match`：把每张参考图缩到本次生成的像素面积（如 640×1120≈0.72MP），快，但全身/远景细节会丢。
  - `max`：短边按 2048 处理，身份和细节还原最好，**代价是慢几倍**（参考 token 会参与每一步采样）。
  - 结论：**脸部/角色一致性优先时用 `max`；只借构图或风格时用 `match`。**

---

## 8. 完整请求示例

### 8.1 两张图（角色 + 机甲）

```bash
curl -X POST "http://<host>:8187/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "model": "MiniMax-CK-20261125",
    "stream": false,
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "<Picture 1> 中的小宝宝是画面唯一主角，必须严格保留其脸型、五官和发型；<Picture 2> 作为机甲与面罩的结构与涂装参考。场景：机甲在半空对战，镜头推进到面罩特写并缓缓打开，露出 <Picture 1> 中的宝宝脸部。电影级写实，冷色调金属高光；环境音为引擎轰鸣，无对白。"},
        {"type": "image_url", "image_url": {"url": "https://.../baby.jpeg"}},
        {"type": "image_url", "image_url": {"url": "https://.../mech.jpeg"}}
      ]
    }],
    "workflow_params": {"aspect_ratio": "9:16", "duration": 5, "resolution": "720p", "steps": 20}
  }'
```

### 8.2 图 + 视频（视频驱动动作）

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "动作、节奏与运镜严格参考 <Video 1>；人物外观以 <Picture 1> 为准，把 <Video 1> 中的角色替换为 <Picture 1> 的人物并保持其五官与服装不变。背景与 <Video 1> 一致，真实电影感。"},
    {"type": "image_url", "image_url": {"url": "https://.../character.jpeg"}},
    {"type": "video_url", "video_url": {"url": "https://.../motion.mp4"}}
  ]
}
```

素材落位规则（服务端行为）：

- 图片按出现顺序 → `img_path1..img_path9`（超过 9 张丢弃，日志 `reference_media_truncated`）
- 视频按出现顺序 → `video_path1..video_path3`
- 音频按出现顺序 → `audio_path1..audio_path3`

---

## 9. 提交前自检清单

- [ ] prompt 里对**每一份**参考素材都写了对应 tag，数量与实际传入一致
- [ ] tag 编号从 1 连续，没有跳号
- [ ] 每张参考图在 prompt 里有**不同**的用途说明
- [ ] 没有把要求写在 `system` / `negative_prompt` 里
- [ ] `duration` 在 5 ~ 15 之间
- [ ] `resolution` 与 `width/height` 没有混用
- [ ] 参考图比例接近目标比例，主体清晰
- [ ] 需要强身份还原时，确认 `ref_image_size` 设成 `max`

---

## 10. 出问题时怎么自查日志

按顺序看这几条（都在服务端日志里）：

1. `[messages_parsed]` → `image_count` / `video_count` / `video_sources`
   **视频数量为 0 就是客户端没传上来**，此时 prompt 里写 `<Video 1>` 毫无意义。
2. `[start_workflow_injected_summary]` → `img_paths` / `video_paths` / `audio_paths`
   这是真正写进节点的槽位，空字符串 = 该槽位没有素材。
3. `[param_effectiveness]` → 每个参数是否 `effective: true`；`downstream_linked: false` 说明该参数只写进 Start Workflow、没有连线到下游节点（`aspect_ratio`/`duration`/`resolution` 常是这种，属于正常，它们由节点内部换算成 width/height/frame_count 再下游）。
4. `[workflow_execution_started]` → `workflow_snapshot`，确认 `length` 与 `136.length` 的派生值。

⚠️ 已知会误导排查的字段：
- `start_workflow_injected` 里的 `image_input`（`count`）**是无效键**，节点实际输入名是 `image_input1/2`，ComfyUI 会忽略这个字段，不能用来判断"图进了几张"。
- 服务端**只解析最后一条 message** 的 `content`。素材如果放在更早的 message 里，会被静默丢弃。
- `content.type` 只认这些白名单，其它 type **静默忽略且不留日志**：
  - 图片：`image_url` / `first_frame` / `first_frame_image` / `last_frame` / `last_frame_image` / `end_frame` / `end_frame_image`
  - 视频：`video_url` / `input_video` / `video` / `reference_video` / `first_video`
  - 音频：`audio_url` / `input_audio` / `audio` / `reference_audio`

⚠️ 注意：本工作流是 **ref2v（参考转视频）**，不是首尾帧工作流。
即使你用 `first_frame` / `last_frame` 这种 type 传图，它们也只会被当成**普通参考图**，不会真正锁定首帧/尾帧。
要锁首尾帧请用专门的首尾帧工作流。
