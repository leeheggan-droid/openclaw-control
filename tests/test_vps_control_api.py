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

    def _route_paths_for_method(self, method: str) -> set[str]:
        paths = set()
        for route in self.api.app.routes:
            methods = getattr(route, "methods", set())
            if method in methods:
                paths.add(route.path)
        return paths

    def test_health_route_aliases_are_registered(self):
        paths = self._route_paths_for_method("GET")
        self.assertTrue(
            {"/health", "/api/health", "/v1/health", "/api/v1/health"}.issubset(paths)
        )

    def test_contract_route_aliases_are_registered(self):
        paths = self._route_paths_for_method("GET")
        self.assertTrue(
            {"/contract", "/api/contract", "/v1/contract", "/api/v1/contract"}.issubset(paths)
        )

    def test_jobs_route_aliases_are_registered(self):
        paths = self._route_paths_for_method("POST")
        self.assertTrue(
            {"/jobs", "/api/jobs", "/v1/jobs", "/api/v1/jobs"}.issubset(paths)
        )

    def test_contract_endpoint_exposes_control_room_metadata(self):
        payload = self.api.contract(api_key="Bearer test-key")
        self.assertEqual(payload["contract_version"], "2026-05-18.1")
        self.assertEqual(payload["manager"]["id"], "link-manager")
        self.assertIn("operators", payload)
        self.assertIn("services", payload)
        self.assertIn("actions", payload)

    def test_health_endpoint_exposes_metadata(self):
        payload = self.api.health()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "vps-control-api")
        self.assertIn("api_version", payload)
        self.assertIn("contract_version", payload)
        self.assertIn("contract", payload["links"])
        self.assertIn("jobs", payload["links"])

    def test_capability_endpoints_are_available(self):
        services = self.api.services(api_key="Bearer test-key")
        actions = self.api.actions(api_key="Bearer test-key")
        operators = self.api.operators(api_key="Bearer test-key")
        self.assertGreaterEqual(len(services["services"]), 1)
        self.assertGreaterEqual(len(actions["actions"]), 1)
        self.assertGreaterEqual(len(operators["operators"]), 1)

    def test_parse_log_lines_accepts_boundary_values(self):
        self.assertEqual(self.api._parse_log_lines(1), 1)
        self.assertEqual(self.api._parse_log_lines(1000), 1000)

    def test_parse_log_lines_rejects_out_of_range_values(self):
        with self.assertRaises(HTTPException):
            self.api._parse_log_lines(0)
        with self.assertRaises(HTTPException):
            self.api._parse_log_lines(1001)

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

    def test_money_risk_service_forces_confirmation_for_non_read_action(self):
        with patch.dict(
            self.api.ACTION_METADATA,
            {
                "custom-control": {
                    "requires_confirmation": False,
                    "category": "control",
                }
            },
            clear=False,
        ):
            self.assertTrue(
                self.api._action_requires_confirmation(
                    "custom-control",
                    "openclaw-crypto.service",
                )
            )
            self.assertFalse(
                self.api._action_requires_confirmation(
                    "custom-control",
                    "openclaw-agent.service",
                )
            )

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

    def test_job_executes_status_all_action_without_service(self):
        with patch.object(self.api, "_run_status_command") as status_mock:
            status_mock.return_value = {"stdout": "active", "stderr": "", "returncode": 0}
            created = self.api.create_job(
                self.api.JobRequest(action="status-all"),
                api_key="Bearer test-key",
            )
        self.assertEqual(created["status"], "succeeded")
        self.assertEqual(created["result"]["action"], "status-all")
        self.assertGreater(created["result"]["data"]["service_count"], 0)
        self.assertEqual(
            created["result"]["data"]["service_count"],
            created["result"]["data"]["active_count"],
        )

    def test_status_collection_endpoint_returns_all_services(self):
        with patch.object(self.api, "_run_status_command") as status_mock:
            status_mock.return_value = {"stdout": "active", "stderr": "", "returncode": 0}
            payload = self.api.status_all(api_key="Bearer test-key")
        self.assertIn("services", payload)
        self.assertGreaterEqual(payload["service_count"], 1)
        self.assertEqual(payload["service_count"], len(payload["services"]))

    def test_get_job_returns_not_found_for_unknown_id(self):
        with self.assertRaises(HTTPException) as context:
            self.api.get_job("missing-job", api_key="Bearer test-key")
        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
