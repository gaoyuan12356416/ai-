import itertools
import random
import unittest
from datetime import datetime, timedelta, timezone
from features.hit_publish import assign, material_ids, timing, future_scope, future_allowed


class AssignmentTests(unittest.TestCase):
    def test_maximum_cardinality_does_not_steal_unique_account(self):
        self.assertEqual(assign(['a','b'], ['x','y'], {('a','x'):100,('a','y'):1,('b','x'):90}), {'b':'x','a':'y'})

    def test_score_breaks_equal_cardinality_ties(self):
        self.assertEqual(assign(['a','b'], ['x','y'], {('a','x'):100,('a','y'):99,('b','x'):98,('b','y'):1}), {'b':'x','a':'y'})

    def test_matches_exhaustive_small_allocations(self):
        rng=random.Random(3)
        for _ in range(30):
            ids, accounts = ['a','b','c','d'], ['x','y','z']
            scores={(m,a):rng.choice([0,0,1,10,100]) for m in ids for a in accounts}
            actual=assign(ids,accounts,scores)
            expected=(0,0)
            for choice in itertools.product([None,*accounts],repeat=len(ids)):
                used=[a for a in choice if a]
                if len(used)!=len(set(used)) or any(a and not scores[m,a] for m,a in zip(ids,choice)):
                    continue
                expected=max(expected,(len(used),sum(scores[m,a] for m,a in zip(ids,choice) if a)))
            self.assertEqual((len(actual),sum(scores[m,a] for m,a in actual.items())),expected)

    def test_input_and_release_boundaries(self):
        self.assertEqual(material_ids('1，2\n1;3'),['1','2','3'])
        for invalid in ['', ['0'], ['1;DROP'], [True]]:
            with self.assertRaises(ValueError): material_ids(invalid)
        now=datetime(2026,11,13,8,tzinfo=timezone.utc)
        later=now+timedelta(days=1)
        plan=timing({'C:pt-br':int(later.timestamp())},'C','pt',now)
        self.assertEqual(datetime.fromisoformat(plan['publish_at_utc']),later-timedelta(minutes=30))
        within=timing({'C:en':int((now+timedelta(minutes=10)).timestamp())},'C','en',now)
        self.assertEqual(datetime.fromisoformat(within['publish_at_utc']),now)
        with self.assertRaises(ValueError): timing({},'C','en',now)

    def test_future_exemption_is_exact_and_stage_scoped(self):
        self.assertFalse(future_allowed('A','en',100))
        with future_scope({'A:en':100}):
            self.assertTrue(future_allowed('A','en',100))
            self.assertFalse(future_allowed('B','en',100))
            self.assertFalse(future_allowed('A','en',101))
        self.assertFalse(future_allowed('A','en',100))
