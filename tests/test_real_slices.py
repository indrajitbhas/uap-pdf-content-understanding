"""Regressions found in the first index1795026330751 run (13 records)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import enrichment as e  # noqa: E402
from fixtures_real import P08_10, P17, P18_19_SNIPPET, P20  # noqa: E402


class TestRealSlices(unittest.TestCase):
    def test_p08_10_year_ignores_print_code(self):
        # "WF-L-29 APR 46" is a print code; letters are dated 24 NOV 1947 / SEP 23 1947.
        self.assertEqual(e.extract_doc_year(P08_10), 1947)

    def test_p18_19_headquarters_as_noun_is_not_an_org(self):
        self.assertIsNone(e.extract_originating_org(P18_19_SNIPPET))

    def test_p20_ocr_garbled_confidential(self):
        self.assertEqual(e.extract_classification_level(P20), "CONFIDENTIAL")

    def test_p17_reference_form(self):
        self.assertEqual(e.extract_document_type(P17), "Form")
        self.assertIsNone(e.extract_classification_level(P17))  # no marking on this page

    def test_ocr_variants_and_false_friends(self):
        self.assertEqual(e.extract_classification_level("SECONET Basic Ltr"), "SECRET")
        self.assertEqual(e.extract_classification_level("RESTRICTD"), "RESTRICTED")
        for word in ("SECRETARY OF WAR", "CONTINENTAL AIR COMMAND", "CONFIDENCE", "SELECTED"):
            self.assertIsNone(e.extract_classification_level(word), word)

    def test_letterhead_org_still_found(self):
        self.assertEqual(e.extract_originating_org("SECRET HEADQUARTERS AIR MATERIEL COMMAND WRIGHT FIELD"),
                         "Headquarters Air Materiel Command")
        self.assertEqual(e.extract_originating_org("HEADQUARTERS KIRTLAND FIELD ALBUQUERQUE, NEW MEXICO"),
                         "Headquarters Kirtland Field")


if __name__ == "__main__":
    unittest.main()
