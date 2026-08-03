import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "qa-backend"))

from guardrails import BudgetFuse, GuardrailSettings, RateLimiter, admin_bypass


class GuardrailTests(unittest.TestCase):
    def test_rate_limiter_blocks_fourth_request(self):
        settings = GuardrailSettings(per_minute=3, per_client_day=30, global_day=300)
        limiter = RateLimiter(settings)
        with patch("guardrails.time.time", return_value=1000.0):
            self.assertTrue(limiter.check("client")[0])
            self.assertTrue(limiter.check("client")[0])
            self.assertTrue(limiter.check("client")[0])
            self.assertFalse(limiter.check("client")[0])

    def test_admin_key_uses_exact_match(self):
        self.assertTrue(admin_bypass("secret", "secret"))
        self.assertFalse(admin_bypass("secret", "Secret"))
        self.assertFalse(admin_bypass("", "secret"))

    def test_budget_fuse_persists_reservations(self):
        settings = GuardrailSettings(daily_budget_usd=0.03, estimated_request_cost_usd=0.02)
        with tempfile.TemporaryDirectory() as temp:
            fuse = BudgetFuse(settings, Path(temp) / "usage.json")
            self.assertTrue(fuse.reserve()[0])
            self.assertFalse(fuse.reserve()[0])
            self.assertEqual(fuse.status()["requests"], 1)


if __name__ == "__main__":
    unittest.main()
