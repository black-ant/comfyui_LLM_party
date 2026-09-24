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
    "videoaspectratio": "aspect_ratio",
    "video_aspect_ratio": "aspect_ratio",
    "videoduration": "duration",
    "video_duration": "duration",
    "videofps": "fps",
    "video_fps": "fps",
    "videoresolution": "resolution",
    "video_resolution": "resolution",
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
    "width": ("width",),
    "height": ("height",),
}

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
            node["inputs"][input_name] = value
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
            if key in values:
                applied_keys.add(key)

    for parameter_name, value in values.items():
        if parameter_name in _CONTROL_KEYS or parameter_name in applied_keys:
            continue

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
        node["inputs"][input_name] = value
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
            inputs[input_name] = value
            applied.append(f"{node_id}.{input_name}")
            count += 1
    return count


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
        if _close_ratio(base_width, base_height, ratio):
            return {"width": base_width, "height": base_height}

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
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else None
    text = str(value).strip().lower().replace("／", "/")
    if ":" in text:
        left, right = text.split(":", 1)
    elif "/" in text:
        left, right = text.split("/", 1)
    else:
        try:
            ratio = float(text)
        except ValueError:
            return None
        return ratio if ratio > 0 else None
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


def _align_dimension(value: float) -> int:
    return max(8, int(math.floor(value / 8 + 0.5) * 8))


def _close_ratio(width: int, height: int, ratio: float) -> bool:
    if height <= 0:
        return False
    return abs((width / height) - ratio) < 0.02
