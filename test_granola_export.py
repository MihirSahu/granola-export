import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import granola_export


def fake_jwt(exp, client_id="client_test"):
    header = {"alg": "none"}
    payload = {"exp": exp, "client_id": client_id}

    def encode_part(value):
        encoded = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        return encoded.rstrip("=")

    return f"{encode_part(header)}.{encode_part(payload)}.signature"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class CredentialRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.granola_dir = self.home / "Library/Application Support/Granola"
        self.granola_dir.mkdir(parents=True)
        self.expired_token = fake_jwt(int(time.time()) - 60)
        self.refreshed_token = fake_jwt(int(time.time()) + 3600)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_legacy_supabase_token_refreshes_expired_access_token(self):
        creds_path = self.granola_dir / "supabase.json"
        creds_path.write_text(json.dumps({
            "workos_tokens": json.dumps({
                "access_token": self.expired_token,
                "refresh_token": "refresh_old",
                "token_type": "Bearer",
            })
        }))

        with patch.object(granola_export.Path, "home", return_value=self.home), \
             patch.object(granola_export.requests, "post", return_value=FakeResponse({
                 "access_token": self.refreshed_token,
                 "refresh_token": "refresh_new",
             })) as post:
            token = granola_export.load_legacy_supabase_token()

        self.assertEqual(token, self.refreshed_token)
        post.assert_called_once_with(
            "https://api.workos.com/user_management/authenticate",
            headers={"Content-Type": "application/json"},
            json={
                "client_id": "client_test",
                "grant_type": "refresh_token",
                "refresh_token": "refresh_old",
            },
        )
        saved = json.loads(creds_path.read_text())
        saved_tokens = json.loads(saved["workos_tokens"])
        self.assertEqual(saved_tokens["access_token"], self.refreshed_token)
        self.assertEqual(saved_tokens["refresh_token"], "refresh_new")

    def test_stored_account_token_refreshes_expired_access_token(self):
        accounts_path = self.granola_dir / "stored-accounts.json"
        accounts_path.write_text(json.dumps({
            "accounts": [{
                "userId": "user_1",
                "savedAt": 1,
                "tokens": json.dumps({
                    "access_token": self.expired_token,
                    "refresh_token": "refresh_old",
                }),
            }]
        }))

        with patch.object(granola_export.Path, "home", return_value=self.home), \
             patch.object(granola_export.requests, "post", return_value=FakeResponse({
                 "access_token": self.refreshed_token,
                 "refresh_token": "refresh_new",
             })):
            token = granola_export.load_stored_account_token()

        self.assertEqual(token, self.refreshed_token)
        saved = json.loads(accounts_path.read_text())
        saved_tokens = json.loads(saved["accounts"][0]["tokens"])
        self.assertEqual(saved_tokens["access_token"], self.refreshed_token)
        self.assertEqual(saved_tokens["refresh_token"], "refresh_new")


if __name__ == "__main__":
    unittest.main()
