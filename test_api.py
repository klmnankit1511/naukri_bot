import importlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {"API_KEY": "test-secret", "NAUKRI_DATA_ROOT": self.temporary.name},
        )
        self.environment.start()
        import api

        self.api = importlib.reload(api)
        self.client_context = TestClient(self.api.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def test_health_is_public(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_trigger_requires_api_key(self):
        self.assertEqual(self.client.post("/2").status_code, 401)

    def test_route_maps_to_remaining_profile(self):
        with patch.object(self.api, "queue") as mocked_queue:
            responses = [
                self.client.post(f"/{person}", headers={"x-api-key": "test-secret"})
                for person in (2,)
            ]
        self.assertEqual([response.status_code for response in responses], [202])
        self.assertEqual([response.json()["person"] for response in responses], [2])
        queued_run_ids = [call.args[0] for call in mocked_queue.put_nowait.call_args_list]
        self.assertEqual(len(queued_run_ids), 1)

    def test_unknown_route_is_unavailable(self):
        response = self.client.post("/999", headers={"x-api-key": "test-secret"})
        self.assertEqual(response.status_code, 404)

    def test_duplicate_profile_run_is_not_queued_twice(self):
        with patch.object(self.api, "queue") as mocked_queue:
            first = self.client.post("/2", headers={"x-api-key": "test-secret"})
            second = self.client.post("/2", headers={"x-api-key": "test-secret"})
        self.assertEqual(first.json()["run_id"], second.json()["run_id"])
        self.assertTrue(second.json()["deduplicated"])
        mocked_queue.put_nowait.assert_called_once()

    def test_profile_uses_its_own_credentials(self):
        with patch.dict(
            os.environ,
            {"NAUKRI_EMAIL": "generic@example.test", "PERSON_2_EMAIL": "person2@example.test"},
        ):
            environment = self.api.profile_environment(2)
        self.assertEqual(environment["NAUKRI_EMAIL"], "person2@example.test")
        self.assertEqual(environment["NAUKRI_NON_INTERACTIVE"], "1")
        self.assertEqual(environment["NAUKRI_HEADLESS"], "1")


if __name__ == "__main__":
    unittest.main()
