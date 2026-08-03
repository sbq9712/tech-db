import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.runtime_assets import (
    AssetError,
    component_ready,
    parse_checksums,
    safe_extract,
    sha256_file,
)


class RuntimeAssetTests(unittest.TestCase):
    def test_parse_checksums(self):
        digest = "a" * 64
        self.assertEqual(parse_checksums(f"{digest}  file.tar.gz\n"), {"file.tar.gz": digest})

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "payload"
            path.write_bytes(b"tech-db")
            self.assertEqual(sha256_file(path), hashlib.sha256(b"tech-db").hexdigest())

    def test_safe_extract_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("../escape.txt")
                payload = b"escape"
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
            with self.assertRaises(AssetError):
                safe_extract(archive, root / "output")

    def test_component_ready_accepts_model_alternative(self):
        config = {
            "components": {
                "model": {
                    "required_any": [
                        "models/bge-m3/model.safetensors",
                        "models/bge-m3/pytorch_model.bin",
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            model = runtime / "models" / "bge-m3" / "model.safetensors"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"model")
            self.assertTrue(component_ready(config, "model", runtime))


if __name__ == "__main__":
    unittest.main()
