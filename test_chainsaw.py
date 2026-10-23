import gzip
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import extract
import server


class ChainsawGalleryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.test_dir = os.path.join(self.tmp.name, "do_MLC_test")
        os.makedirs(self.test_dir)
        with open(os.path.join(self.test_dir, "test.rpt"), "w"):
            pass
        with open(os.path.join(self.test_dir, "TEST_END"), "w"):
            pass
        self.folder = os.path.join(self.test_dir, "cfi_jem_trackers", "chainsaw_mem")
        os.makedirs(self.folder)
        self.image = os.path.join(self.folder, "output.overall_bw_samples.png.gz")
        with gzip.open(self.image, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nexample")
        with gzip.open(os.path.join(self.folder, "output.overall_bw_stats.txt.gz"), "wt") as fh:
            fh.write("{'Overall Mean BW of soc_mem_rd_data (MBytes/s)': 39142.1}\n")
        with gzip.open(os.path.join(self.folder, "output.counts.txt.gz"), "wt") as fh:
            fh.write("name,count\nsoc_mem_rd_data,2366790\n")
        latency = os.path.join(self.test_dir, "ddr_latency_read")
        os.makedirs(latency)
        with gzip.open(os.path.join(latency, "output.latencies_histogram.png.gz"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nexample")

    def test_gallery_preserves_graphs_and_metrics(self):
        groups = extract.chainsaw_gallery(self.test_dir)
        self.assertEqual(len(groups), 2)
        memory = next(g for g in groups if g["folder"].endswith("chainsaw_mem"))
        self.assertEqual(memory["images"], ["cfi_jem_trackers/chainsaw_mem/output.overall_bw_samples.png.gz"])
        self.assertEqual(memory["metrics"]["overall_bw_stats"][0]["value"], 39142.1)
        self.assertEqual(memory["metrics"]["counts"][0]["value"], 2366790)

    def test_bandwidth_summaries_without_images_are_included(self):
        folder = os.path.join(self.test_dir, "trackers_cdie0", "chainsaw_idi")
        os.makedirs(folder)
        with gzip.open(os.path.join(folder, "output.overall_bw_stats.txt.gz"), "wt") as fh:
            fh.write("{'Overall Mean BW of idi_c2u_data (MBytes/s)': 1234}\n")
        with gzip.open(os.path.join(folder, "output.bw_stats_bygrp.txt.gz"), "wt") as fh:
            fh.write("{'Mean BW - (idi_c2u_data) of core_id IA_IDI_1 (MBytes/s)': 1234}\n")
        idi = next(g for g in extract.chainsaw_gallery(self.test_dir) if g["folder"].endswith("chainsaw_idi"))
        self.assertEqual(idi["images"], [])
        self.assertEqual(idi["metrics"]["overall_bw_stats"][0]["value"], 1234)
        self.assertEqual(idi["metrics"]["bw_stats_bygrp"][0]["value"], 1234)

    def test_endpoints_return_png_and_reject_non_graphs(self):
        server.ALLOWED_ROOTS[:] = [self.tmp.name]
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_port}"
        with urlopen(base + "/api/chainsaw?" + urlencode({"path": self.test_dir})) as response:
            self.assertEqual(len(json.load(response)["groups"]), 2)
        args = {"path": self.test_dir, "file": "cfi_jem_trackers/chainsaw_mem/output.overall_bw_samples.png.gz"}
        with urlopen(base + "/api/chainsaw/image?" + urlencode(args)) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG"))
        for file in ("../../outside.png.gz", "test.rpt", "cfi_jem_trackers/chainsaw_mem/missing.png.gz"):
            with self.subTest(file=file), self.assertRaises(HTTPError) as raised:
                urlopen(base + "/api/chainsaw/image?" + urlencode({"path": self.test_dir, "file": file}))
            self.assertIn(raised.exception.code, (403, 400))
            raised.exception.close()


if __name__ == "__main__":
    unittest.main()
