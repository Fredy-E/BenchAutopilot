"""Bench Autopilot tests (stdlib unittest only).

Proves, RED-first:
  1. failing stage stops the run (later stages skipped)
  2. timeout kills ONLY the owned child (two-sleeper proof)
  3. receipt contains exact sha256 hashes + required fields

Run:  python -m unittest test_run -v
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

RUN_PY = Path(__file__).resolve().parent / "run.py"


def run_autopilot(config: dict, cwd: Path, extra_args=None):
    cfg_path = cwd / "cfg.json"
    cfg_path.write_text(json.dumps(config), encoding="utf-8")
    receipt_path = cwd / config.get("receipt", "receipt.json")
    cmd = [sys.executable, str(RUN_PY), "--config", str(cfg_path)]
    if extra_args:
        cmd += extra_args
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120)
    receipt = None
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    return proc, receipt


class TestAutopilot(unittest.TestCase):
    def test_failing_stage_stops_run(self):
        """A failing stage must stop the run; later stages are skipped."""
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            config = {
                "receipt": "receipt.json",
                "defaults": {"timeout": 20, "tail_lines": 10},
                "stages": [
                    {
                        "name": "static-checks",
                        "type": "exec",
                        "cmd": [sys.executable, "-c", "print('boom'); raise SystemExit(3)"],
                        "timeout": 20,
                    },
                    {
                        "name": "smoke-test",
                        "type": "exec",
                        "cmd": [sys.executable, "-c", "open('MARKER.txt','w').write('should-not-exist')"],
                        "timeout": 20,
                    },
                ],
            }
            proc, receipt = run_autopilot(config, cwd)
            self.assertNotEqual(proc.returncode, 0, "run must fail when a stage fails")
            self.assertFalse((cwd / "MARKER.txt").exists(), "later stage must NOT execute after failure")
            self.assertIsNotNone(receipt, "receipt must be written even on failure")
            by_name = {s["name"]: s for s in receipt["stages"]}
            self.assertEqual(by_name["static-checks"]["exit"], 3)
            self.assertEqual(by_name["smoke-test"]["status"], "skipped")
            self.assertFalse(receipt["ok"])

    def test_timeout_kills_only_owned_child(self):
        """Two-sleeper proof: timeout kills the owned stage child, not an unrelated sleeper."""
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            # Decoy sleeper: NOT owned by autopilot; must survive the stage timeout.
            decoy = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                self.assertIsNone(decoy.poll(), "decoy must start alive")
                config = {
                    "receipt": "receipt.json",
                    "defaults": {"timeout": 2, "tail_lines": 5},
                    "stages": [
                        {
                            "name": "slow-stage",
                            "type": "exec",
                            "cmd": [sys.executable, "-c", "import time; print('sleeping'); time.sleep(30)"],
                            "timeout": 2,
                        },
                        {
                            "name": "never-reached",
                            "type": "exec",
                            "cmd": [sys.executable, "-c", "print('unreached')"],
                            "timeout": 5,
                        },
                    ],
                }
                t0 = time.time()
                proc, receipt = run_autopilot(config, cwd)
                elapsed = time.time() - t0
                self.assertLess(elapsed, 25, "timed-out run must return promptly, not wait 30s")
                self.assertNotEqual(proc.returncode, 0, "timed-out run must fail")
                self.assertIsNotNone(receipt)
                by_name = {s["name"]: s for s in receipt["stages"]}
                self.assertTrue(by_name["slow-stage"]["timed_out"], "slow stage must be marked timed_out")
                self.assertEqual(by_name["never-reached"]["status"], "skipped")
                # The owned child is gone (run returned), but the decoy must still be alive:
                # this proves the timeout killed only the owned Popen child
                # (a broad taskkill-by-image would have killed the decoy too).
                self.assertIsNone(decoy.poll(), "decoy sleeper must SURVIVE the stage timeout")
            finally:
                decoy.kill()
                try:
                    decoy.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    decoy.terminate()
                self.assertIsNotNone(decoy.poll(), "decoy must be reaped after explicit kill")

    def test_receipt_contains_exact_hashes(self):
        """Manifest stage must record exact sha256 of artifacts + required receipt fields."""
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            payload = b"bench-autopilot-fixture-123\n"
            artifact = cwd / "artifact.bin"
            artifact.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            config = {
                "receipt": "receipt.json",
                "defaults": {"timeout": 20, "tail_lines": 10},
                "stages": [
                    {
                        "name": "static-checks",
                        "type": "exec",
                        "cmd": [sys.executable, "-c", "print('static ok')"],
                        "timeout": 20,
                    },
                    {
                        "name": "artifact-manifest",
                        "type": "manifest",
                        "artifacts": ["artifact.bin"],
                    },
                ],
            }
            proc, receipt = run_autopilot(config, cwd)
            self.assertEqual(proc.returncode, 0, f"run must succeed; stderr={proc.stdout[-2000:]}")
            self.assertIsNotNone(receipt)
            self.assertTrue(receipt["ok"])
            by_name = {s["name"]: s for s in receipt["stages"]}
            manifest = by_name["artifact-manifest"]
            self.assertEqual(manifest["sha256s"]["artifact.bin"], expected)
            # Required receipt fields per stage:
            for stage in receipt["stages"]:
                for field in ("name", "exit", "started_at", "ended_at"):
                    self.assertIn(field, stage, f"stage missing field {field}: {stage}")
            # Timestamps must parse and be ordered:
            self.assertLessEqual(manifest["started_at"], manifest["ended_at"])


if __name__ == "__main__":
    unittest.main()
