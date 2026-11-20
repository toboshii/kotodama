"""server/config.example.json shows real settings at their defaults."""

from __future__ import annotations

import json
import unittest

from kotodama.config import DEFAULTS, SERVER_DIR


class ExampleConfigTests(unittest.TestCase):
    def test_every_setting_is_real_and_has_its_default(self):
        # The example says its values are the defaults, so a key it names
        # that the sidecar doesn't read (or reads elsewhere) is a mistake.
        example = json.loads((SERVER_DIR / "config.example.json").read_text(encoding="utf-8"))
        del example["models"]       # sample adapters, not defaults

        def check(ex, default, where):
            for key, value in ex.items():
                if key.startswith("_"):     # comments
                    continue
                path = where + key
                self.assertIn(key, default, f"{path} is not a setting")
                if isinstance(value, dict) and isinstance(default[key], dict):
                    check(value, default[key], path + ".")
                else:
                    self.assertEqual(value, default[key], f"{path} is not the default")

        check(example, DEFAULTS, "")


if __name__ == "__main__":
    unittest.main()
