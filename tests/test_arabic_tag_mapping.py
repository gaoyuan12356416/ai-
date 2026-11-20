"""Arabic income evidence maps to the same controlled themes as other languages."""
import unittest
from features.x_tag_revenue.tags import lookup
from features.x_tag_revenue.taxonomy import classify


class ArabicTagMappingTests(unittest.TestCase):
    def test_all_labels_of_arabic_war_god_drama_are_recognized(self):
        expected = {
            'خيال': 'fantasy', 'إله الحرب': 'war_god',
            'الخيال الأسطوري': 'mythological_fiction', 'الأساطير الإسكندنافية': 'norse_myth',
            'كشف الهوية': 'identity_reveal', 'مثلث الحب': 'love_triangle',
            'هيمنة ساحقة': 'overpowered_dominance', 'مثير ومشوق': 'thrilling_suspenseful',
            'الخيال الملحمي': 'high_fantasy',
        }
        for label, tag in expected.items():
            self.assertEqual(lookup(label)['id'], tag)
            self.assertTrue(lookup(label)['matchable'])
        arabic = classify(list(expected))
        canonical = classify(list(expected.values()))
        self.assertEqual(arabic['themes'], canonical['themes'])
        self.assertIn('revenge', arabic['themes'])
        self.assertIn('fantasy', arabic['themes'])

    def test_common_female_and_modern_drama_labels(self):
        for label, tag in [('رئيس تنفيذي', 'ceo'), ('تطور المرأة', 'female_growth'),
                           ('حب بعد الزواج', 'love_after_vows'), ('رومانسي حلو', 'sweet'),
                           ('معاصر', 'contemporary'), ('خيال علمي', 'sci_fi')]:
            self.assertEqual(lookup(label)['id'], tag)

    def test_unknown_arabic_labels_do_not_invent_evidence(self):
        self.assertFalse(lookup('مجهول')['matchable'])
        self.assertFalse(lookup('قزم')['matchable'])


if __name__ == '__main__':
    unittest.main()
