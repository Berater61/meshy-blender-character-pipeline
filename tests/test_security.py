from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from final_mesh_pipeline import MeshySession
from meshy_client import MeshyClient


class SessionSecurityTests(unittest.TestCase):
    def test_final_pipeline_does_not_authenticate_download_session(self) -> None:
        client = MeshySession("dummy-test-key")

        self.assertEqual(
            client.session.headers.get("Authorization"),
            "Bearer dummy-test-key",
        )
        self.assertNotIn("Authorization", client.download_session.headers)

    def test_legacy_client_redacts_urls_in_nested_payloads(self) -> None:
        payload = {
            "model": "https://storage.example/model.glb?signature=private",
            "nested": ["unchanged", "https://storage.example/texture.png"],
        }

        redacted = MeshyClient.redact_urls(payload)

        self.assertEqual(redacted["model"], "[SIGNED_URL_REDACTED]")
        self.assertEqual(redacted["nested"][0], "unchanged")
        self.assertEqual(redacted["nested"][1], "[SIGNED_URL_REDACTED]")

    def test_legacy_client_keeps_authentication_inside_api_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = MeshyClient("dummy-test-key", Path(temp_dir))

        self.assertEqual(
            client.session.headers.get("Authorization"),
            "Bearer dummy-test-key",
        )


if __name__ == "__main__":
    unittest.main()
