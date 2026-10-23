# SIA-PTP-CLIENT-AUTOMATION
This serves as tool for debug and dashboard excel generation for all the domains under the team

## runDashboard

Web dashboard for emulation regression run areas (RZL BLLC/MLC style `simregress` lists).
Paste a run-area path to get an all-runs overview and sortable summary table (status, configuration,
bandwidth, cache, latency, instruction and runtime columns). Click a row or select a run, then open
**Bandwidth**, **Charts**, **Loops**, **Cache / IDI**, or **Configuration** when you need that level of detail.
Each selected run's **Overview** displays a conceptual workload-to-DRAM block diagram
annotated with measurements where available, its entered/exited logbook stages, and
register families referenced by its local `.inc` memory recipe. Diagram arrows are
architectural context, not traced individual requests. The recipe is common to these
runs; the dashboard does not claim that any individual setting improved performance
without a controlled same-workload before/after comparison.
Deep-analysis columns populate automatically in the background (at most two tests at a time).
The table distinguishes **Loading…**, **No source**, **Not recorded**, and **Error**;
**Expected instr** is labelled separately from measured **Instructions** rather than substituted for it.
RFO Pref rate = `C2U_RFO_PREF / (C2U_RFO_PREF + C2U_RFO)` and DRd Pref rate =
`C2U_DRD_OPT_PREF / (C2U_DRD_OPT_PREF + C2U_DRD_OPT)` from the workbook opcode
summary. The UI shows percentages; CSV `rfo_pref_rate` and `drd_pref_rate` are
raw fractions (0–1). Missing counts or zero totals leave the rate unavailable.
Per-test opcode summaries are read from `.xlsx` or compressed `.xlsx.gz` workbooks.
An absent workbook is distinct from an absent opcode within an existing workbook.
Use **Retry missing metrics** if an analysis request fails.
The Export CSV action requests all available deep metrics, including both recorded and expected
instructions; missing source files remain blank rather than being estimated. Positive bandwidth
below 0.05 GB/s displays as `<0.1` in the UI, with its exact MB/s value on hover.
The "Sum peaks" figure adds separately measured read/write maxima and is **not** a simultaneous peak.
Emulation duration in the UI uses `days:hours:minutes:seconds:milliseconds`
(for example, 1112.8 seconds is `0:00:18:32:800`). CSV retains raw seconds for calculations.
The **Compare runs** button shows cross-run charts; tracker-based cache analysis loads only on request.
The **Charts** tab can reveal every original compressed Chainsaw PNG, grouped by source folder, with
expandable counts, bandwidth, and latency metrics. No graphs are fetched until requested.
The **Bandwidth** tab also loads all available Chainsaw overall mean/max and per-group
bandwidth summaries on demand, including IDI and CFI sources. These are separate
interfaces, shown in GB/s from source MB/s; do not add their values together.
Pure Python 3 standard library, no CDN, no installs.

## Run

```bash
# on any Linux host that can see /nfs/site/disks
/usr/intel/bin/python3 server.py --port 8765                    # localhost only
/usr/intel/bin/python3 server.py --host 0.0.0.0 --port 8765     # share with the team: http://<host>:8765/
/usr/intel/bin/python3 server.py --allow-root /nfs/site/disks/csa_pnp_bdc_003 --allow-root /nfs/site/disks/other
```

From a Windows laptop with localhost-only mode: `ssh -N -L 8765:127.0.0.1:8765 <user>@<host>` then open
<http://localhost:8765/>. Dashboards are shareable as `http://<host>:8765/?path=<run area>`.

CLI (no server):

```bash
python3 extract.py <run_area>                 # summary table
python3 extract.py <run_area> --deep -j 2     # + trackers/xlsx/MRC
python3 extract.py <run_area> --json > out.json
```

## API

| Endpoint | Returns |
|---|---|
| `/api/scan?path=<dir>` | summary for every test dir under `<dir>` (fast, < 1 s) |
| `/api/deep?path=<test_dir>` | cbo / santa / xlsx / MRC stats for one test (~15–60 s first time, cached) |
| `/api/flow?path=<test_dir>` | logbook stage transitions and applied local `.inc` register families |
| `/api/csv?path=<dir>[&deep=1]` | flat CSV of all tests |
| `/api/config` | allowed roots, cache dir |
| `/api/chainsaw?path=<test_dir>` | plot inventory and associated metric summaries |
| `/api/chainsaw/image?path=<test_dir>&file=<relative_png.gz>` | original compressed PNG served as an image |

Paths are restricted to `--allow-root` prefixes (default `/nfs/site/disks`). Results are cached in
`~/.cache/runDashboard` (override with `RUNDASH_CACHE`), keyed by path + rpt mtime, so re-runs refresh automatically.

## Data sources

| Field | File (per test dir) |
|---|---|
| status, failure reasons, model, DUT, cycles, host, date | `<test>.rpt.gz` (all `TEST RESULT:` lines), `report.json.gz` |
| tool versions, efficiency score, qslot | `tb_perf_summary.log.gz` |
| knobs / command line, exit status | `.end_trex.env.gz` → `TEST_CMDLINE`, `MODEL_EXIT_STATUS` |
| frequencies, DDR, LLC size, dies/cores | parsed from the test name + `*fuse_ovrd` RATIO knobs |
| DRAM rd/wr BW (overall, per channel, per rank) | `output.overall_bw_stats.txt.gz`, `output.bw_stats_bygrp.txt.gz` (rank_core_id = MC,CH,RANK) |
| L3 hit / miss | `trackers_cdie0/cbo_tracker_*.log.gz` (`un_cbo_llc_hit` / `un_cbo_llc_miss`) |
| CFI read latency + BW | `cfi_jem_trackers/cdie0_santa*.log.gz`: `T-REQ Rd*` → first `R-DAT MemData` with same RTID |
| instructions, IDI opcodes, cache sizes, threads | `<test>.xlsx` (sheet 1, `SUMMARY` row) |
| DRAM timing / address-map registers, xtor latencies | `minibios_files/mrc_*/MRC.yaml.gz` |
| workload loops, outer loop, moves, stride and line size | `<test>.xlsx` (first sheet) |
| scheduler looping count / enable / type | `*.output.bl2.gz`, `DefaultConcurrencySchedulerParams` |
| original plots and their summary metrics | `chainsaw_*`, `ddr_latency_*`, and root `*.png.gz` and adjacent `output.counts.txt.gz`, `output.*_stats.txt.gz` |

## Caveats

- DRAM timings are shown as raw `TC_*` register values (not decoded to tCL/tRCD/...).
- `idi_read` / `idi_write` / `l2_miss` / `l2_evict` are derived from C2U opcode names (heuristic).
- Santa BW assumes 64 B per read; it measures CFI traffic, so it does not equal DRAM BW.
- xlsx BW cells are formulas without cached values and are not read; some tests have no xlsx.
- Knob diff ignores per-test `seed`, `rseed`, `predicted_runtime`, `list_line_num`.
- The Chainsaw gallery shows generated plots unchanged; it does not reinterpret their axes or
  combine data from different units. Only summary statistics are read, not large raw sample streams.
- Workload loop counts require the per-test workbook; they are not inferred from scheduler looping
  settings, which describe a separate mechanism.
