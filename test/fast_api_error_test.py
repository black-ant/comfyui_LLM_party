import importlib
import sys
import urllib.error
import unittest


class FastAPIErrorDetailsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original_argv = sys.argv[:]
        sys.argv = ["fast_api.py"]
        try:
            cls.fast_api = importlib.import_module("fast_api")
        finally:
            sys.argv = original_argv

    def test_history_error_keeps_comfyui_node_diagnostics(self):
        execution_error = {
            "prompt_id": "prompt-1",
            "node_id": "42",
            "node_type": "FailingNode",
            "exception_type": "RuntimeError",
            "exception_message": "CUDA out of memory",
            "traceback": ["Traceback...", "RuntimeError: CUDA out of memory"],
            "current_inputs": {"steps": 20},
            "current_outputs": ["41"],
        }
        history = {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [["execution_error", execution_error]],
            }
        }

        details = self.fast_api._history_error_details(history)

        self.assertEqual(details["status"], "error")
        self.assertEqual(details["details"][0]["detail"]["node_id"], "42")
        self.assertEqual(
            details["details"][0]["detail"]["exception_message"],
            "CUDA out of memory",
        )

    def test_completion_error_payload_exposes_workflow_details(self):
        details = {
            "status": "error",
            "completed": False,
            "prompt_id": "prompt-2",
            "details": [
                {
                    "type": "execution_error",
                    "detail": {
                        "node_id": "7",
                        "exception_type": "ValueError",
                        "exception_message": "bad input",
                        "traceback": ["ValueError: bad input"],
                    },
                }
            ],
        }
        error = self.fast_api.ComfyUIWorkflowError("ComfyUI workflow failed", details)

        payload = self.fast_api._completion_error_payload(error, "request-1", "workflow")

        self.assertEqual(payload["error"]["type"], "comfyui_workflow_error")
        self.assertEqual(payload["error"]["request_id"], "request-1")
        self.assertEqual(payload["error"]["details"]["prompt_id"], "prompt-2")
        self.assertEqual(
            payload["error"]["details"]["details"][0]["detail"]["node_id"],
            "7",
        )

    def test_connection_refused_is_classified_as_comfyui_unreachable(self):
        error = urllib.error.URLError(ConnectionRefusedError("connection refused"))

        self.assertTrue(self.fast_api._is_comfyui_unreachable_error(error))

    def test_expired_remote_image_url_has_actionable_error(self):
        class Response:
            status_code = 403
            text = "<Error><Code>ExpiredToken</Code><Message>Request has expired</Message></Error>"

        detail = self.fast_api._image_fetch_failure_detail(Response())

        self.assertIn("expired", detail)
        self.assertIn("HTTP 403", detail)

    def test_request_parameter_log_traces_nested_duration_precedence(self):
        request_data = self.fast_api.CompletionRequest(
            model="Mimax-H3-202609",
            messages=[self.fast_api.Message(role="user", content="animate")],
            video_config={"duration": 5},
            params={"duration": 4},
            workflow_params={"duration": 3, "fps": 24},
        )

        payload = self.fast_api._completion_request_parameters_for_log(request_data)

        self.assertEqual(payload["duration_trace"]["video_config"], 5)
        self.assertEqual(payload["duration_trace"]["params"], 4)
        self.assertEqual(payload["duration_trace"]["workflow_params"], 3)
        self.assertEqual(payload["duration_trace"]["effective"], 3)
        self.assertEqual(payload["normalized_workflow_params"]["duration"], 3)
        self.assertEqual(payload["normalized_workflow_params"]["fps"], 24)


if __name__ == "__main__":
    unittest.main()
