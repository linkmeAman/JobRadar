"""Tests for cross-process compatible run locking."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import run_lock


class RunLockTests(unittest.TestCase):
    def test_second_owner_is_rejected_and_lock_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job-radar.lock"
            with run_lock.single_instance(path):
                with self.assertRaises(run_lock.AlreadyRunningError):
                    with run_lock.single_instance(path):
                        pass
            with run_lock.single_instance(path):
                pass
