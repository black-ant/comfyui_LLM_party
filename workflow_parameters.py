import math
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


_CONTROL_KEYS = {
    "bindings",
    "dimension_bindings",
    "node_overrides",
    "nodes",
    "parameter_bindings",
    "values",
}

_KEY_ALIASES = {
    "aspectratio": "aspect_ratio",
    "aspect_ratio": "aspect_ratio",
    "ratio": "aspect_ratio",
    "videoaspectratio": "aspect_ratio",
    "video_aspect_ratio": "aspect_ratio",
    "videoduration": "duration",
    "video_duration": "duration",
    "videofps": "fps",
    "video_fps": "fps",
    "videoresolution": "resolution",
    "video_resolution": "resolution",
    "videosize": "size",
    "video_size": "size",
    "megapixel": "megapixels",
    "megapixels": "megapixels",
    "video_megapixels": "megapixels",
    "framerate": "fps",
    "frame_rate": "fps",
    "framespersecond": "fps",
    "frame_rate_fps": "fps",
}

_INPUT_ALIASES = {
    "aspect_ratio": ("aspect_ratio", "aspectRatio", "video_aspect_ratio", "videoAspectRatio", "ratio"),
    "duration": ("duration", "seconds", "video_duration", "videoDuration"),
    "fps": ("fps", "frame_rate", "frameRate", "video_fps", "videoFps"),
    "resolution": ("resolution", "video_resolution", "videoResolution"),
    "megapixels": ("megapixels", "mega_pixels", "megapixel"),
    "width": ("width",),
    "height": ("height",),
}

_MINIMAX_H3_LENGTH_NODES = frozenset({
    "EmptyMiniMaxH3LatentAV",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
})
_MINIMAX_H3_FPS = 24
_VIDEO_OUTPUT_NODES = frozenset({
    "SaveVideo",
    "VideoCrop",
    "VideoOutput",
    "VideoTrim",
})
_VIDEO_INTERMEDIATE_NODES = frozenset({
    "ConcatenateVideo",
    "CreateVideo",
    "GetVideoComponents",
    "LoadVideo",
})
_VIDEO_SLICE_NODES = frozenset({"Video Slice", "VideoSlice"})

_ASPECT_RATIO_INPUT_NAMES = frozenset(_INPUT_ALIASES["aspect_ratio"])
_ASPECT_RATIO_MATCH_TOLERANCE = 1e-6
_ASPECT_RATIO_PRESETS = (
    (1.0, "1:1", "1:1 (Square)"),
    (2.0 / 3.0, "2:3", "2:3 (Portrait Photo)"),
    (3.0 / 2.0, "3:2", "3:2 (Photo)"),
    (3.0 / 4.0, "3:4", "3:4 (Portrait Standard)"),
    (4.0 / 3.0, "4:3", "4:3 (Standard)"),
    (9.0 / 16.0, "9:16", "9:16 (Portrait Widescreen)"),
    (16.0 / 9.0, "16:9", "16:9 (Widescreen)"),
    (21.0 / 9.0, "21:9", "21:9 (Ultrawide)"),
)

_RESOLUTION_SHORT_EDGES = {
    "sd": 480,
    "480p": 480,
    "hd": 720,
    "720p": 720,
    "fhd": 1080,
    "1080p": 1080,
    "2k": 1440,
    "1440p": 1440,
    "4k": 2160,
    "2160p": 2160,
}


