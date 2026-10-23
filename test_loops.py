import gzip
import os
import tempfile
import unittest
import zipfile

import extract


class LoopExtractionTests(unittest.TestCase):
    def test_expected_instructions_do_not_replace_missing_recorded_count(self):
        summary = {"name": "W6", "config": {}, "dram": {"channels": {}}, "fail_reasons": []}
        row = extract.flat_row(summary, {"xlsx": {"instructions": None, "expected_instructions": 1179647}})
        self.assertIsNone(row["instrs_retired"])
        self.assertEqual(row["instrs_expected"], 1179647)

    def test_scheduler_and_workload_loops(self):
        with tempfile.TemporaryDirectory() as root:
            with gzip.open(os.path.join(root, "mlc.output.bl2.gz"), "wt") as fh:
                fh.write('DefaultConcurrencySchedulerParams CHOOSER{\n'
                         ' LoopingCount := CHOICE{\n value := "1";\n };\n'
                         ' LoopingEnable := CHOICE{\n value := "0";\n };\n'
                         ' LoopingType := CHOICE{\n value := "Cycle";\n };\n }\n')
            self.assertEqual(extract.scheduler_loops(root),
                             {"LoopingCount": 1, "LoopingEnable": 0, "LoopingType": "Cycle"})
            ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            cells = {118: {"G": "innerLoop per Loop"},
                     119: {"G": "HowManyMoves", "I": "16"},
                     120: {"G": "HowManyMovesPerStride", "I": "4"},
                     121: {"G": "OutterLoop", "I": "1"},
                     132: {"B": "Lsz:", "C": "0x00000040"},
                     133: {"B": "Loops", "C": "0x00000001"}}
            rows = "".join(
                f'<row r="{r}">' + "".join(
                    f'<c r="{col}{r}" t="inlineStr"><is><t>{value}</t></is></c>'
                    for col, value in values.items()) + "</row>"
                for r, values in cells.items())
            with zipfile.ZipFile(os.path.join(root, "mlc.xlsx"), "w") as z:
                z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{rows}</sheetData></worksheet>')
            loops = extract.xlsx_stats(root)["workload_loops"]
            self.assertEqual(loops, {"HowManyMoves": 16, "HowManyMovesPerStride": 4,
                                     "OutterLoop": 1, "Lsz": 64, "Loops": 1})


if __name__ == "__main__":
    unittest.main()
