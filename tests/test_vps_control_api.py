import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "vps-control-api" / "api.py"


def load_api_module():
    os.environ.setdefault("VPS_CONTROL_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("openclaw_control_api", API_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VpsControlApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = load_api_module()

    def test_contract_endpoint_exposes_control_room_metadata(self):
        payload = self.api.contract(api_key="Bearer test-key")
        self.assertEqual(payload["contract_version"], "2026-05-16.1")
        self.assertEqual(payload["manager"]["id"], "link-manager")
        self.assertIn("operators", payload)
        self.assertIn("services", payload)
        self.assertIn("actions", payload)

    def test_capability_endpoints_are_available(self):
        services = self.api.services(api_key="Bearer test-key")
        actions = self.api.actions(api_key="Bearer test-key")
        operators = self.api.operators(api_key="Bearer test-key")
        self.assertGreaterEqual(len(services["services"]), 1)
        self.assertGreaterEqual(len(actions["actions"]), 1)
        self.assertGreaterEqual(len(operators["operators"]), 1)

    def test_diagnostics_endpoint_returns_standardized_bundle(self):
        with (
            patch.object(self.api, "_run_status_command") as status_mock,
            patch.object(self.api, "_run_logs_command") as logs_mock,
        ):
            status_mock.return_value = {"stdout": "active", "stderr": "", "returncode": 0}
            logs_mock.return_value = {
                "stdout": "line one\nline two",
                "stderr": "",
                "returncode": 0,
            }
            payload = self.api.diagnostics(
                "openclaw-agent.service",
                n=2,
                api_key="Bearer test-key",
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "diagnostics")
        self.assertEqual(payload["data"]["status_summary"]["state"], "active")
        self.assertEqual(payload["artifacts"]["log_lines"], ["line one", "line two"])

    def test_job_requires_confirmation_for_restart(self):
        payload = self.api.create_job(
            self.api.JobRequest(action="restart", service="openclaw-agent.service"),
            api_key="Bearer test-key",
        )
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "confirmation_required")
        self.assertIn("requires explicit confirmation", payload["result"]["reason"])

    def test_job_requires_confirmation_note_for_money_risk_service(self):
        payload = self.api.create_job(
            self.api.JobRequest(
                action="deploy",
                service="openclaw-crypto.service",
                confirmed=True,
            ),
            api_key="Bearer test-key",
        )
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "confirmation_required")
        self.assertIn("confirmation_note", payload["result"]["reason"])

    def test_job_validates_log_parameter(self):
        payload = self.api.create_job(
            self.api.JobRequest(
                action="logs",
                service="openclaw-agent.service",
                parameters={"n": "not-a-number"},
            ),
            api_key="Bearer test-key",
        )
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "execution_failed")
        self.assertIn("Parameter 'n' must be an integer", payload["result"]["reason"])

    def test_job_executes_status_action_and_records_job(self):
        with patch.object(self.api, "_run_status_command") as status_mock:
            status_mock.return_value = {"stdout": "active", "stderr": "", "returncode": 0}
            created = self.api.create_job(
                self.api.JobRequest(action="status", service="openclaw-agent.service"),
                api_key="Bearer test-key",
            )
        self.assertEqual(created["status"], "succeeded")
        self.assertEqual(created["result"]["data"]["state"], "active")

        job_id = created["id"]
        fetched = self.api.get_job(job_id, api_key="Bearer test-key")
        self.assertEqual(fetched["id"], job_id)
        self.assertEqual(fetched["status"], "succeeded")

    def test_get_job_returns_not_found_for_unknown_id(self):
        with self.assertRaises(HTTPException) as context:
            self.api.get_job("missing-job", api_key="Bearer test-key")
        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
