import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location('lane', pathlib.Path(__file__).with_name('worker.py'))
lane = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lane)


class ScopeTests(unittest.TestCase):
    def test_cover_only_is_accepted(self):
        class Executor:
            def capabilities(self): return lane.CAPABILITIES
        lane.assert_cover_only(Executor())

    def test_publish_and_other_renderers_fail_closed(self):
        for cap in ['publish:youtube', 'prepare:fb', 'youtube_publish']:
            with self.subTest(cap=cap):
                class Executor:
                    def capabilities(self): return lane.CAPABILITIES + [cap]
                with self.assertRaises(RuntimeError): lane.assert_cover_only(Executor())

    def test_unconfigured_tool_fails_closed(self):
        class Executor:
            def capabilities(self): return ['backend:cpu']
        with self.assertRaises(RuntimeError): lane.assert_cover_only(Executor())


if __name__ == '__main__': unittest.main()
