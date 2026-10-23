import gzip
import os
import tempfile
import unittest
import zipfile

import extract


class PrefetchRateTests(unittest.TestCase):
    def test_requires_both_counts_and_positive_denominator(self):
        self.assertEqual(extract.prefetch_rate({"P": 3, "D": 1}, "P", "D"), 0.75)
        self.assertEqual(extract.prefetch_rate({"P": 0, "D": 5}, "P", "D"), 0)
        self.assertIsNone(extract.prefetch_rate({"P": 5}, "P", "D"))
        self.assertIsNone(extract.prefetch_rate({"P": 0, "D": 0}, "P", "D"))

    def test_workbook_rates_and_csv_fields(self):
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        cells = {
            1: {"B": "Total Opcodes Seen @ NVL-IDI", "D": "C2U_RFO_PREF",
                "E": "C2U_RFO", "F": "C2U_DRD_OPT_PREF", "G": "C2U_DRD_OPT"},
            2: {"C": "SUMMARY", "D": "20", "E": "80", "F": "3", "G": "1"},
        }
        rows = "".join(
            f'<row r="{r}">' + "".join(
                f'<c r="{col}{r}" t="inlineStr"><is><t>{value}</t></is></c>'
                for col, value in values.items()) + "</row>"
            for r, values in cells.items())
        with tempfile.TemporaryDirectory() as root:
            with zipfile.ZipFile(os.path.join(root, "test.xlsx"), "w") as workbook:
                workbook.writestr("xl/worksheets/sheet1.xml",
                                  f'<worksheet xmlns="{ns}"><sheetData>{rows}</sheetData></worksheet>')
            xlsx = extract.xlsx_stats(root)
            self.assertEqual(xlsx["rfo_pref_rate"], 0.2)
            self.assertEqual(xlsx["drd_pref_rate"], 0.75)
            summary = {"config": {}, "dram": {"channels": {}}, "fail_reasons": []}
            row = extract.flat_row(summary, {"xlsx": xlsx})
            self.assertEqual(row["rfo_pref_rate"], 0.2)
            self.assertEqual(row["drd_pref_rate"], 0.75)
            empty = extract.flat_row(summary)
            self.assertIsNone(empty["rfo_pref_rate"])
            self.assertIsNone(empty["drd_pref_rate"])

    def test_compressed_workbook_is_discovered_and_parsed(self):
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        with tempfile.TemporaryDirectory() as root:
            workbook_path = os.path.join(root, "test.xlsx")
            with zipfile.ZipFile(workbook_path, "w") as workbook:
                workbook.writestr(
                    "xl/worksheets/sheet1.xml",
                    f'<worksheet xmlns="{ns}"><sheetData>'
                    '<row r="1"><c r="B1" t="inlineStr"><is><t>Total Opcodes Seen @ NVL-IDI:</t></is></c>'
                    '<c r="D1" t="inlineStr"><is><t>C2U_DRD_OPT_PREF</t></is></c>'
                    '<c r="E1" t="inlineStr"><is><t>C2U_DRD_OPT</t></is></c></row>'
                    '<row r="2"><c r="C2" t="inlineStr"><is><t>SUMMARY</t></is></c>'
                    '<c r="D2"><v>3</v></c><c r="E2"><v>1</v></c></row>'
                    '</sheetData></worksheet>',
                )
            with open(workbook_path, "rb") as source, gzip.open(workbook_path + ".gz", "wb") as dest:
                dest.write(source.read())
            os.remove(workbook_path)
            self.assertTrue(extract.summary(root)["files"]["xlsx"])
            xlsx = extract.xlsx_stats(root)
            self.assertEqual(xlsx["idi_opcodes"]["C2U_DRD_OPT_PREF"], 3)
            self.assertEqual(xlsx["drd_pref_rate"], 0.75)
            self.assertEqual(xlsx["file"], "test.xlsx.gz")


if __name__ == "__main__":
    unittest.main()