def normalize_workflow_parameters(*sources: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue

        nested_values = source.get("values")
        if isinstance(nested_values, Mapping):
            result.update(nested_values)

        nested_params = source.get("params")
        if isinstance(nested_params, Mapping):
            result.update(nested_params)

        for key, value in source.items():
            if key not in {"values", "params"}:
                result[key] = value
    return {_canonical_key(key): value for key, value in result.items()}


def apply_workflow_parameters(
    prompt: Dict[str, Any],
    parameters: Optional[Mapping[str, Any]],
    workflow_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    values = normalize_workflow_parameters(parameters)
    report: Dict[str, Any] = {
        "requested": sorted(values.keys()),
        "applied": [],
        "ignored": [],
        "derived": {},
    }
    if not values:
        return report

    locked_targets = set()
    applied_keys = set()

    for node_id, overrides in _iter_node_overrides(values):
        node = prompt.get(str(node_id))
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), dict):
            report["ignored"].append(f"node:{node_id}")
            continue
        for input_name, value in overrides.items():
            target = (str(node_id), str(input_name))
            if input_name not in node["inputs"]:
                report["ignored"].append(f"{node_id}.{input_name}")
                continue
            node["inputs"][input_name] = _coerce_input_value(node, input_name, value)
            locked_targets.add(target)
            report["applied"].append(f"{node_id}.{input_name}")

    bindings = _collect_bindings(prompt, workflow_config, values)
    for parameter_name, binding in bindings.items():
        canonical_name = _canonical_key(parameter_name)
        if canonical_name not in values:
            continue
        applied = _apply_binding(
            prompt,
            binding,
            values[canonical_name],
            locked_targets,
            report["applied"],
        )
        if applied:
            applied_keys.add(canonical_name)

    derived_dimensions = _derive_dimensions(values)
    if derived_dimensions:
        report["derived"].update(derived_dimensions)
        for key, value in derived_dimensions.items():
            if key not in values:
                values[key] = value
        for key in ("aspect_ratio", "ratio", "resolution", "size"):
            if key not in values:
                continue
            if key == "resolution":
                _apply_resolution_input(
                    prompt,
                    values[key],
                    locked_targets,
                    report["applied"],
                )
            else:
                aliases = _INPUT_ALIASES.get(key, (key,))
                _apply_matching_inputs(
                    prompt,
                    aliases,
                    values[key],
                    locked_targets,
                    report["applied"],
                )
            applied_keys.add(key)

    if _apply_preset_duration(
        prompt,
        values,
        locked_targets,
        report['applied'],
        report['derived'],
    ):
        applied_keys.add('duration')

    if report["derived"].get("minimax_h3_length") is not None:
        _apply_minimax_h3_exact_duration_trim(
            prompt,
            values.get("duration"),
            report["applied"],
            report["derived"],
        )

    for parameter_name, value in values.items():
        if parameter_name in _CONTROL_KEYS or (
            parameter_name in applied_keys and parameter_name != 'duration'
        ):
            continue

        if parameter_name == "resolution":
            targets = _apply_resolution_input(
                prompt,
                value,
                locked_targets,
                report["applied"],
            )
        else:
            aliases = _INPUT_ALIASES.get(parameter_name, (parameter_name,))
            targets = _apply_matching_inputs(
                prompt,
                aliases,
                value,
                locked_targets,
                report["applied"],
            )
        if targets:
            applied_keys.add(parameter_name)

    for parameter_name in values:
        if parameter_name in _CONTROL_KEYS:
            continue
        if parameter_name not in applied_keys:
            report["ignored"].append(parameter_name)

    report["applied"] = sorted(set(report["applied"]))
    report["ignored"] = sorted(set(report["ignored"]))
    return report


def _canonical_key(key: Any) -> str:
    text = str(key)
    return _KEY_ALIASES.get(text, _KEY_ALIASES.get(text.lower(), text))


def _iter_nodes(prompt: Mapping[str, Any]) -> Iterable[Tuple[str, Mapping[str, Any]]]:
    for node_id, node in prompt.items():
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), dict):
            continue
        if not node.get("class_type"):
            continue
        yield str(node_id), node


def _is_linked_input(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[1], int)
        and isinstance(value[0], (str, int))
    )


