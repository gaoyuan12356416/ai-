import threading
import unittest

from features.fb_auto_posts.languages import candidate_snapshots_for_pages
from features.fb_auto_posts.repositories import CandidateSnapshot, PageTarget


class LanguageQueryConcurrencyTests(unittest.TestCase):
    def test_seven_languages_share_three_read_lanes(self):
        lock = threading.Lock()
        first_three = threading.Event()
        active = peak = 0
        calls = []

        class Materials:
            def candidate_snapshot(self, config):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    calls.append(config['language'])
                    if active == 3:
                        first_three.set()
                if not first_three.wait(5):
                    raise RuntimeError('language queries were serialized')
                with lock:
                    active -= 1
                return CandidateSnapshot((), (), ())

        languages = ['en', 'es', 'id', 'pl', 'th', 'tl', 'zh-tw']
        pages = [PageTarget('62', ('62',), str(i), '248', 'UTC', lang, 1)
                 for i, lang in enumerate(languages)]
        result = candidate_snapshots_for_pages({}, pages, Materials())
        self.assertEqual(peak, 3)
        self.assertCountEqual(calls, languages)
        self.assertEqual(set(result), set(languages))


if __name__ == '__main__':
    unittest.main()
