"""Extract summary / debug stats from emurun regression run areas.

Pure standard library so it runs on any NFS host with python3 (>=3.8).

Two levels of extraction per test directory:
  * summary(test_dir) -- fast: rpt, report.json, env, bw stats, test-name config, knobs
  * deep(test_dir)    -- slow: cbo trackers (L3 hit/miss), santa CFI trackers
                         (read latency / BW), postprocessed xlsx (IDI opcodes,
                         instructions), MRC timing registers
Results are cached on disk keyed by test path + rpt mtime.
"""

import ast
import concurrent.futures as cf
import csv
import glob
import gzip
import hashlib
import io
import json
import os
import re
import statistics
import zipfile
import xml.etree.ElementTree as ET

CACHE_DIR = os.environ.get("RUNDASH_CACHE", os.path.expanduser("~/.cache/runDashboard"))
SCHEMA_VERSION = 8
MAX_SCAN_DEPTH = 8
CHAINSAW_METRICS = ("output.counts.txt.gz", "output.overall_bw_stats.txt.gz",
                    "output.bw_stats_bygrp.txt.gz", "output.overall_latency_stats.txt.gz")
INC_PATH = re.compile(r"user\.MINIBIOS_[A-Z0-9_]*INC[A-Z0-9_]*PATHS=([^\s]+?\.inc)\b")
INC_REGISTER = re.compile(r"^;Assembly code for .*?CR_(.+?)_0_0_0_MCHBAR:", re.I)
RUN_STAGE = re.compile(r"\b(Entering|Leaving) stage:\s*(.+)")


# --------------------------------------------------------------------------- helpers

def _open(path):
    return gzip.open(path, "rt", errors="replace") if path.endswith(".gz") else open(path, errors="replace")


def _first(test_dir, *patterns):
    for p in patterns:
        hits = sorted(glob.glob(os.path.join(test_dir, p)))
        if hits:
            return hits[0]
    return None


def _num(v):
    if v is None:
        return None
    try:
        f = float(str(v).replace(",", "").strip())
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _cache_path(test_dir, kind):
    rpt = _first(test_dir, "*.rpt.gz", "*.rpt")
    stamp = os.path.getmtime(rpt) if rpt else 0
    key = hashlib.sha1(f"{SCHEMA_VERSION}|{kind}|{os.path.realpath(test_dir)}|{stamp}".encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{kind}_{key}.json")


def _cached(test_dir, kind, fn):
    path = _cache_path(test_dir, kind)
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        pass
    data = fn(test_dir)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    return data


# --------------------------------------------------------------------------- discovery

TEST_MARKERS = (".end_trex.env.gz", ".end_trex.env", "TEST_END.gz", "TEST_END", "logbook.log.gz",
                "logbook.log", "report.json.gz", ".test.yml.gz")


def is_test_dir(path):
    # List roots also carry a *.rpt (e.g. <list>.dump.rpt), so require a per-test marker too.
    if not (glob.glob(os.path.join(path, "*.rpt.gz")) or glob.glob(os.path.join(path, "*.rpt"))):
        return False
    return any(os.path.exists(os.path.join(path, m)) for m in TEST_MARKERS)


def find_test_dirs(root, max_depth=MAX_SCAN_DEPTH):
    """Return test directories (containing a *.rpt[.gz]) under root, not descending into tests."""
    root = os.path.realpath(root)
    if is_test_dir(root):
        return [root]
    found, frontier = [], [root]
    for _ in range(max_depth):
        nxt = []
        for d in frontier:
            try:
                entries = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for e in entries:
                if e.name.startswith(".") or e.name in ("results", "pre_post_exec_logs", "trackers_cdie0", "trackers_hub"):
                    continue
                (found if is_test_dir(e.path) else nxt).append(e.path)
        frontier = nxt
        if not frontier:
            break
    return sorted(found)


# --------------------------------------------------------------------------- summary parsers

RPT_KEY = re.compile(r"^\s*([A-Z][A-Z0-9/ _\-]*?):\s?(.*)$")


def parse_rpt(path):
    out = {"_results": []}
    if not path:
        return out
    with _open(path) as fh:
        for line in fh:
            m = RPT_KEY.match(line.rstrip("\n"))
            if not m:
                continue
            key = m.group(1).strip()
            if key == "TEST RESULT" and m.group(2).strip():
                out["_results"].append(m.group(2).strip())
            if key not in out:
                out[key] = m.group(2).strip()
    return out


def parse_report_json(path):
    if not path:
        return {}
    try:
        with _open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


TB_KEYS = ("SPARK VERSION", "EMURUN VERSION", "SIMICS BASE", "ZEBU ROOT", "QSLOT", "EXIT STATUS",
           "START TIME", "FINISH TIME")


def parse_tb_summary(path):
    out = {}
    if not path:
        return out
    with _open(path) as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z][A-Z /]+?):\s*(\S.*)$", line)
        if m and m.group(1).strip() in TB_KEYS:
            out.setdefault(m.group(1).strip(), m.group(2).strip())
        if "EFFICIENCY SCORE" in line and i + 2 < len(lines):
            score = re.search(r"[\d.]+", lines[i + 2])
            if score:
                out.setdefault("EFFICIENCY SCORE", score.group(0))
    return out