def _iter_node_overrides(parameters: Mapping[str, Any]) -> Iterable[Tuple[str, Mapping[str, Any]]]:
    raw_overrides = parameters.get("node_overrides") or parameters.get("nodes")
    if not isinstance(raw_overrides, Mapping):
        return
    for node_id, raw_inputs in raw_overrides.items():
        if isinstance(raw_inputs, Mapping) and isinstance(raw_inputs.get("inputs"), Mapping):
            yield str(node_id), raw_inputs["inputs"]
        elif isinstance(raw_inputs, Mapping):
            yield str(node_id), raw_inputs


def _collect_bindings(
    prompt: Mapping[str, Any],
    workflow_config: Optional[Mapping[str, Any]],
    parameters: Mapping[str, Any],
) -> Dict[str, Any]:
    candidates = []
    if isinstance(parameters.get("bindings"), Mapping):
        candidates.append(parameters["bindings"])
    if isinstance(parameters.get("parameter_bindings"), Mapping):
        candidates.append(parameters["parameter_bindings"])
    if isinstance(workflow_config, Mapping):
        for key in ("bindings", "parameter_bindings"):
            if isinstance(workflow_config.get(key), Mapping):
                candidates.append(workflow_config[key])

    for _, node in _iter_nodes(prompt):
        metadata = node.get("_meta")
        if not isinstance(metadata, Mapping):
            continue
        llm_party = metadata.get("llm_party")
        if not isinstance(llm_party, Mapping):
            continue
        for key in ("bindings", "parameter_bindings"):
            if isinstance(llm_party.get(key), Mapping):
                candidates.append(llm_party[key])

    merged: Dict[str, Any] = {}
    for candidate in reversed(candidates):
        merged.update(candidate)
    return merged


def _apply_binding(
    prompt: Mapping[str, Any],
    binding: Any,
    value: Any,
    locked_targets: set,
    applied: list,
) -> bool:
    targets = _binding_targets(binding)
    applied_any = False
    for node_id, input_name in targets:
        node = prompt.get(str(node_id))
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), dict):
            continue
        if input_name not in node["inputs"]:
            continue
        if (str(node_id), str(input_name)) in locked_targets:
            continue
        node["inputs"][input_name] = _coerce_input_value(node, input_name, value)
        locked_targets.add((str(node_id), str(input_name)))
        applied.append(f"{node_id}.{input_name}")
        applied_any = True
    return applied_any


def _binding_targets(binding: Any) -> Iterable[Tuple[str, str]]:
    if isinstance(binding, Mapping):
        if isinstance(binding.get("targets"), list):
            for target in binding["targets"]:
                yield from _binding_targets(target)
            return
        node_id = binding.get("node_id", binding.get("node"))
        input_name = binding.get("input", binding.get("input_name"))
        if node_id is not None and input_name is not None:
            yield str(node_id), str(input_name)
            return
        for node_id, input_names in binding.items():
            if isinstance(input_names, str):
                yield str(node_id), input_names
            elif isinstance(input_names, list):
                for input_name in input_names:
                    yield str(node_id), str(input_name)
        return
    if isinstance(binding, list):
        for item in binding:
            yield from _binding_targets(item)
        return
    if isinstance(binding, str) and "." in binding:
        node_id, input_name = binding.split(".", 1)
        yield node_id, input_name


def _apply_matching_inputs(
    prompt: Mapping[str, Any],
    aliases: Iterable[str],
    value: Any,
    locked_targets: set,
    applied: list,
) -> int:
    alias_set = set(aliases)
    count = 0
    for node_id, node in _iter_nodes(prompt):
        inputs = node["inputs"]
        for input_name in list(inputs.keys()):
            if input_name not in alias_set or (node_id, input_name) in locked_targets:
                continue
            if _is_linked_input(inputs[input_name]):
                continue
            inputs[input_name] = _coerce_input_value(node, input_name, value)
            applied.append(f"{node_id}.{input_name}")
            count += 1
    return count


