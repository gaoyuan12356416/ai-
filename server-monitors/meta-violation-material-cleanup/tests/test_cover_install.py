import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('cover_installer',Path(__file__).resolve().parents[1]/'deploy/install_cover.py')
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallBackupTests(unittest.TestCase):
    def state(self):
        return {installer.OLD_UNITS[0]:{'is-active':'inactive'},
                installer.OLD_UNITS[1]:{'is-active':'inactive','is-enabled':'disabled'}}

    def test_preserved_legacy_requires_explicit_off_and_disabled_inactive_units(self):
        installer.require_disabled_legacy({'live_enabled':False},self.state())
        for value in (True,None,0,'false'):
            with self.assertRaises(RuntimeError):
                installer.require_disabled_legacy({'live_enabled':value},self.state())

    def test_any_live_or_enabled_legacy_blocks_active_only_backup(self):
        for unit,field,value in (
            (installer.OLD_UNITS[0],'is-active','active'),
            (installer.OLD_UNITS[1],'is-active','active'),
            (installer.OLD_UNITS[1],'is-enabled','enabled')):
            state=self.state();state[unit][field]=value
            with self.assertRaises(RuntimeError):
                installer.require_disabled_legacy({'live_enabled':False},state)
