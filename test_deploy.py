"""systemd unit contract: ordinary user, graceful SIGINT stop, this checkout."""

import configparser
import unittest

import config

UNIT = config.APP_DIRECTORY / "deploy" / "levelsvc.service"


class ServiceUnitTests(unittest.TestCase):
    def setUp(self):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read(UNIT, encoding="utf-8")
        self.service = parser["Service"]

    def test_runs_as_ordinary_user_from_this_checkout(self):
        self.assertNotIn(self.service["User"], ("root", "0"))
        self.assertEqual(self.service["WorkingDirectory"], str(config.APP_DIRECTORY))
        executable, script = self.service["ExecStart"].split()[0], self.service["ExecStart"].split()[-1]
        self.assertEqual(executable, str(config.APP_DIRECTORY / ".venv/bin/python"))
        self.assertTrue((config.APP_DIRECTORY / script).is_file())

    def test_stop_uses_the_ctrl_c_path_with_time_to_flush(self):
        self.assertEqual(self.service["KillSignal"], "SIGINT")
        self.assertGreaterEqual(int(self.service["TimeoutStopSec"]), 20)


if __name__ == "__main__":
    unittest.main()