def _coerce_input_value(node: Mapping[str, Any], input_name: str, value: Any) -> Any:
    if input_name not in _ASPECT_RATIO_INPUT_NAMES:
        return value

    inputs = node.get("inputs")
    current_value = inputs.get(input_name) if isinstance(inputs, Mapping) else None
    use_labeled_default = node.get("class_type") == "ResolutionSelector"
    return _normalize_aspect_ratio_for_target(value, current_value, use_labeled_default)


def _normalize_aspect_ratio_for_target(
    value: Any,
    current_value: Any,
    use_labeled_default: bool,
) -> Any:
    ratio = _parse_aspect_ratio(value)
    if ratio is None or isinstance(current_value, bool):
        return value

    preset = _find_aspect_ratio_preset(ratio)
    if preset is None:
        return value

    _, raw_value, labeled_value = preset
    if use_labeled_default:
        return labeled_value
    if isinstance(current_value, (int, float)):
        return ratio
    if _is_labeled_aspect_ratio(current_value):
        return labeled_value
    if isinstance(current_value, str) or current_value is None:
        return raw_value
    return value


def _find_aspect_ratio_preset(ratio: float):
    for preset in _ASPECT_RATIO_PRESETS:
        if math.isclose(
            ratio,
            preset[0],
            rel_tol=_ASPECT_RATIO_MATCH_TOLERANCE,
            abs_tol=_ASPECT_RATIO_MATCH_TOLERANCE,
        ):
            return preset
    return None


