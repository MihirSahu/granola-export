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


class FakeCompletedProcess:
    def __init__(self, stdout):
        self.stdout = stdout


class CredentialRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.granola_dir = self.home / "Library/Application Support/Granola"
        self.granola_dir.mkdir(parents=True)
        self.expired_token = fake_jwt(int(time.time()) - 60)
        self.refreshed_token = fake_jwt(int(time.time()) + 3600)

    def tearDown(self):
        granola_export._encrypted_storage_cache = None
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

    def test_stored_account_token_prefers_encrypted_storage(self):
        stale_token = fake_jwt(int(time.time()) + 60)
        encrypted_token = fake_jwt(int(time.time()) + 3600)
        accounts_path = self.granola_dir / "stored-accounts.json"
        accounts_path.write_text(json.dumps({
            "accounts": [{
                "userId": "user_1",
                "savedAt": 1,
                "tokens": json.dumps({"access_token": stale_token}),
            }]
        }))

        encrypted_data = {
            "accounts": json.dumps([{
                "userId": "user_1",
                "savedAt": 2,
                "tokens": json.dumps({"access_token": encrypted_token}),
            }])
        }

        with patch.object(granola_export.Path, "home", return_value=self.home), \
             patch.object(granola_export, "load_encrypted_storage_file", return_value=encrypted_data, create=True):
            token = granola_export.load_stored_account_token()

        self.assertEqual(token, encrypted_token)

    def test_legacy_supabase_token_prefers_encrypted_storage(self):
        stale_token = fake_jwt(int(time.time()) + 60)
        encrypted_token = fake_jwt(int(time.time()) + 3600)
        creds_path = self.granola_dir / "supabase.json"
        creds_path.write_text(json.dumps({
            "workos_tokens": json.dumps({"access_token": stale_token})
        }))

        encrypted_data = {
            "workos_tokens": json.dumps({"access_token": encrypted_token})
        }

        with patch.object(granola_export.Path, "home", return_value=self.home), \
             patch.object(granola_export, "load_encrypted_storage_file", return_value=encrypted_data, create=True):
            token = granola_export.load_legacy_supabase_token()

        self.assertEqual(token, encrypted_token)

    def test_encrypted_storage_cache_uses_one_decrypt_process(self):
        (self.granola_dir / "storage.dek").write_bytes(b"dek")
        (self.granola_dir / "stored-accounts.json.enc").write_bytes(b"accounts")
        (self.granola_dir / "supabase.json.enc").write_bytes(b"supabase")
        payload = {
            "stored-accounts.json": {"accounts": "[]"},
            "supabase.json": {"workos_tokens": "{}"},
        }

        with patch.object(granola_export.Path, "home", return_value=self.home), \
             patch.object(granola_export.subprocess, "run", return_value=FakeCompletedProcess(json.dumps(payload))) as run:
            self.assertEqual(granola_export.load_encrypted_storage_file("stored-accounts.json"), payload["stored-accounts.json"])
            self.assertEqual(granola_export.load_encrypted_storage_file("supabase.json"), payload["supabase.json"])

        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
