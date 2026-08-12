from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "x-auto-post-static-files.txt"
STATIC_ROOT = ROOT / "static"


class XAutoPostStaticDeployContractTests(unittest.TestCase):
    def manifest_items(self) -> list[str]:
        return [
            line.strip()
            for line in MANIFEST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def test_manifest_contains_every_feature_asset_exactly_once(self) -> None:
        expected = sorted(path.name for path in STATIC_ROOT.glob("x-auto-publish*"))
        actual = self.manifest_items()
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(sorted(actual), expected)
        self.assertIn("x-auto-publish.css", actual)

    def test_manifest_covers_page_local_references(self) -> None:
        manifest = set(self.manifest_items())
        referenced: set[str] = set()
        for page in STATIC_ROOT.glob("x-auto-publish-*.html"):
            source = page.read_text(encoding="utf-8")
            referenced.update(
                match.group(1)
                for match in re.finditer(
                    r"(?:href|src)=[\"']/((?:x-auto-publish)[^\"']+)[\"']",
                    source,
                )
            )
        self.assertTrue(referenced)
        self.assertLessEqual(referenced, manifest)

    def test_manifest_paths_are_flat_safe_files(self) -> None:
        for item in self.manifest_items():
            path = Path(item)
            self.assertEqual(path.name, item)
            self.assertTrue((STATIC_ROOT / item).is_file())


if __name__ == "__main__":
    unittest.main()
