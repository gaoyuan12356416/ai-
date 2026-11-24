import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from codex_account_watch import auth_identity, read_json
from sync_youtube_cover_auth import cover_snapshot, install_snapshot


def login(account, expiry):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": account, "exp": expiry}).encode()).decode()
    token = "test." + payload + ".signature"
    return {"auth_mode": "chatgpt", "last_refresh": "timestamp", "OPENAI_API_KEY": "private",
            "tokens": {"account_id": account, "access_token": token, "id_token": token, "refresh_token": "private"}}


class YouTubeCoverAuthTests(unittest.TestCase):
    def test_only_current_access_is_exported(self):
        primary = login("primary", int(time.time()) + 3600)
        fallback = login("fallback", int(time.time()) + 3600)
        for auth in (primary, fallback):
            snapshot = cover_snapshot(auth, auth_identity(auth))
            self.assertEqual(snapshot["auth_mode"], "chatgptAuthTokens")
            self.assertEqual(auth_identity(snapshot), auth_identity(auth))
            self.assertNotIn("private", json.dumps(snapshot))
        with self.assertRaises(ValueError):
            cover_snapshot(primary, auth_identity(fallback))
        expired = login("primary", 1)
        with self.assertRaises(ValueError):
            cover_snapshot(expired, auth_identity(expired))

    @unittest.skipUnless(os.name == "posix", "worker file ownership is verified on Linux")
    def test_account_switch_replaces_private_file_and_identical_tick_keeps_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            for label in ("primary", "fallback"):
                auth = login(label, int(time.time()) + 3600)
                snapshot = cover_snapshot(auth, auth_identity(auth))
                self.assertTrue(install_snapshot(path, snapshot))
                self.assertEqual(read_json(path), snapshot)
                inode = path.stat().st_ino
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertFalse(install_snapshot(path, snapshot))
                self.assertEqual(path.stat().st_ino, inode)


if __name__ == "__main__":
    unittest.main()
