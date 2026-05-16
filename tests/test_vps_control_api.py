import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


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
        cls.client = TestClient(cls.api.app)
        cls.headers = {"Authorization": "Bearer test-key"}

    def test_contract_endpoint_exposes_control_room_metadata(self):
        response = self.client.get("/contract", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["contract_version"], "2026-05-16.1")
        self.assertEqual(payload["manager"]["id"], "link-manager")
        self.assertIn("operators", payload)
        self.assertIn("services", payload)
        self.assertIn("actions", payload)

    def test_diagnostics_endpoint_returns_standardized_bundle(self):
        with patch.object(self.api, "_run") as run_mock:
            run_mock.side_effect = [
                {"stdout": "active", "stderr": "", "returncode": 0},
                {"stdout": "line one\nline two", "stderr": "", "returncode": 0},
            ]
            response = self.client.get(
                "/diagnostics/openclaw-agent.service?n=2",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "diagnostics")
        self.assertEqual(payload["data"]["status_summary"]["state"], "active")
        self.assertEqual(payload["artifacts"]["log_lines"], ["line one", "line two"])

    def test_job_requires_confirmation_for_restart(self):
        response = self.client.post(
            "/jobs",
            headers=self.headers,
            json={"action": "restart", "service": "openclaw-agent.service"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "confirmation_required")
        self.assertIn("requires explicit confirmation", payload["result"]["reason"])

    def test_job_executes_status_action_and_records_job(self):
        with patch.object(self.api, "_run") as run_mock:
            run_mock.return_value = {"stdout": "active", "stderr": "", "returncode": 0}
            create_response = self.client.post(
                "/jobs",
                headers=self.headers,
                json={"action": "status", "service": "openclaw-agent.service"},
            )

        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()
        self.assertEqual(created["status"], "succeeded")
        self.assertEqual(created["result"]["data"]["state"], "active")

        job_id = created["id"]
        get_response = self.client.get(f"/jobs/{job_id}", headers=self.headers)
        self.assertEqual(get_response.status_code, 200)
        fetched = get_response.json()
        self.assertEqual(fetched["id"], job_id)
        self.assertEqual(fetched["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