def parse_env(path):
    out = {}
    if not path:
        return out
    with _open(path) as fh:
        for line in fh:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)='(.*)'\s*$", line.rstrip("\n"))
            if m:
                out[m.group(1)] = m.group(2)
    return out


def _literal_lines(path):
    if not path:
        return []
    rows = []
    with _open(path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(ast.literal_eval(line))
                except (ValueError, SyntaxError):
                    pass
    return rows


def chainsaw_gallery(test_dir):
    """Discover existing Chainsaw PNGs and their adjacent summary metrics."""
    groups = []
    for root, dirs, files in os.walk(test_dir):
        rel = os.path.relpath(root, test_dir)
        depth = 0 if rel == "." else len(rel.split(os.sep))
        dirs[:] = sorted(d for d in dirs if depth < 4 and not d.startswith("."))
        if rel != "." and not any(part.startswith("chainsaw_") or part.startswith("ddr_latency_")
                                  for part in rel.split(os.sep)):
            continue
        images = sorted(f for f in files if f.endswith(".png.gz"))
        if not images and not any(filename in files for filename in CHAINSAW_METRICS[1:3]):
            continue
        metrics = {}
        for filename in CHAINSAW_METRICS:
            if filename not in files:
                continue
            path = os.path.join(root, filename)
            try:
                with gzip.open(path, "rt", errors="replace") as fh:
                    if filename == "output.counts.txt.gz":
                        entries = [{"name": row["name"], "value": _num(row["count"])}
                                   for row in csv.DictReader(fh)]
                    else:
                        entries = [{"name": k, "value": _num(v)}
                                   for line in fh if line.lstrip().startswith("{")
                                   for k, v in ast.literal_eval(line).items()]
                metrics[filename[7:-7]] = entries
            except (OSError, ValueError, SyntaxError, KeyError, csv.Error) as exc:
                metrics[filename[7:-7]] = {
                    "error": f"{type(exc).__name__}: {exc}"}
        groups.append({"folder": rel.replace(os.sep, "/"),
                       "images": [os.path.relpath(os.path.join(root, f), test_dir).replace(os.sep, "/")
                                  for f in images],
                       "metrics": metrics})
    return groups


BW_METRIC = {"ddr_rd_req": "rd", "ddr_wr_req": "wr"}


def parse_dram_bw(test_dir):
    """DRAM BW from chainsaw output; read/write kept separate, plus totals. MB/s."""
    out = {"rd_mean": None, "rd_max": None, "wr_mean": None, "wr_max": None,
           "dram_bw_mean": None, "dram_bw_max": None, "bw_window_ns": None, "channels": {}, "ranks": {}}
    for row in _literal_lines(_first(test_dir, "output.overall_bw_stats.txt.gz", "output.overall_bw_stats.txt")):
        for k, v in row.items():
            m = re.match(r"Overall (Mean|Max) BW of (\w+)", k)
            if m and m.group(2) in BW_METRIC:
                out[f"{BW_METRIC[m.group(2)]}_{m.group(1).lower()}"] = _num(v)
            elif k.startswith("Time Window"):
                out["bw_window_ns"] = round(_num(v), 1)
    for stat in ("mean", "max"):
        vals = [out[f"{d}_{stat}"] for d in ("rd", "wr") if out[f"{d}_{stat}"] is not None]
        out[f"dram_bw_{stat}"] = round(sum(vals), 1) if vals else None
    for row in _literal_lines(_first(test_dir, "output.bw_stats_bygrp.txt.gz", "output.bw_stats_bygrp.txt")):
        for k, v in row.items():
            m = re.search(r"(Mean|Max) BW - \((\w+)\) of rank_core_id (\d+)", k)
            if not m or m.group(2) not in BW_METRIC:
                continue
            rid = m.group(3)
            # rank_core_id digits: <MC><CH><RANK>
            name = f"MC{rid[0]}_CH{rid[1]}_R{rid[2]}" if len(rid) == 3 else rid
            out["ranks"].setdefault(name, {})[f"{BW_METRIC[m.group(2)]}_{m.group(1).lower()}"] = _num(v)
    for name, r in out["ranks"].items():
        c = out["channels"].setdefault(name.rsplit("_R", 1)[0], {})
        for k, v in r.items():
            c[k] = round(c.get(k, 0) + (v or 0), 1)
    for c in out["channels"].values():
        c["total_mean"] = round(c.get("rd_mean", 0) + c.get("wr_mean", 0), 1)
    return out


NAME_PATTERNS = {
    "cclk_mhz": r"CCLK_(\d+)MHz",
    "nclk_mhz": r"NCLK_(\d+)MHz",
    "iatom_mhz": r"iatom_(\d+)MHz",
    "ddr": r"((?:lp)?ddr\d+x?_\d+)",
    "msc": r"MSC_(ON|OFF)",
    "sagv": r"SAGV_(enabled|disabled)",
    "cstate": r"cState_(enabled|disabled)",
    "llc_mb": r"_(\d+)MB_",
    "llc_lanes": r"_\d+MB_(\d+)L",
    "dies_cores": r"_(\d+D\d+C)_",
    "traffic": r"_([RW]\d+)_\d+D",
}


def parse_test_name(name):
    out = {}
    for k, pat in NAME_PATTERNS.items():
        m = re.search(pat, name, re.I)
        if m:
            out[k] = _num(m.group(1)) if m.group(1).isdigit() else m.group(1)
    return out


def parse_knobs(cmdline):
    """Split a TEST CMD-LINE into categorized knobs: env vars, grouped options (-x ... -x-), flags."""
    toks = cmdline.split()
    knobs, i = [], 1 if toks else 0
    while i < len(toks):
        t = toks[i]
        if not t.startswith("-") and "=" in t:
            knobs.append({"cat": "env", "value": t})
            i += 1
            continue
        m = re.match(r"^-([A-Za-z0-9_]+)$", t)
        if m:
            closer = f"-{m.group(1)}-"
            if closer in toks[i + 1:]:
                j = toks.index(closer, i + 1)
                body = toks[i + 1:j]
                if m.group(1) in ("perspec", "bs", "ms"):
                    knobs.append({"cat": m.group(1), "value": " ".join(body)})
                else:
                    for b in body:
                        knobs.append({"cat": m.group(1), "value": b})
                i = j + 1
                continue
            if i + 1 < len(toks) and not toks[i + 1].startswith("-"):
                knobs.append({"cat": m.group(1), "value": toks[i + 1]})
                i += 2
                continue
            knobs.append({"cat": "flag", "value": t})
        i += 1
    return knobs


FUSE_FREQ = re.compile(r"(?:^|/)(?:punit_fw_fuses_|fw_fuses_)?([A-Z0-9_]*RATIO[A-Z0-9_]*)=(0x[0-9a-fA-F]+|\d+)")


def summary(test_dir):
    name = os.path.basename(test_dir.rstrip("/"))
    rpt = parse_rpt(_first(test_dir, "*.rpt.gz", "*.rpt"))
    rj = parse_report_json(_first(test_dir, "report.json.gz", "report.json"))
    tb = parse_tb_summary(_first(test_dir, "tb_perf_summary.log.gz", "tb_perf_summary.log"))
    env = parse_env(_first(test_dir, ".end_trex.env.gz", ".end_trex.env"))
    cmdline = env.get("TEST_CMDLINE") or rpt.get("TEST CMD-LINE", "")
    knobs = parse_knobs(cmdline)
    ratios = {}
    for k in knobs:
        if k["cat"].endswith("fuse_ovrd"):
            m = FUSE_FREQ.search(k["value"])
            if m:
                ratios[m.group(1)] = int(m.group(2), 0)
    mem_cfg = [k["value"] for k in knobs if k["cat"] == "emu_mem_cfg"]
    status = rpt.get("TEST STATUS") or ("PASS" if rj.get("RESULT") == "PASSED" else rj.get("RESULT"))
    return {
        "test_dir": test_dir,
        "name": name,
        "test_name": rpt.get("TEST NAME", name),
        "status": status or "UNKNOWN",
        "result": rpt.get("TEST RESULT"),
        "fail_reasons": rpt["_results"] if status and status.upper() != "PASS" else [],
        "exitstatus": _num(env.get("MODEL_EXIT_STATUS", tb.get("EXIT STATUS"))),
        "product": os.path.basename(rpt.get("MODEL VERSION", "")) or None,
        "model_path": rpt.get("MODEL VERSION"),
        "dut": rpt.get("DUT") or rpt.get("CLUSTER"),
        "cth_model": rpt.get("CTH MODEL"),
        "cycles": _num(rj.get("CYCLES RUN")),
        "ticks": _num(rj.get("TICKS RUN") or rpt.get("CYCLES")),
        "user": rj.get("USER"),
        "date": rpt.get("DATE/TIME RUN"),
        "date_sec": _num(rpt.get("DATE/TIME SEC")),
        "host": rj.get("HOST") or rpt.get("HOST OF RUN"),
        "platform": rj.get("PLATFORM"),
        "emu_runtime_s": _num(rj.get("Runtime")),
        "model_run_time_s": _num(rpt.get("MODEL RUN TIME")),
        "efficiency_score": _num(tb.get("EFFICIENCY SCORE")),
        "simulator_name": rpt.get("TEST SIM-TOOL", "emurun"),
        "emurun_version": tb.get("EMURUN VERSION"),
        "spark_version": tb.get("SPARK VERSION"),
        "simics_version": tb.get("SIMICS BASE"),
        "zebu_version": tb.get("ZEBU ROOT"),
        "runtools_version": rpt.get("RUNTOOLS_VERSION"),
        "qslot": tb.get("QSLOT"),
        "feedlist": rpt.get("BASENAME_FEEDLIST"),
        "dirtag": rpt.get("DIR TAG"),
        "mem_cfg": [m for m in mem_cfg if m != "cxm"] or mem_cfg,
        "config": parse_test_name(name),
        "scheduler_loops": scheduler_loops(test_dir),
        "fuse_ratios": ratios,
        "dram": parse_dram_bw(test_dir),
        "cmdline": cmdline,
        "knobs": knobs,
        "files": {k: bool(_first(test_dir, *v)) for k, v in {
            "rpt": ("*.rpt.gz", "*.rpt"), "xlsx": ("*.xlsx", "*.xlsx.gz"),
            "cbo": ("trackers_cdie0/cbo_tracker_*.log.gz",),
            "santa": ("cfi_jem_trackers/cdie0_santa*.log.gz",), "mrc": ("minibios_files/mrc_*/MRC.yaml.gz",),
        }.items()},
    }


def flow_summary(test_dir):
    """Read the run's actual stage transitions and referenced local memory recipe."""
    info = summary(test_dir)
    stages = []
    path = _first(test_dir, "logbook.log.gz", "logbook.log")
    if path:
        with _open(path) as fh:
            for line in fh:
                match = RUN_STAGE.search(line)
                if match:
                    if match.group(1) == "Entering":
                        stages.append({"name": match.group(2).strip(), "completed": False})
                    elif stages and stages[-1]["name"] == match.group(2).strip():
                        stages[-1]["completed"] = True
    includes = []
    # Only inspect recipes under the bucket's local recipe directory.
    recipe_root = os.path.realpath(os.path.join(test_dir, "..", "..", "..",
                                                 "ddg_recipe", "inc"))
    for ref in dict.fromkeys(INC_PATH.findall(info["cmdline"])):
        item = {"path": ref, "registers": [], "note": None}
        resolved = os.path.realpath(ref)
        if os.path.commonpath((recipe_root, resolved)) != recipe_root:
            item["note"] = "Recipe outside the bucket's local recipe directory; not inspected"
        elif not os.path.isfile(resolved):
            item["note"] = "Recipe file not found"
        elif os.path.getsize(resolved) > 256 * 1024:
            item["note"] = "Recipe exceeds 256 KB inspection limit"
        else:
            with open(resolved, errors="replace") as fh:
                item["registers"] = sorted({m.group(1).upper() for line in fh
                                            if (m := INC_REGISTER.match(line))})
        includes.append(item)
    return {"stages": stages, "stage_source": os.path.basename(path) if path else None,
            "includes": includes}


# --------------------------------------------------------------------------- deep parsers

def _count_tokens_gz(path, tokens, chunk=1 << 22):
    counts = {t: 0 for t in tokens}
    keep = max(len(t) for t in tokens) - 1
    tail = b""
    with gzip.open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            data = tail + buf
            # Count only up to len-keep and carry the rest, so tokens split across chunks count once.
            cut = max(0, len(data) - keep)
            for t in tokens:
                counts[t] += data.count(t, 0, cut + len(t) - 1) if cut else 0
            tail = data[cut:]
    for t in tokens:
        counts[t] += tail.count(t)
    return counts


def cbo_stats(test_dir, workers=6):
    files = sorted(glob.glob(os.path.join(test_dir, "trackers_cdie0", "cbo_tracker_*.log.gz")))
    files = [f for f in files if re.search(r"cbo_tracker_\d+\.log\.gz$", f)]
    if not files:
        return None
    toks = (b"un_cbo_llc_hit", b"un_cbo_llc_miss", b"un_cbo_pipe_pref_elimination")
    per = {}
    with cf.ThreadPoolExecutor(workers) as ex:
        for f, c in zip(files, ex.map(lambda p: _count_tokens_gz(p, toks), files)):
            per[re.search(r"cbo_tracker_(\d+)", f).group(1)] = {
                "hit": c[toks[0]], "miss": c[toks[1]], "pref_elim": c[toks[2]]}
    hit = sum(v["hit"] for v in per.values())
    miss = sum(v["miss"] for v in per.values())
    return {"l3_hit": hit, "l3_miss": miss,
            "l3_hit_rate": round(100.0 * hit / (hit + miss), 2) if hit + miss else None,
            "per_cbo": per}


SANTA_READS = {"RdDataMig", "RdData", "RdCode", "RdInv", "RdInvOwn", "RdCur", "RdDataMigOpt"}
SANTA_WRITE_DATA = ("Wb", "NcWr")


def _santa_file(path):
    pending, lat = {}, []
    reads = writes = 0
    t_first = t_last = None
    with gzip.open(path, "rt", errors="replace") as fh:
        for line in fh:
            if not line[:1].isdigit():
                continue
            f = line.split("|")
            if len(f) < 13:
                continue
            try:
                t = int(f[0])
            except ValueError:
                continue
            intf, op, rtid = f[2].strip(), f[7].strip(), f[12].strip()
            if intf == "T-REQ" and op in SANTA_READS:
                pending[rtid] = t
                reads += 1
                if t_first is None:
                    t_first = t
            elif intf == "R-DAT" and op == "MemData":
                t0 = pending.pop(rtid, None)
                if t0 is not None:
                    lat.append(t - t0)
                    t_last = t
            elif intf == "T-DAT" and op.startswith(SANTA_WRITE_DATA):
                writes += 1
    return lat, reads, writes, t_first, t_last


def santa_stats(test_dir):
    files = sorted(glob.glob(os.path.join(test_dir, "cfi_jem_trackers", "cdie0_santa*.log.gz")))
    if not files:
        return None
    out = {"per_santa": {}}
    all_lat, tot_r, tot_w, first, last = [], 0, 0, [], []
    with cf.ThreadPoolExecutor(len(files)) as ex:
        for f, (lat, r, w, t0, t1) in zip(files, ex.map(_santa_file, files)):
            key = os.path.basename(f).split(".")[0]
            out["per_santa"][key] = {
                "reads": r, "writes": w, "matched": len(lat),
                "lat_mean_ns": round(statistics.fmean(lat) / 1000, 2) if lat else None,
                "lat_min_ns": round(min(lat) / 1000, 2) if lat else None,
            }
            all_lat += lat
            tot_r += r
            tot_w += w
            if t0 is not None:
                first.append(t0)
            if t1 is not None:
                last.append(t1)
    window_ps = (max(last) - min(first)) if first and last else 0
    if all_lat:
        s = sorted(all_lat)
        out.update({
            "read_lat_mean_ns": round(statistics.fmean(s) / 1000, 2),
            "read_lat_min_ns": round(s[0] / 1000, 2),
            "read_lat_p50_ns": round(s[len(s) // 2] / 1000, 2),
            "read_lat_p99_ns": round(s[min(len(s) - 1, int(len(s) * 0.99))] / 1000, 2),
            "read_lat_max_ns": round(s[-1] / 1000, 2),
            "hist": _hist([v / 1000 for v in s]),
        })
    # 64B per cacheline; TIME column is in ps -> bytes/ps * 1e6 = MB/s
    out["read_bw_mbs"] = round(tot_r * 64 / window_ps * 1e6, 1) if window_ps else None
    out["total_bw_mbs"] = round((tot_r + tot_w) * 64 / window_ps * 1e6, 1) if window_ps else None
    out["reads"], out["writes"], out["window_ns"] = tot_r, tot_w, round(window_ps / 1000, 1)
    return out


def _hist(vals, bins=24):
    if not vals:
        return None
    hi = vals[min(len(vals) - 1, int(len(vals) * 0.995))]
    lo = vals[0]
    width = (hi - lo) / bins or 1
    counts = [0] * bins
    for v in vals:
        counts[min(bins - 1, int((v - lo) / width))] += 1
    return {"lo": round(lo, 1), "width": round(width, 2), "counts": counts}


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _read_xlsx_first_sheet(path, max_row=2000):
    """Return {row: {col: value}} for the first sheet using cached cell values only."""
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            archive = io.BytesIO(fh.read())
    else:
        archive = path
    with zipfile.ZipFile(archive) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        sheet = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))[0]
        rows = {}
        with z.open(sheet) as fh:
            for _, el in ET.iterparse(fh):
                if el.tag != f"{{{NS['m']}}}c":
                    continue
                m = CELL_REF.match(el.get("r", ""))
                if not m:
                    continue
                r = int(m.group(2))
                if r > max_row:
                    el.clear()
                    continue
                v = el.find("m:v", NS)
                if v is None or v.text is None:
                    is_ = el.find("m:is", NS)
                    val = "".join(t.text or "" for t in is_.iter(f"{{{NS['m']}}}t")) if is_ is not None else None
                else:
                    val = shared[int(v.text)] if el.get("t") == "s" else v.text
                if val not in (None, ""):
                    rows.setdefault(r, {})[m.group(1)] = val
                el.clear()
    return rows


def _col_idx(col):
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


def _int0(v):
    try:
        return int(str(v).strip(), 0)
    except (TypeError, ValueError):
        return None


def scheduler_loops(test_dir):
    path = _first(test_dir, "*.output.bl2.gz", "*.output.bl2")
    if not path:
        return {}
    out = {}
    key = None
    with _open(path) as fh:
        for index, line in enumerate(fh):
            if index > 150:
                break
            match = re.search(r"\b(LoopingCount|LoopingEnable|LoopingType|NumThreads)\s*:=\s*CHOICE", line)
            if match:
                key = match.group(1)
            elif key:
                value = re.search(r'\bvalue\s*:=\s*"([^"]+)"', line)
                if value:
                    out[key] = _num(value.group(1)) if value.group(1).isdigit() else value.group(1)
                    key = None
    return out


def prefetch_rate(opcodes, prefetch, demand):
    pref = opcodes.get(prefetch)
    base = opcodes.get(demand)
    if pref is None or base is None or pref + base <= 0:
        return None
    return pref / (pref + base)


def xlsx_stats(test_dir):
    path = _first(test_dir, "*.xlsx", "*.xlsx.gz")
    if not path:
        return None
    rows = _read_xlsx_first_sheet(path)
    out = {"file": os.path.basename(path), "idi_opcodes": {}, "instructions": None,
           "workload_loops": {}}
    hdr_row = None
    for r in sorted(rows):
        cells = rows[r]
        b = (cells.get("B") or "").strip()
        if b.startswith("Total Opcodes Seen @ NVL-IDI"):
            hdr_row = r
        elif hdr_row and cells.get("C") == "SUMMARY" and not out["idi_opcodes"]:
            hdr = rows[hdr_row]
            for col, label in hdr.items():
                if _col_idx(col) > _col_idx("C") and col in cells:
                    out["idi_opcodes"][label] = _num(cells[col])
        elif b.startswith("Number of instructions"):
            out["instructions"] = _num(cells.get("C"))
        elif b in ("Loops", "Lsz:"):
            raw = cells.get("C")
            out["workload_loops"][b.rstrip(":")] = _int0(raw) if raw else None
        elif b.startswith("L1 and L2 cache") and r + 1 in rows:
            nxt = rows[r + 1]
            out["l1_mb"], out["l2_mb"], out["llc_mb_xlsx"] = (_num(nxt.get(c)) for c in "CDE")
        elif b.startswith("Threads:"):
            out["threads"] = _int0(cells.get("C"))
        elif b.startswith("Cores:"):
            out["cores_mask"] = (cells.get("C") or "").strip()
        g = (cells.get("G") or "").strip()
        if g in ("HowManyMoves", "HowManyMovesPerStride", "OutterLoop"):
            out["workload_loops"][g] = _num(cells.get("I"))
        if g.startswith("Traffic Weight"):
            out["traffic_weight"] = _num(cells.get("I"))
        elif g.startswith("BufferSize"):
            out["buffer_bytes"] = _num(cells.get("I"))
        elif g.startswith("Total Expected @ IDI"):
            out["expected_instructions"] = _num(cells.get("I"))
    op = out["idi_opcodes"]
    out["rfo_pref_rate"] = prefetch_rate(op, "C2U_RFO_PREF", "C2U_RFO")
    out["drd_pref_rate"] = prefetch_rate(op, "C2U_DRD_OPT_PREF", "C2U_DRD_OPT")
    if op:
        up = {k.upper(): (v or 0) for k, v in op.items()}
        rd = sum(v for k, v in up.items() if k.startswith("C2U_") and ("RD" in k or "PREF" in k))
        wr = sum(v for k, v in up.items() if k.startswith(
            ("C2U_WB", "C2U_WIL", "C2U_WCIL", "C2U_ITOM", "C2U_UCWR", "C2U_WOWR", "C2U_NTWR", "C2U_MEMPUSHWR")))
        out["idi_read"], out["idi_write"], out["idi_total"] = rd, wr, rd + wr
        out["l2_miss"] = sum(up.get(k, 0) for k in ("C2U_DRD_OPT", "C2U_DRD_OPT_PREF", "C2U_CRD", "C2U_PRD", "C2U_RFO", "C2U_RFO_PREF"))
        out["l2_evict"] = sum(v for k, v in up.items() if k.startswith("C2U_WB"))
    return out


MRC_REG = re.compile(r"creg_name: '((?:TC_|MC_CHANNEL_CFG|MAD_)[A-Z0-9_\[\]]*)', reg_file: ([\w.]+), value: (0x[0-9a-fA-F]+)")


def mrc_stats(test_dir):
    path = _first(test_dir, "minibios_files/mrc_*/MRC.yaml.gz")
    if not path:
        return None
    regs = {}
    with _open(path) as fh:
        for line in fh:
            m = MRC_REG.search(line)
            if m:
                name = m.group(1).replace("_0_0_0_MCHBAR", "")
                regs.setdefault(name, {}).setdefault(m.group(3), []).append(m.group(2))
    xtor = None
    xt = _first(test_dir, "minibios_files/mrc_*/xtor_config.yaml.gz")
    if xt:
        with _open(xt) as fh:
            xtor = dict(re.findall(r"^(\w+):\s*(\S+)", fh.read(), re.M))
    return {"file": os.path.relpath(path, test_dir), "registers": regs, "xtor_config": xtor}


def deep(test_dir):
    out = {}
    with cf.ThreadPoolExecutor(4) as ex:
        futs = {k: ex.submit(fn, test_dir) for k, fn in
                (("cbo", cbo_stats), ("santa", santa_stats), ("xlsx", xlsx_stats), ("mrc", mrc_stats))}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception as e:  # keep one broken file from hiding the rest
                out[k] = {"error": f"{type(e).__name__}: {e}"}
    return out


def cached_summary(test_dir):
    return _cached(test_dir, "summary", summary)


def cached_deep(test_dir):
    return _cached(test_dir, "deep", deep)


# --------------------------------------------------------------------------- flat row for CSV

CSV_FIELDS = [
    "name", "status", "exitstatus", "product", "dut", "cth_model", "user", "date", "host", "cycles", "ticks",
    "instrs_retired", "instrs_expected", "dram_bw_mean", "dram_bw_max", "dram_rd_bw_mean", "dram_wr_bw_mean",
    "l3_hit", "l3_miss", "l3_hit_rate",
    "santa_read_lat_mean_ns", "santa_read_lat_min_ns", "santa_read_bw_mbs", "santa_total_bw_mbs",
    "idi_read", "idi_write", "idi_total", "l2_miss", "l2_evict", "dpt_ev",
    "rfo_pref_rate", "drd_pref_rate",
    "cclk_mhz", "nclk_mhz", "iatom_mhz", "ddr", "msc", "sagv", "cstate", "llc_mb", "dies_cores", "mem_cfg",
    "simulator_name", "emurun_version", "spark_version", "simics_version", "zebu_version", "test_dir",
]


def flat_row(s, d=None):
    d = d or {}
    cfg = s.get("config", {})
    x = d.get("xlsx") or {}
    c = d.get("cbo") or {}
    sa = d.get("santa") or {}
    row = {k: s.get(k) for k in CSV_FIELDS if k in s}
    row.update({k: cfg.get(k) for k in ("cclk_mhz", "nclk_mhz", "iatom_mhz", "ddr", "msc", "sagv", "cstate", "llc_mb", "dies_cores")})
    row.update({
        "mem_cfg": ";".join(s.get("mem_cfg") or []),
        "dram_bw_mean": s["dram"].get("dram_bw_mean"), "dram_bw_max": s["dram"].get("dram_bw_max"),
        "dram_rd_bw_mean": s["dram"].get("rd_mean"), "dram_wr_bw_mean": s["dram"].get("wr_mean"),
        "instrs_retired": x.get("instructions"), "instrs_expected": x.get("expected_instructions"),
        "idi_read": x.get("idi_read"), "idi_write": x.get("idi_write"),
        "idi_total": x.get("idi_total"), "l2_miss": x.get("l2_miss"), "l2_evict": x.get("l2_evict"),
        "rfo_pref_rate": x.get("rfo_pref_rate"), "drd_pref_rate": x.get("drd_pref_rate"),
        "dpt_ev": (x.get("idi_opcodes") or {}).get("DPTEV"),
        "l3_hit": c.get("l3_hit"), "l3_miss": c.get("l3_miss"), "l3_hit_rate": c.get("l3_hit_rate"),
        "santa_read_lat_mean_ns": sa.get("read_lat_mean_ns"), "santa_read_lat_min_ns": sa.get("read_lat_min_ns"),
        "santa_read_bw_mbs": sa.get("read_bw_mbs"), "santa_total_bw_mbs": sa.get("total_bw_mbs"),
    })
    for ch, v in sorted((s["dram"].get("channels") or {}).items()):
        row[f"dram_bw_{ch.lower()}"] = v.get("total_mean")
    row["fail_reason"] = " | ".join(s.get("fail_reasons") or [])[:500]
    return row


if __name__ == "__main__":
    import argparse
    import csv
    import sys

    ap = argparse.ArgumentParser(description="Extract run-area stats to CSV/JSON")
    ap.add_argument("run_area")
    ap.add_argument("--deep", action="store_true", help="also parse trackers/xlsx (slow, cached)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of CSV")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    a = ap.parse_args()
    tests = find_test_dirs(a.run_area)
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        sums = list(ex.map(cached_summary, tests))
        deeps = list(ex.map(cached_deep, tests)) if a.deep else [None] * len(tests)
    if a.json:
        json.dump([{"summary": s, "deep": d} for s, d in zip(sums, deeps)], sys.stdout, indent=1)
    else:
        rows = [flat_row(s, d) for s, d in zip(sums, deeps)]
        fields = list(dict.fromkeys(CSV_FIELDS + [k for r in rows for k in r]))
        w = csv.DictWriter(sys.stdout, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
