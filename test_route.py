import contextlib
import io
import unittest
from unittest import mock

import route


class PlatformSubagentDiscoveryTests(unittest.TestCase):
    def test_subagent_fallback_never_names_a_remembered_model(self):
        with mock.patch.object(route, "ollama_choice", return_value=(None, "unavailable")), \
             mock.patch.object(route, "codex_choice", return_value=(False, "unavailable")):
            plan = "\n".join(route.decide("repo-read", 50_000, True)).lower()

        self.assertIn("discover", plan)
        self.assertIn("omit the model override", plan)
        self.assertNotIn("haiku", plan)
        self.assertNotIn("sonnet", plan)

    def test_status_says_platform_workers_must_be_discovered(self):
        fake_state = {"models": {}, "gpu": None, "loaded": {}, "warn": None}
        out = io.StringIO()
        with mock.patch.object(route.run, "discover", return_value=fake_state), \
             mock.patch.object(route.run, "server_info", return_value="test"), \
             mock.patch.object(route.codex_run, "status", return_value=(False, "test")), \
             contextlib.redirect_stdout(out):
            route.show_status()

        status = out.getvalue().lower()
        self.assertIn("platform", status)
        self.assertIn("discover", status)
        self.assertNotIn("haiku", status)
        self.assertNotIn("sonnet", status)

    def test_unverified_platform_subagent_is_not_presented_as_cheapest_choice(self):
        with mock.patch.object(route, "ollama_choice", return_value=(None, "unavailable")), \
             mock.patch.object(route, "codex_choice", return_value=(False, "unavailable")):
            plan = "\n".join(route.decide("edit", 10_000, True)).lower()

        self.assertIn("use main model, inline", plan)
        self.assertIn("confirmed cheaper", plan)
        self.assertIn("model-less", plan)
        self.assertNotIn("use platform", plan)


if __name__ == "__main__":
    unittest.main()