def _is_labeled_aspect_ratio(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return "(" in text and ")" in text and _parse_aspect_ratio(text) is not None


def _apply_resolution_input(
    prompt: Mapping[str, Any],
    value: Any,
    locked_targets: set,
    applied: list,
) -> int:
    megapixels = _positive_float(value)
    if megapixels is not None:
        targets = _apply_matching_inputs(
            prompt,
            _INPUT_ALIASES["megapixels"],
            megapixels,
            locked_targets,
            applied,
        )
        if targets:
            return targets

    return _apply_matching_inputs(
        prompt,
        _INPUT_ALIASES["resolution"],
        value,
        locked_targets,
        applied,
    )


def _apply_preset_duration(
    prompt: Mapping[str, Any],
    parameters: Mapping[str, Any],
    locked_targets: set,
    applied: list,
    derived: Dict[str, Any],
) -> bool:
    duration = _positive_float(parameters.get('duration'))
    if duration is None:
        return False

    fps = _positive_float(parameters.get('fps'))
    if fps is None:
        fps = _find_node_input(prompt, 'CreateVideo', 'fps')
    wan_length = max(1, int(round(duration * fps)) + 1) if fps is not None else None
    minimax_h3_length = _minimax_h3_length(duration)
    applied_any = False
    found_wan_node = False
    found_minimax_h3_node = False
    for node_id, node in _iter_nodes(prompt):
        class_type = node.get('class_type')
        is_minimax_h3 = _is_minimax_h3_length_node(node)
        if class_type == 'WanFirstLastFrameToVideo':
            found_wan_node = True
            length = wan_length
        elif is_minimax_h3:
            found_minimax_h3_node = True
            length = minimax_h3_length
        else:
            continue
        if length is None:
            continue
        if 'length' not in node['inputs'] or (node_id, 'length') in locked_targets:
            continue
        if not is_minimax_h3 and _is_linked_input(node['inputs']['length']):
            continue
        node['inputs']['length'] = length
        locked_targets.add((node_id, 'length'))
        applied.append(f'{node_id}.length')
        applied_any = True

    if found_wan_node and wan_length is not None:
        derived['wan_length'] = wan_length
    if found_minimax_h3_node:
        derived['minimax_h3_length'] = minimax_h3_length
        derived['minimax_h3_fps'] = _MINIMAX_H3_FPS
        derived['minimax_h3_duration_seconds'] = round(
            minimax_h3_length / _MINIMAX_H3_FPS,
            3,
        )
    return applied_any


def _minimax_h3_length(duration: float) -> int:
    requested_frames = max(5, int(round(duration * _MINIMAX_H3_FPS)))
    return requested_frames + (5 - requested_frames) % 17


def _is_minimax_h3_length_node(node: Mapping[str, Any]) -> bool:
    if not isinstance(node, Mapping):
        return False
    inputs = node.get('inputs')
    if not isinstance(inputs, Mapping) or 'length' not in inputs:
        return False
    class_type = str(node.get('class_type') or '')
    if class_type in _MINIMAX_H3_LENGTH_NODES:
        return True
    normalized = re.sub(r'[^a-z0-9]', '', class_type.lower())
    return 'minimaxh3' in normalized


def _apply_minimax_h3_exact_duration_trim(
    prompt: Dict[str, Any],
    duration_value: Any,
    applied: list,
    derived: Dict[str, Any],
) -> bool:
    duration = _positive_float(duration_value)
    if duration is None:
        return False

    trim_node_ids = []
    output_node_ids = []
    next_node_id = _next_numeric_node_id(prompt)
    consumed_node_ids = _collect_consumed_node_ids(prompt)
    for node_id, node in list(_iter_nodes(prompt)):
        if not _is_video_output_node(node_id, node, consumed_node_ids):
            continue
        video_link = node["inputs"].get("video")
        if not _is_linked_input(video_link):
            continue

        output_node_ids.append(node_id)
        source_node_id = str(video_link[0])
        source_node = prompt.get(source_node_id)
        if (
            isinstance(source_node, Mapping)
            and str(source_node.get("class_type") or "") in _VIDEO_SLICE_NODES
            and isinstance(source_node.get("inputs"), dict)
        ):
            source_node["inputs"]["start_time"] = 0.0
            source_node["inputs"]["duration"] = duration
            source_node["inputs"]["strict_duration"] = True
            trim_node_ids.append(source_node_id)
            applied.extend(
                [
                    f"{source_node_id}.start_time",
                    f"{source_node_id}.duration",
                    f"{source_node_id}.strict_duration",
                ]
            )
            continue

        while str(next_node_id) in prompt:
            next_node_id += 1
        trim_node_id = str(next_node_id)
        next_node_id += 1
        prompt[trim_node_id] = {
            "inputs": {
                "video": video_link,
                "start_time": 0.0,
                "duration": duration,
                "strict_duration": True,
            },
            "class_type": "Video Slice",
            "_meta": {
                "title": "LLM Party exact video duration",
            },
        }
        node["inputs"]["video"] = [trim_node_id, 0]
        trim_node_ids.append(trim_node_id)
        applied.extend(
            [
                f"{trim_node_id}.start_time",
                f"{trim_node_id}.duration",
                f"{trim_node_id}.strict_duration",
            ]
        )

    derived["minimax_h3_exact_duration_seconds"] = duration
    derived["minimax_h3_trim_node_ids"] = trim_node_ids
    derived["minimax_h3_trim_output_node_ids"] = output_node_ids
    derived["minimax_h3_trim_save_node_ids"] = output_node_ids
    if not trim_node_ids:
        derived["minimax_h3_trim_missing_reason"] = (
            "no linked video output node (SaveVideo, VideoTrim, VideoCrop, VideoOutput, or terminal export node)"
        )
    return bool(trim_node_ids)


def _collect_consumed_node_ids(prompt: Mapping[str, Any]) -> set:
    consumed_node_ids = set()
    for _, node in _iter_nodes(prompt):
        for value in node["inputs"].values():
            if _is_linked_input(value):
                consumed_node_ids.add(str(value[0]))
    return consumed_node_ids


def _is_video_output_node(
    node_id: str,
    node: Mapping[str, Any],
    consumed_node_ids: set,
) -> bool:
    class_type = str(node.get("class_type") or "")
    if class_type in _VIDEO_OUTPUT_NODES:
        return True
    if class_type in _VIDEO_SLICE_NODES or class_type in _VIDEO_INTERMEDIATE_NODES:
        return False
    if str(node_id) in consumed_node_ids:
        return False

    metadata = node.get("_meta")
    title = metadata.get("title", "") if isinstance(metadata, Mapping) else ""
    normalized = re.sub(
        r"[^a-z0-9]",
        "",
        f"{class_type} {title}".lower(),
    )
    return any(
        marker in normalized
        for marker in ("savevideo", "previewvideo", "videooutput", "exportvideo")
    )


def _next_numeric_node_id(prompt: Mapping[str, Any]) -> int:
    numeric_ids = []
    for node_id in prompt:
        try:
            numeric_ids.append(int(str(node_id)))
        except (TypeError, ValueError):
            continue
    return max(numeric_ids, default=0) + 1


def _find_node_input(
    prompt: Mapping[str, Any],
    class_type: str,
    input_name: str,
) -> Optional[float]:
    for _, node in _iter_nodes(prompt):
        if node.get('class_type') != class_type:
            continue
        value = _positive_float(node['inputs'].get(input_name))
        if value is not None:
            return value
    return None


def _derive_dimensions(parameters: Mapping[str, Any]) -> Dict[str, int]:
    width = _positive_int(parameters.get("width"))
    height = _positive_int(parameters.get("height"))
    ratio = _parse_aspect_ratio(parameters.get("aspect_ratio", parameters.get("ratio")))
    if width and height:
        return {}
    if not ratio:
        parsed_resolution = _parse_resolution_dimensions(
            parameters.get("resolution", parameters.get("size"))
        )
        if parsed_resolution:
            return {
                "width": parsed_resolution[0] if not width else width,
                "height": parsed_resolution[1] if not height else height,
            }
        return {}

    resolution = parameters.get("resolution", parameters.get("size"))
    parsed_resolution = _parse_resolution_dimensions(resolution)
    if parsed_resolution and not (width or height):
        base_width, base_height = parsed_resolution
        return {"width": base_width, "height": base_height}

    if _positive_float(resolution) is not None and not (width or height):
        return {}

    short_edge = _parse_short_edge(resolution) or 1024
    if width:
        height = _align_dimension(width / ratio)
    elif height:
        width = _align_dimension(height * ratio)
    elif ratio >= 1:
        height = _align_dimension(short_edge)
        width = _align_dimension(height * ratio)
    else:
        width = _align_dimension(short_edge)
        height = _align_dimension(width / ratio)
    return {"width": max(width or 8, 8), "height": max(height or 8, 8)}


def _parse_aspect_ratio(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ratio = float(value)
        return ratio if math.isfinite(ratio) and ratio > 0 else None

    text = (
        str(value)
        .strip()
        .lower()
        .replace("／", "/")
        .replace("：", ":")
    )
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*[:/x×]\s*(\d+(?:\.\d+)?)(?:\s*\([^)]*\))?",
        text,
    )
    if match:
        left, right = match.groups()
    else:
        try:
            ratio = float(text)
        except ValueError:
            return None
        return ratio if math.isfinite(ratio) and ratio > 0 else None
    try:
        left_value = float(left.strip())
        right_value = float(right.strip())
    except ValueError:
        return None
    if left_value <= 0 or right_value <= 0:
        return None
    return left_value / right_value


def _parse_resolution_dimensions(value: Any) -> Optional[Tuple[int, int]]:
    if value is None:
        return None
    text = str(value).strip().lower().replace("×", "x")
    match = re.fullmatch(r"(\d+)\s*x\s*(\d+)", text)
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return _align_dimension(width), _align_dimension(height)


def _parse_short_edge(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _RESOLUTION_SHORT_EDGES:
        return _RESOLUTION_SHORT_EDGES[text]
    match = re.fullmatch(r"(\d+)p", text)
    if match:
        return int(match.group(1))
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return None


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _align_dimension(value: float) -> int:
    return max(8, int(math.floor(value / 8 + 0.5) * 8))
