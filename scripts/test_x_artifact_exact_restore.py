import copy
import unittest
from unittest.mock import Mock
from scripts.restore_x_random_artifact import exact_callback

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
