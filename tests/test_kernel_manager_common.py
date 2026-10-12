import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kernel_manager_common as common


class KernelManagerCommonTests(unittest.TestCase):
    def test_log_event_writes_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(common, "_CACHE_DIR", Path(tmp)), \
                 mock.patch.object(common, "_LOG_PATH", Path(tmp) / "operations.jsonl"):
                event = common.log_event("build_started", version="6.12.4", jobs=8)
                self.assertEqual(event["event"], "build_started")
                lines = (Path(tmp) / "operations.jsonl").read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 1)
                payload = json.loads(lines[0])
                self.assertEqual(payload["version"], "6.12.4")
                self.assertEqual(payload["jobs"], 8)

    def test_write_build_metadata_persists_latest_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(common, "_CACHE_DIR", Path(tmp)), \
                 mock.patch.object(common, "_METADATA_PATH", Path(tmp) / "build-metadata.json"):
                record = common.write_build_metadata(version="6.12.4", status="complete", jobs=8)
                self.assertEqual(record["status"], "complete")
                saved = common.read_build_metadata()
                self.assertEqual(saved["version"], "6.12.4")
                self.assertEqual(saved["jobs"], 8)


if __name__ == "__main__":
    unittest.main()
