"""Pure deployment validation: never invokes systemd, MySQL, or production files."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fb_assignment_deploy', ROOT / 'ops/fb-page-assignment/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentValidationTests(unittest.TestCase):
    def test_exact_ddl_target_and_no_second_statement(self):
        sql = 'CREATE TABLE IF NOT EXISTS ads_ai.fb_drama_page_assignment_log (task_id CHAR(32));'
        self.assertEqual(deploy.validate_ddl(sql), sql)
        for invalid in (
            sql.replace('fb_drama_page_assignment_log', 'another_table'),
            sql.replace('ads_ai.', 'kunlunads_dev.'),
            sql + '\nDROP TABLE ads_ai.fb_drama_page_assignment_log;',
            sql + '\nSELECT 1;',
        ):
            with self.assertRaises(RuntimeError):
                deploy.validate_ddl(invalid)

    def test_committed_schema_passes_allowlist(self):
        schema = ROOT / 'features/fb_page_assignment/schema.sql'
        self.assertIn('fb_drama_page_assignment_log', deploy.validate_ddl(schema.read_text(encoding='utf-8')))

    def test_environment_preserves_token_and_separates_reader_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / 'source.env', root / 'assignment.env'
            source.write_text('ADMIN_MAPPING_MYSQL_HOST=101.32.56.53\n'
                              'ADMIN_MAPPING_MYSQL_PORT=63350\n'
                              'ADMIN_MAPPING_MYSQL_USER=test-reader\n'
                              'ADMIN_MAPPING_MYSQL_PASSWORD=fixture-password\n'
                              'FEISHU_APP_ID=test-app\nFEISHU_APP_SECRET=test-secret\n'
                              'DRAMA_JOB_DB_PATH=/fixture/jobs.sqlite3\n'
                              'UNRELATED_SECRET=do-not-copy\n', encoding='utf-8')
            destination.write_text('FB_PAGE_ASSIGNMENT_API_TOKEN=existing-fixture-token\n', encoding='utf-8')
            with patch.object(deploy, 'ENV', destination):
                values = deploy.write_environment(source)
                again = deploy.write_environment(source)
            self.assertEqual(values['FB_PAGE_ASSIGNMENT_API_TOKEN'], 'existing-fixture-token')
            self.assertEqual(again['FB_PAGE_ASSIGNMENT_API_TOKEN'], values['FB_PAGE_ASSIGNMENT_API_TOKEN'])
            self.assertEqual(values['ADMIN_MAPPING_MYSQL_PORT'], '63350')
            self.assertEqual(values['FB_PAGE_ASSIGNMENT_MYSQL_WRITE_PORT'], '63353')
            self.assertEqual(values['DRAMA_JOB_DB_PATH'], '/fixture/jobs.sqlite3')
            self.assertNotIn('UNRELATED_SECRET', values)

    def test_rejects_reader_write_endpoint_without_replacing_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / 'source.env', root / 'assignment.env'
            source.write_text('ADMIN_MAPPING_MYSQL_HOST=101.32.56.53\n'
                              'ADMIN_MAPPING_MYSQL_PORT=63353\n'
                              'ADMIN_MAPPING_MYSQL_USER=test-reader\n'
                              'ADMIN_MAPPING_MYSQL_PASSWORD=fixture-password\n', encoding='utf-8')
            destination.write_text('FB_PAGE_ASSIGNMENT_API_TOKEN=existing-fixture-token\n', encoding='utf-8')
            before = destination.read_bytes()
            with patch.object(deploy, 'ENV', destination), self.assertRaises(RuntimeError):
                deploy.write_environment(source)
            self.assertEqual(destination.read_bytes(), before)

    def test_path_boundary_does_not_match_similar_prefix(self):
        root = Path('/mnt/data-disk')
        self.assertTrue(deploy.inside(root / 'fb-page-assignment' / 'deployments', root))
        self.assertFalse(deploy.inside(Path('/mnt/data-disk-other/deployment'), root))


if __name__ == '__main__':
    unittest.main()
