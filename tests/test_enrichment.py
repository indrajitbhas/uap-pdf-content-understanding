"""Self-contained tests for Tier 1 enrichment (no `func start` needed).

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import enrichment as e  # noqa: E402

SAMPLE_PAYLOAD = {"values": [{"recordId": "1", "data": {
    "pages": [
        {"pageNumber": 1, "text": "HqAMC form No. 10-514 SECRET HEADQUARTERS AIR MATERIEL COMMAND DECLASSIFIED "
                                  "Authority: NND 700188 WRIGHT FIELD, DAYTON, OHIO DEC 19 1947 SUBJECT: Flying "
                                  "Discs TO: Chief of Staff United States Air Force"},
        {"pageNumber": 2, "text": "DATE: 22 Dec 47 FROM: OIN TYPE: R&R"},
    ],
    "fileName": "18_100754_ General 1946-7_Vol_2__p01-02.curated.json",
    "sourceJson": "18_100754_ General 1946-7_Vol_2.json",
}}]}

EXPECTED_TIER1 = {
    "naraRecordGroup": "18",
    "naraItemNumber": "100754",
    "declassificationAuthority": "NND 700188",
    "classificationLevel": "SECRET",
    "documentType": "Letter",
    "docYear": 1947,
    "originatingOrg": "Headquarters Air Materiel Command",
}


def _sample_text():
    pages = SAMPLE_PAYLOAD["values"][0]["data"]["pages"]
    return "\n\n".join(p["text"] for p in pages)


class TestSamplePayload(unittest.TestCase):
    def test_extract_tier1_sample(self):
        d = SAMPLE_PAYLOAD["values"][0]["data"]
        out = e.extract_tier1(_sample_text(), d["fileName"], d["sourceJson"])
        print("\nTier1 output:", json.dumps(out, indent=2))
        self.assertEqual(out, EXPECTED_TIER1)
        self.assertIsInstance(out["docYear"], int)


class TestHandler(unittest.TestCase):
    """End-to-end through the HTTP handler; skipped if azure-functions isn't installed."""

    def test_combine_envelope(self):
        try:
            import azure.functions as func
            import function_app
        except ImportError:
            self.skipTest("azure-functions not installed")
        req = func.HttpRequest(method="POST", url="/api/combine",
                               body=json.dumps(SAMPLE_PAYLOAD).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
        handler = function_app.combine
        handler = getattr(handler, "build", lambda: None)() or handler
        handler = getattr(handler, "get_user_function", lambda: handler)()
        resp = handler(req)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.get_body())
        rec = body["values"][0]
        print("\nHandler output:", json.dumps(body, indent=2))
        self.assertEqual(rec["recordId"], "1")
        self.assertEqual(rec["errors"], [])
        data = rec["data"]
        # existing outputs intact
        self.assertEqual(data["pageFrom"], 1)
        self.assertEqual(data["pageTo"], 2)
        self.assertEqual(data["sourcePdfName"], "18_100754_ General 1946-7_Vol_2.pdf")
        self.assertIn("NND 700188", data["contentText"])
        for k, v in EXPECTED_TIER1.items():
            self.assertEqual(data[k], v, k)


class TestMissingValues(unittest.TestCase):
    def test_all_none_inputs(self):
        out = e.extract_tier1(None, None, None)
        self.assertEqual(set(out), set(e.TIER1_FIELDS))
        self.assertTrue(all(v is None for v in out.values()), out)

    def test_empty_strings(self):
        out = e.extract_tier1("", "", "")
        self.assertTrue(all(v is None for v in out.values()), out)

    def test_non_string_filename(self):
        self.assertIsNone(e.extract_nara_record_group(12345))

    def test_text_without_signals(self):
        out = e.extract_tier1("lorem ipsum dolor", "random.json")
        self.assertEqual(out["documentType"], "Other")
        for k in ("naraRecordGroup", "naraItemNumber", "declassificationAuthority",
                  "classificationLevel", "docYear", "originatingOrg"):
            self.assertIsNone(out[k], k)


class TestExtractors(unittest.TestCase):
    def test_filename_fallback_to_source_json(self):
        self.assertEqual(e.extract_nara_item_number("bad.json", "341_6369445_General.json"), "6369445")

    def test_authority_variants(self):
        self.assertEqual(e.extract_declassification_authority("DECLASSIFIED NND700188"), "NND 700188")
        self.assertEqual(e.extract_declassification_authority("Authority: EO 12958"), "EO 12958")

    def test_classification_priority(self):
        self.assertEqual(e.extract_classification_level("RESTRICTED ... CONFIDENTIAL"), "CONFIDENTIAL")
        self.assertEqual(e.extract_classification_level("TOP SECRET"), "TOP SECRET")
        self.assertIsNone(e.extract_classification_level("DECLASSIFIED"))

    def test_doc_types(self):
        self.assertEqual(e.extract_document_type("MEMORANDUM FOR THE RECORD"), "Memo")
        self.assertEqual(e.extract_document_type("TWX PRIORITY"), "Telegram")
        self.assertEqual(e.extract_document_type("AF FORM NO. 112"), "Form")
        self.assertEqual(e.extract_document_type("INCIDENT REPORT summary"), "Report")

    def test_doc_year_patterns(self):
        self.assertEqual(e.extract_doc_year("dated 23 September 1947"), 1947)
        self.assertEqual(e.extract_doc_year("Sep 23 1948"), 1948)
        self.assertEqual(e.extract_doc_year("on 9/23/47"), 1947)
        self.assertEqual(e.extract_doc_year("NOV 1949"), 1949)
        # most frequent year wins; a tie goes to the latest (letters cite older events)
        self.assertEqual(e.extract_doc_year("NOV 1949 and 3 Jan 1952"), 1952)
        self.assertEqual(e.extract_doc_year("sighting 8 July 1946. 2 Oct 1947, ref 1 Oct 1947"), 1947)
        self.assertIsNone(e.extract_doc_year("12 May 2015"))
        # form-revision stamps are ignored
        self.assertEqual(e.extract_doc_year("(Rev 10 Sep 46) ... DEC 19 1947"), 1947)

    def test_org_variants(self):
        self.assertEqual(e.extract_originating_org("HEADQUARTERS ARMY AIR FORCES WASHINGTON 25 D C"),
                         "Headquarters Army Air Forces")
        self.assertEqual(e.extract_originating_org("HEADQUARTERS DEPARTMENT OF THE ARMY WASHINGTON"),
                         "Headquarters Department of the Army")
        self.assertEqual(e.extract_originating_org("FROM: Commanding General, AMC TO: CSAF"),
                         "Commanding General, AMC")


if __name__ == "__main__":
    unittest.main(verbosity=2)
