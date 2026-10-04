import unittest

from workflow_parameters import apply_workflow_parameters


class WorkflowParametersTest(unittest.TestCase):
    def test_wan_preset_converts_duration_to_length_using_preset_fps(self):
        prompt = {
            '81': {
                'class_type': 'WanFirstLastFrameToVideo',
                'inputs': {'length': 81},
            },
            '86': {
                'class_type': 'CreateVideo',
                'inputs': {'fps': 16},
            },
        }

        report = apply_workflow_parameters(prompt, {'duration': 6})

        self.assertEqual(prompt['81']['inputs']['length'], 97)
        self.assertEqual(report['derived']['wan_length'], 97)
        self.assertEqual(report['ignored'], [])

    def test_wan_preset_uses_requested_fps_for_duration_conversion(self):
        prompt = {
            '81': {
                'class_type': 'WanFirstLastFrameToVideo',
                'inputs': {'length': 81},
            },
            '86': {
                'class_type': 'CreateVideo',
                'inputs': {'fps': 16},
            },
        }

        report = apply_workflow_parameters(prompt, {'duration': 5, 'fps': 24})

        self.assertEqual(prompt['81']['inputs']['length'], 121)
        self.assertEqual(prompt['86']['inputs']['fps'], 24)
        self.assertEqual(report['ignored'], [])

    def test_wan_duration_conversion_preserves_direct_duration_inputs(self):
        prompt = {
            '81': {
                'class_type': 'WanFirstLastFrameToVideo',
                'inputs': {'length': 81},
            },
            '86': {
                'class_type': 'CreateVideo',
                'inputs': {'fps': 16},
            },
            '12': {
                'class_type': 'VideoConfig',
                'inputs': {'duration': 4},
            },
        }

        report = apply_workflow_parameters(prompt, {'duration': 6})

        self.assertEqual(prompt['81']['inputs']['length'], 97)
        self.assertEqual(prompt['12']['inputs']['duration'], 6)
        self.assertEqual(report['ignored'], [])

    def test_wan_preset_updates_visible_start_workflow_video_parameters(self):
        prompt = {
            '81': {
                'class_type': 'WanFirstLastFrameToVideo',
                'inputs': {
                    'width': 640,
                    'height': 640,
                    'length': 81,
                    'batch_size': 1,
                },
            },
            '86': {
                'class_type': 'CreateVideo',
                'inputs': {'fps': 16},
            },
            '97': {
                'class_type': 'start_workflow',
                'inputs': {
                    'duration': 5,
                    'fps': 16,
                    'resolution': '',
                    'aspect_ratio': '',
                    'size': '',
                    'width': 640,
                    'height': 640,
                },
            },
        }

        report = apply_workflow_parameters(
            prompt,
            {
                'duration': 6,
                'fps': 24,
                'resolution': '720p',
                'aspect_ratio': '16:9',
            },
        )

        self.assertEqual(prompt['97']['inputs']['duration'], 6)
        self.assertEqual(prompt['97']['inputs']['fps'], 24)
        self.assertEqual(prompt['97']['inputs']['resolution'], '720p')
        self.assertEqual(prompt['97']['inputs']['aspect_ratio'], '16:9')
        self.assertEqual(prompt['97']['inputs']['width'], 1280)
        self.assertEqual(prompt['97']['inputs']['height'], 720)
        self.assertEqual(prompt['81']['inputs']['length'], 145)
        self.assertEqual(prompt['86']['inputs']['fps'], 24)
        self.assertEqual(report['ignored'], [])

    def test_wan_preset_preserves_connected_parameter_outputs(self):
        prompt = {
            '81': {
                'class_type': 'WanFirstLastFrameToVideo',
                'inputs': {
                    'width': ['97', 14],
                    'height': ['97', 15],
                    'length': ['97', 16],
                },
            },
            '86': {
                'class_type': 'CreateVideo',
                'inputs': {'fps': ['97', 10]},
            },
            '97': {
                'class_type': 'start_workflow',
                'inputs': {
                    'duration': 5,
                    'fps': 16,
                    'resolution': '',
                    'aspect_ratio': '',
                    'width': 640,
                    'height': 640,
                },
            },
        }

        report = apply_workflow_parameters(
            prompt,
            {'duration': 6, 'fps': 24, 'aspect_ratio': '16:9', 'resolution': '720p'},
        )

        self.assertEqual(prompt['81']['inputs']['width'], ['97', 14])
        self.assertEqual(prompt['81']['inputs']['height'], ['97', 15])
        self.assertEqual(prompt['81']['inputs']['length'], ['97', 16])
        self.assertEqual(prompt['86']['inputs']['fps'], ['97', 10])
        self.assertEqual(prompt['97']['inputs']['duration'], 6)
        self.assertEqual(prompt['97']['inputs']['fps'], 24)
        self.assertEqual(prompt['97']['inputs']['width'], 1280)
        self.assertEqual(prompt['97']['inputs']['height'], 720)
        self.assertEqual(report['derived']['wan_length'], 145)
        self.assertEqual(report['ignored'], [])

    def test_aspect_ratio_derives_dimensions_and_updates_matching_nodes(self):
        prompt = {
            "8": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512, "batch_size": 1},
            },
        }

        report = apply_workflow_parameters(
            prompt,
            {"aspect_ratio": "16:9", "resolution": "720p", "batch_size": 2},
        )

        self.assertEqual(prompt["8"]["inputs"]["width"], 1280)
        self.assertEqual(prompt["8"]["inputs"]["height"], 720)
        self.assertEqual(prompt["8"]["inputs"]["batch_size"], 2)
        self.assertEqual(report["ignored"], [])

    def test_aspect_ratio_shorthand_maps_to_resolution_selector_label(self):
        prompt = {
            "7": {
                "class_type": "ResolutionSelector",
                "inputs": {
                    "aspect_ratio": "1:1 (Square)",
                    "megapixels": 0.9,
                    "multiple": 32,
                },
            },
            "97": {
                "class_type": "start_workflow",
                "inputs": {
                    "aspect_ratio": "",
                    "width": 640,
                    "height": 640,
                },
            },
        }

        report = apply_workflow_parameters(prompt, {"aspect_ratio": "9:16"})

        self.assertEqual(
            prompt["7"]["inputs"]["aspect_ratio"],
            "9:16 (Portrait Widescreen)",
        )
        self.assertEqual(prompt["97"]["inputs"]["aspect_ratio"], "9:16")
        self.assertEqual(report["ignored"], [])

    def test_aspect_ratio_adapts_to_label_raw_numeric_and_alias_inputs(self):
        prompt = {
            "selector": {
                "class_type": "ResolutionSelector",
                "inputs": {"aspect_ratio": "16:9 (Widescreen)"},
            },
            "raw": {
                "class_type": "RawRatioNode",
                "inputs": {"aspect_ratio": "16:9"},
            },
            "numeric": {
                "class_type": "NumericRatioNode",
                "inputs": {"ratio": 1.0},
            },
            "auto": {
                "class_type": "AutoRatioNode",
                "inputs": {"aspect_ratio": "auto"},
            },
        }

        apply_workflow_parameters(prompt, {"aspect_ratio": 0.5625})

        self.assertEqual(
            prompt["selector"]["inputs"]["aspect_ratio"],
            "9:16 (Portrait Widescreen)",
        )
        self.assertEqual(prompt["raw"]["inputs"]["aspect_ratio"], "9:16")
        self.assertEqual(prompt["numeric"]["inputs"]["ratio"], 0.5625)
        self.assertEqual(prompt["auto"]["inputs"]["aspect_ratio"], "9:16")

    def test_aspect_ratio_parser_accepts_labels_slashes_x_and_numbers(self):
        prompt = {
            str(index): {
                "class_type": "ResolutionSelector",
                "inputs": {"aspect_ratio": "1:1 (Square)"},
            }
            for index in range(4)
        }

        values = ["9:16", "9/16", "9x16", "9:16 (Portrait Widescreen)"]
        for node_id, value in zip(prompt, values):
            apply_workflow_parameters({node_id: prompt[node_id]}, {"aspect_ratio": value})

        for node in prompt.values():
            self.assertEqual(
                node["inputs"]["aspect_ratio"],
                "9:16 (Portrait Widescreen)",
            )

    def test_all_standard_aspect_ratio_presets_map_to_comfy_labels(self):
        presets = [
            ("1:1", "1:1 (Square)"),
            ("2:3", "2:3 (Portrait Photo)"),
            ("3:2", "3:2 (Photo)"),
            ("3:4", "3:4 (Portrait Standard)"),
            ("4:3", "4:3 (Standard)"),
            ("9:16", "9:16 (Portrait Widescreen)"),
            ("16:9", "16:9 (Widescreen)"),
            ("21:9", "21:9 (Ultrawide)"),
        ]

        for value, expected in presets:
            with self.subTest(value=value):
                prompt = {
                    "7": {
                        "class_type": "ResolutionSelector",
                        "inputs": {"aspect_ratio": "1:1 (Square)"},
                    },
                }

                apply_workflow_parameters(prompt, {"aspect_ratio": value})

                self.assertEqual(prompt["7"]["inputs"]["aspect_ratio"], expected)

    def test_ratio_alias_uses_the_same_aspect_ratio_normalization(self):
        prompt = {
            "7": {
                "class_type": "ResolutionSelector",
                "inputs": {"aspect_ratio": "1:1 (Square)"},
            },
            "97": {
                "class_type": "start_workflow",
                "inputs": {"aspect_ratio": ""},
            },
        }

        apply_workflow_parameters(prompt, {"ratio": "16:9"})

        self.assertEqual(prompt["7"]["inputs"]["aspect_ratio"], "16:9 (Widescreen)")
        self.assertEqual(prompt["97"]["inputs"]["aspect_ratio"], "16:9")

    def test_node_overrides_take_precedence_over_automatic_matching(self):
        prompt = {
            "8": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512},
            },
            "9": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 256, "height": 256},
            },
        }

        apply_workflow_parameters(
            prompt,
            {
                "width": 1024,
                "height": 576,
                "node_overrides": {"9": {"width": 640}},
            },
        )

        self.assertEqual(prompt["8"]["inputs"]["width"], 1024)
        self.assertEqual(prompt["8"]["inputs"]["height"], 576)
        self.assertEqual(prompt["9"]["inputs"]["width"], 640)
        self.assertEqual(prompt["9"]["inputs"]["height"], 576)

    def test_workflow_metadata_bindings_target_specific_input(self):
        prompt = {
            "8": {
                "class_type": "CustomResolution",
                "inputs": {"target_width": 512, "target_height": 512},
                "_meta": {
                    "llm_party": {
                        "parameter_bindings": {
                            "width": {"node": "8", "input": "target_width"},
                            "height": {"node": "8", "input": "target_height"},
                        }
                    }
                },
            },
        }

        report = apply_workflow_parameters(prompt, {"width": 1024, "height": 576})

        self.assertEqual(prompt["8"]["inputs"]["target_width"], 1024)
        self.assertEqual(prompt["8"]["inputs"]["target_height"], 576)
        self.assertEqual(report["ignored"], [])

    def test_size_and_video_parameters_update_workflow_inputs(self):
        prompt = {
            "8": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512},
            },
            "12": {
                "class_type": "VideoConfig",
                "inputs": {"duration": 4, "fps": 16},
            },
        }

        report = apply_workflow_parameters(
            prompt,
            {"size": "1536x1024", "duration": 8, "fps": 24},
        )

        self.assertEqual(prompt["8"]["inputs"]["width"], 1536)
        self.assertEqual(prompt["8"]["inputs"]["height"], 1024)
        self.assertEqual(prompt["12"]["inputs"]["duration"], 8)
        self.assertEqual(prompt["12"]["inputs"]["fps"], 24)
        self.assertEqual(report["ignored"], [])

    def test_video_size_alias_updates_workflow_dimensions(self):
        prompt = {
            "8": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512},
            },
        }

        report = apply_workflow_parameters(prompt, {"videoSize": "1536x1024"})

        self.assertEqual(prompt["8"]["inputs"]["width"], 1536)
        self.assertEqual(prompt["8"]["inputs"]["height"], 1024)
        self.assertEqual(report["ignored"], [])

    def test_video_resolution_maps_to_comfy_resolution_selector_megapixels(self):
        prompt = {
            "7": {
                "class_type": "ResolutionSelector",
                "inputs": {
                    "aspect_ratio": "16:9 (Widescreen)",
                    "megapixels": 0.5,
                    "preview": [],
                    "multiple": 32,
                },
            },
            "12": {
                "class_type": "VideoConfig",
                "inputs": {"duration": 4},
            },
        }

        report = apply_workflow_parameters(
            prompt,
            {"resolution": 0.9, "duration": 8},
        )

        self.assertEqual(prompt["7"]["inputs"]["megapixels"], 0.9)
        self.assertEqual(prompt["12"]["inputs"]["duration"], 8)
        self.assertEqual(report["applied"], ["12.duration", "7.megapixels"])
        self.assertEqual(report["ignored"], [])


if __name__ == "__main__":
    unittest.main()
