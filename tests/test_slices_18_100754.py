"""Golden values for all 13 slices of NARA RG 18 / 100754, checked by hand
against context_text exported from index1795026330751."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import enrichment as e  # noqa: E402

AMC, AAF = "Headquarters Air Materiel Command", "Headquarters Army Air Forces"
# suffix: (classificationLevel, documentType, docYear, originatingOrg)
EXPECTED = {
    "p01-02": ("SECRET", "Letter", 1947, AMC),
    "p03-04": ("SECRET", "Letter", 1947, None),
    "p05-07": ("SECRET", "Letter", 1947, "Headquarters Fourth Air Force"),
    "p08-10": ("SECRET", "Letter", 1947, AMC),
    "p11-14": ("SECRET", "Letter", 1947, None),  # "Headquarters, Army Air Forces issue..." is prose
    "p15-16": ("SECRET", "Letter", 1947, None),
    "p17": (None, "Form", 1947, AAF),
    "p18-19": ("CONFIDENTIAL", "Letter", 1947, AMC),
    "p20": ("CONFIDENTIAL", "Letter", 1947, None),
    "p21-22": ("CONFIDENTIAL", "Letter", 1947, "Headquarters Kirtland Field"),
    "p23-25": ("SECRET", "Memo", 1947, AMC),
    "p26-27": ("RESTRICTED", "Memo", 1947, AAF),  # "(20 MAY 44)" is the form print date
    "p28": ("RESTRICTED", "Memo", 1947, AAF),     # OCR "AUG 2 9 1947"
}


class TestSlices100754(unittest.TestCase):
    def test_all_slices(self):
        with open(os.path.join(HERE, "slices_18_100754.json"), encoding="utf-8") as f:
            slices = json.load(f)
        self.assertEqual(len(slices), 13)
        for s in slices:
            key = s["title"].split("__")[-1].replace(".curated.json", "")
            out = e.extract_tier1(s["context_text"], file_name=s["title"])
            got = (out["classificationLevel"], out["documentType"], out["docYear"], out["originatingOrg"])
            with self.subTest(slice=key):
                self.assertEqual(got, EXPECTED[key])
                self.assertEqual((out["naraRecordGroup"], out["naraItemNumber"]), ("18", "100754"))


if __name__ == "__main__":
    unittest.main()
