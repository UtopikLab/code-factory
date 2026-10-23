import json
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
READINESS_FILTER = SCRIPT_DIR / "workload-readiness.jq"


@unittest.skipUnless(shutil.which("jq"), "jq is required to run the readiness filter")
class WorkloadReadinessTests(unittest.TestCase):
    def check_readiness(self, workloads):
        result = subprocess.run(
            ["jq", "-e", "-f", str(READINESS_FILTER)],
            input=json.dumps({"items": workloads}),
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def test_daemonset_with_no_matching_nodes_is_ready(self):
        self.assertTrue(self.check_readiness([{
            "kind": "DaemonSet",
            "metadata": {"name": "mps-control"},
            "status": {"desiredNumberScheduled": 0, "numberReady": 0},
        }]))

    def test_daemonset_with_unavailable_desired_pod_is_not_ready(self):
        self.assertFalse(self.check_readiness([{
            "kind": "DaemonSet",
            "metadata": {"name": "nvidia-device-plugin"},
            "status": {"desiredNumberScheduled": 1, "numberReady": 0},
        }]))


if __name__ == "__main__":
    unittest.main()
