import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from scripts.restore_x_random_artifact import exact_callback, preserve_qualified

class ExactArtifactTests(unittest.TestCase):
    def test_different_bytes_recipe_or_destination_never_upload(self):
        frozen={'result':{'output_sha256':'a'*64,'recipe':{'seed':3}},'object_key':'original/a.mp4'}
        for field in ('output_sha256','recipe','key'):
            journal=Mock();result=copy.deepcopy(frozen['result']);key=frozen['object_key']
            if field=='key':key='changed.mp4'
            else:result[field]='changed'
            with self.assertRaisesRegex(RuntimeError,'regenerated_bytes_differ'):
                exact_callback(journal,frozen)(result,key)
            journal.qualified.assert_not_called()

    def test_only_original_qualified_result_can_proceed_to_upload(self):
        journal=Mock();frozen={'result':{'output_sha256':'a'*64},'object_key':'original/a.mp4'}
        exact_callback(journal,frozen)(copy.deepcopy(frozen['result']),frozen['object_key'])
        journal.qualified.assert_called_once_with(frozen['result'],frozen['object_key'])

    def test_only_verified_cache_survives_temporary_output_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'render.mp4';source.write_bytes(b'qualified')
            target=Path(root)/'cache'/'original.mp4'
            expected={'output_sha256':hashlib.sha256(b'qualified').hexdigest(),'output_size':9}
            preserve_qualified(source,target,expected);source.unlink()
            self.assertEqual(target.read_bytes(),b'qualified')
            source.write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError,'cached_bytes_differ'):preserve_qualified(source,target,expected)
            self.assertEqual(target.read_bytes(),b'qualified')
