import unittest

from workflow_parameters import apply_workflow_parameters


class WorkflowParametersTest(unittest.TestCase):
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
