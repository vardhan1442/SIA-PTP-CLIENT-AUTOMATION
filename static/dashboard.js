"use strict";
const $=s=>document.querySelector(s);
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=v=>v==null||v===""||Number.isNaN(Number(v))?"—":Number(v).toLocaleString(undefined,{maximumFractionDigits:2});
const percent=v=>v==null?"—":(v*100).toFixed(2)+"%";
const gb=v=>v==null?"—":v>0&&v<50?"<0.1 GB/s":(v/1000).toFixed(1)+" GB/s";
const gbCell=v=>v>0&&v<50?"<0.1":(v/1000).toFixed(1);
function duration(seconds){
 if(seconds==null||seconds===""||!Number.isFinite(Number(seconds))||Number(seconds)<0)return "—";
 let ms=Math.round(Number(seconds)*1000);
 const millis=ms%1000;ms=Math.floor(ms/1000);
 const secs=ms%60;ms=Math.floor(ms/60);
 const mins=ms%60;ms=Math.floor(ms/60);
 const hours=ms%24,days=Math.floor(ms/24);
 return `${days}:${String(hours).padStart(2,"0")}:${String(mins).padStart(2,"0")}:${String(secs).padStart(2,"0")}:${String(millis).padStart(3,"0")}`;
}
let state={root:null,tests:[],selected:null,tab:"Overview",deep:{},flow:{},gallery:{},galleryOpen:false,scanId:0,view:"run",sort:"name",reverse:false,browseOpen:false,browsePath:null,browseCache:{},allowedRoots:[],domain:window.DASHBOARD_DOMAIN||""};
function name(t){return t.name.replace(/^do_/,"").replace(/_72MB_.*$/,"")}
function url(route,params){return route+"?"+new URLSearchParams(params)}
async function json(route,params){const r=await fetch(url(route,params));if(r.status===401){location.href="/login.html";throw Error("not authenticated")}const data=await r.json();if(!r.ok)throw Error(data.error||`HTTP ${r.status}`);return data}
function metrics(items){return `<div class="metrics">${items.map(([label,value])=>`<div><small>${esc(label)}</small><strong>${esc(value)}</strong></div>`).join("")}</div>`}
function kv(data){return `<div class="kv">${Object.entries(data).map(([key,value])=>`<span>${esc(key)}</span><span>${esc(value??"—")}</span>`).join("")}</div>`}
function table(rows){return `<div class="scroll"><table><tbody>${rows.map(([a,b])=>`<tr><td>${esc(a)}</td><td>${esc(b??"—")}</td></tr>`).join("")}</tbody></table></div>`}
function errorText(error){return `<div class="error">${esc(error)}</div>`}
async function scan(){
 const path=$("#path").value.trim();if(!path)return;
 const token=++state.scanId;$("#scan").disabled=true;$("#main").innerHTML='<div class="panel">Scanning run area…</div>';
 try{const result=await json("/api/scan",{path});if(token!==state.scanId)return;
  state={...state,root:result.root,tests:result.tests,selected:result.tests[0]?.test_dir||null,tab:"Overview",view:"run",deep:{},flow:{},gallery:{},galleryOpen:false,browseOpen:false,browsePath:result.root,browseCache:{}};
  history.replaceState(null,"","?path="+encodeURIComponent(path));
  $("#location").textContent=result.root;$("#csv").disabled=!result.count;render();loadFlow(state.selected);loadAllDeep();
 }catch(error){if(token===state.scanId)$("#main").innerHTML=errorText(error.message)}
 finally{if(token===state.scanId)$("#scan").disabled=false}
}
function select(path){state.selected=path;state.tab="Overview";state.view="run";state.galleryOpen=false;render();loadFlow(path)}
function switchTab(tab){state.tab=tab;state.galleryOpen=false;render();if(["Charts","Loops","Cache / IDI"].includes(tab))loadDeep(state.selected);if(tab==="Bandwidth")loadBandwidthGallery(state.selected)}
function compare(){state.view="compare";render()}
function current(){return state.tests.find(t=>t.test_dir===state.selected)}
const COLUMNS=[["name","Run"],["status","Status"],["exit","Exit"],["config","Cores"],["llc","LLC MB"],
 ["cclk","CCLK MHz"],["nclk","NCLK MHz"],["ddr","DDR"],["read","DDR: Rd BW Avg (GB/s)"],["write","DDR: Wr BW Avg (GB/s)"],
 ["total","DDR: Total BW Avg (GB/s)"],["peak","DDR: Peak BW (GB/s)"],["hit","L3 hit %"],["latency","Read ns"],["p99","p99 ns"],
 ["instructions","Instructions"],["expected","Expected instr"],["idi","IDI"],["miss","L2 miss"],
 ["rfoPref","RFO pref %"],["drdPref","DRd pref %"],["cycles","Cycles"],["runtime","Emu d:hh:mm:ss:ms"]];
const DEEP_SOURCE={hit:"cbo",latency:"santa",p99:"santa",instructions:"xlsx",expected:"xlsx",idi:"xlsx",miss:"xlsx",rfoPref:"xlsx",drdPref:"xlsx"};
function summaryRow(t){
 const d=state.deep[t.test_dir],x=d?.xlsx||{},s=d?.santa||{},c=d?.cbo||{},m=t.dram||{},cfg=t.config||{};
 return {name:name(t),status:t.status,exit:t.exitstatus,config:cfg.dies_cores,llc:cfg.llc_mb,
  cclk:cfg.cclk_mhz,nclk:cfg.nclk_mhz,ddr:cfg.ddr,read:m.rd_mean,write:m.wr_mean,total:m.dram_bw_mean,
  peak:m.dram_bw_max,hit:c.l3_hit_rate,latency:s.read_lat_mean_ns,p99:s.read_lat_p99_ns,
  instructions:x.instructions,expected:x.expected_instructions,idi:x.idi_total,miss:x.l2_miss,
  rfoPref:x.rfo_pref_rate,drdPref:x.drd_pref_rate,cycles:t.cycles,runtime:t.emu_runtime_s};
}
function summaryCell(t,key,v){
 const source=DEEP_SOURCE[key],deep=state.deep[t.test_dir];
 if(source&&v==null){
  const label={xlsx:"workbook",cbo:"CBO tracker",santa:"santa tracker"}[source];
  if(!t.files?.[source])return `<td class="unavailable" title="No ${esc(label)} available for this run">No source</td>`;
  if(deep?.error||deep?.[source]?.error)return `<td class="failed" title="${esc(deep.error||deep[source].error)}">Error</td>`;
  if(deep==="queued"||deep==="loading"||!deep)return `<td class="pending" title="${esc(label)} analysis pending">Loading…</td>`;
  const reason=["rfoPref","drdPref"].includes(key)?"Both opcode counts are required and their sum must be positive":`No ${COLUMNS.find(([k])=>k===key)?.[1]} value recorded in ${label}`;
  return `<td class="unavailable" title="${esc(reason)}">Not recorded</td>`;
 }
 if(key==="status")return `<td><span class="pill ${esc(v)}">${esc(v)}</span></td>`;
 if(["name","config","ddr"].includes(key))return `<td>${esc(v??"—")}</td>`;
 if(["read","write","total","peak"].includes(key)){
  if(v==null)return `<td class="unavailable" title="No ${esc(key)} bandwidth recorded">N/A</td>`;
  const note=key==="peak"?"Sum of separate read and write maxima; not a simultaneous peak":"Bandwidth";
  return `<td title="${note}: ${fmt(v)} MB/s">${gbCell(v)}</td>`;
 }
 if(key==="runtime")return `<td title="Days:hours:minutes:seconds:milliseconds">${duration(v)}</td>`;
 if(key==="rfoPref"||key==="drdPref")return `<td>${percent(v)}</td>`;
 return `<td>${esc(fmt(v))}</td>`;
}
function summaryTable(){
 const rows=state.tests.map(t=>({test:t,values:summaryRow(t)})),key=state.sort;
 rows.sort((a,b)=>{const x=a.values[key],y=b.values[key];if(x==null)return y==null?0:1;if(y==null)return -1;
  const cmp=typeof x==="number"&&typeof y==="number"?x-y:String(x).localeCompare(String(y));
  return state.reverse?-cmp:cmp});
 return `<div class="overview-table"><table><thead><tr>${COLUMNS.map(([k,label])=>`<th data-sort="${k}" title="Sort by ${esc(label)}">${esc(label)} ${state.sort===k?state.reverse?"▼":"▲":""}</th>`).join("")}</tr></thead>
 <tbody>${rows.map(({test,values})=>`<tr class="${state.view==="run"&&state.selected===test.test_dir?"selected":""}" data-summary-run="${esc(test.test_dir)}">${COLUMNS.map(([k])=>summaryCell(test,k,values[k])).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function bindSummary(){
 document.querySelectorAll("[data-summary-run]").forEach(row=>row.onclick=()=>{select(row.dataset.summaryRun);$(".detail").scrollIntoView({behavior:"smooth",block:"start"})});
 document.querySelectorAll("[data-sort]").forEach(th=>th.onclick=()=>{state.reverse=state.sort===th.dataset.sort?!state.reverse:false;state.sort=th.dataset.sort;refreshSummary()});
}
function refreshSummary(){
 const existing=$(".overview-table");if(!existing)return;
 const left=existing.scrollLeft,top=existing.scrollTop;
 existing.outerHTML=summaryTable();
 $(".overview-table").scrollLeft=left;$(".overview-table").scrollTop=top;
 bindSummary();
 const remaining=state.tests.filter(t=>state.deep[t.test_dir]==="loading"||state.deep[t.test_dir]==="queued").length;
 const progress=$("#summaryProgress");if(progress)progress.textContent=remaining?`${remaining} runs analysing in background`:"Analysis complete; unavailable cells are labelled by source";
}
function render(){
 const tests=state.tests,pass=tests.filter(t=>t.status==="PASS").length,fail=tests.filter(t=>t.status==="FAIL").length;
 $("#main").innerHTML=`<div class="muted">${tests.length} runs in this area</div>
 ${state.browseOpen?`<section class="panel" style="margin-bottom:16px" id="browsePanel">${browseView()}</section>`:""}
 <div class="summary">${[["Runs",tests.length],["Passed",pass],["Failed",fail],["Other",tests.length-pass-fail]].map(([k,v])=>`<div class="tile"><span class="muted">${k}</span><b>${v}</b></div>`).join("")}</div>
 <section class="panel" style="margin-bottom:16px"><div class="detail-head"><div><h2>All-runs summary</h2><div class="muted">Click a row to inspect its run. Scroll right for detailed columns. Cache and latency come from trackers; instruction and IDI counts come from per-test workbooks.</div><div id="summaryProgress" class="muted" role="status">Analysing available sources in the background</div></div><button id="summaryDeep">Retry missing metrics</button></div>${summaryTable()}</section>
 <div class="layout"><nav class="runs" aria-label="Runs"><h2>All runs</h2>${tests.map(t=>`<button class="run ${state.view==="run"&&state.selected===t.test_dir?"selected":""}" data-run="${esc(t.test_dir)}"><span class="name">${esc(name(t))}</span><small><span class="pill ${esc(t.status)}">${esc(t.status)}</span> ${esc(t.config?.dies_cores||"")} · ${esc(t.config?.ddr||"")}</small></button>`).join("")}</nav><div class="detail">${state.view==="compare"?compareView():detailView()}</div></div>`;
 document.querySelectorAll("[data-run]").forEach(b=>b.onclick=()=>select(b.dataset.run));
 bindSummary();refreshSummary();
 $("#summaryDeep").onclick=()=>loadAllDeep(true);
 document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
 const cmp=$("#compare");if(cmp)cmp.onclick=compare;
 const back=$("#back");if(back)back.onclick=()=>{state.view="run";render()};
 const orig=$("#original");if(orig)orig.onclick=()=>{state.galleryOpen=!state.galleryOpen;render();if(state.galleryOpen)loadGallery(state.selected)};
 bindBrowse();
 renderCharts();
}
function byteSize(n){
 if(n==null)return "—";
 const units=["B","KB","MB","GB","TB"];let i=0,v=n;
 while(v>=1024&&i<units.length-1){v/=1024;i++}
 return (i===0?v:v.toFixed(1))+" "+units[i];
}
function toggleBrowse(){
 state.browseOpen=!state.browseOpen;
 if(state.browseOpen&&!state.browsePath)state.browsePath=state.root||$("#path").value.trim()||state.allowedRoots[0]||"/nfs/site/disks";
 render();
 if(state.browseOpen)loadBrowse(state.browsePath);
}
function useBrowsePath(){
 if(!state.browsePath)return;
 $("#path").value=state.browsePath;
 state.browseOpen=false;
 scan();
}
function browseView(){
 const path=state.browsePath,entries=state.browseCache[path];
 const segs=(path||"").split("/").filter(Boolean);
 let acc="";
 const crumbs=[`<button data-browse-crumb="/">/</button>`]
  .concat(segs.map(seg=>{acc=acc+"/"+seg;return `<span>/</span><button data-browse-crumb="${esc(acc)}">${esc(seg)}</button>`}));
 const parent=entries&&entries!=="loading"&&!entries.error?entries.parent:null;
 const body=entries==="loading"?'<div class="notice">Loading directory…</div>'
  :entries?.error?errorText(entries.error)
  :entries?`<div class="scroll"><table class="browse-table"><tbody>${entries.entries.length?entries.entries.map(e=>{
    const full=path.replace(/\/$/,"")+"/"+e.name;
    return `<tr><td>${e.is_dir?`📁 <button class="entry" data-browse-dir="${esc(full)}">${esc(e.name)}</button>`:`📄 <button class="entry" data-browse-file="${esc(full)}">${esc(e.name)}</button>`}</td><td class="size">${e.is_dir?"—":byteSize(e.size)}</td><td class="mtime">${e.mtime?new Date(e.mtime*1000).toLocaleString():"—"}</td></tr>`
   }).join(""):'<tr><td colspan="3" class="muted">Empty directory</td></tr>'}</tbody></table></div>`
  :'<div class="notice">Loading directory…</div>';
 return `<div class="detail-head"><h3>Browse files</h3><div><button id="browseBack" ${parent?"":"disabled"} title="Go to parent folder">&larr; Back</button> <button id="browseUse" title="Load this folder as the run area">Open this folder as run area</button> <button id="browseClose">Close</button></div></div><div class="breadcrumb">${crumbs.join("")}</div>${body}`;
}
function bindBrowse(){
 document.querySelectorAll("[data-browse-crumb]").forEach(b=>b.onclick=()=>loadBrowse(b.dataset.browseCrumb));
 document.querySelectorAll("[data-browse-dir]").forEach(b=>b.onclick=()=>loadBrowse(b.dataset.browseDir));
 document.querySelectorAll("[data-browse-file]").forEach(b=>b.onclick=()=>window.open(url("/api/file",{path:b.dataset.browseFile}),"_blank"));
 const close=$("#browseClose");if(close)close.onclick=()=>{state.browseOpen=false;render()};
 const use=$("#browseUse");if(use)use.onclick=useBrowsePath;
 const back=$("#browseBack");
 if(back&&!back.disabled){
  const entries=state.browseCache[state.browsePath];
  const parent=entries&&entries!=="loading"&&!entries.error?entries.parent:null;
  if(parent)back.onclick=()=>loadBrowse(parent);
 }
}
async function loadBrowse(path){
 state.browsePath=path;state.browseCache[path]="loading";
 if($("#browsePanel"))$("#browsePanel").innerHTML=browseView();bindBrowse();
 try{const data=await json("/api/browse",{path});state.browseCache[path]=data}
 catch(error){state.browseCache[path]={error:error.message}}
 if(state.browseOpen&&state.browsePath===path&&$("#browsePanel")){$("#browsePanel").innerHTML=browseView();bindBrowse()}
}
function detailView(){
 const t=current();if(!t)return '<div class="notice">No tests found here.</div>';
 const tabs=["Overview","Bandwidth","Charts","Loops","Cache / IDI","Configuration"];
 return `<div class="detail-head"><div><h2>${esc(name(t))} <span class="pill ${esc(t.status)}">${esc(t.status)}</span></h2><div class="muted">${esc(t.name)}</div></div><button id="compare">Compare runs</button></div>
 <div class="tabs" role="tablist">${tabs.map(tab=>`<button role="tab" aria-selected="${state.tab===tab}" class="${state.tab===tab?"active":""}" data-tab="${tab}">${esc(tab)}</button>`).join("")}</div><div class="section" id="content">${tabView(t)}</div>`;
}
const REGISTER_EFFECT={
 RH_TRR_CONTROL:"Row-hammer / refresh controls; effect on bandwidth not isolated",
 PM_PDWN_CONFIG:"Power-down settings; possible wake-up latency tradeoff",
 DFI_LOW_POWER_CTL:"DFI low-power settings; possible wake-up latency tradeoff",
 PM_CONFIG_THERM_CONTROL:"Thermal MR4 interval; possible refresh overhead",
 MC_RDB_CREDITS:"Read buffer credits; possible read concurrency",
 SCHED_CBIT:"Scheduler / opportunistic refresh control",
 MC_INIT_STATE_G:"Refresh enable; verify safety before changing",
 MC_CFI_FABRIC_CONTROL:"CFI transmit / SAGV blocking control"
};
const STAGE_DESCRIPTION={
 "Init":"Initialize test harness",
 "Command line parsing":"Resolve run options",
 "Create test's work area & preprocessing":"Set up artifacts and workload",
 "Test build":"Prepare test image",
 "Model run":"Execute emulation workload",
 "Creating RPT":"Create result report",
 "Create FAIL RPT":"Create failure report",
 "Post processing":"Collect tracker results",
 "Fail Post processing":"Process failure artifacts",
 "End of run":"Finalize run",
 "Final end of run, Copy back":"Copy back artifacts"
};
function flowView(t){
 const deep=state.deep[t.test_dir],x=deep?.xlsx,c=deep?.cbo,s=deep?.santa;
 const traffic=t.config?.traffic||state.domain;
 const path=[
  ["Workload",`${traffic} · ${t.config?.dies_cores||"core config unavailable"}`],
  ["Core / L1 / L2",x?.idi_total!=null?`${fmt(x.idi_total)} derived IDI events`:"IDI counts not recorded"],
  ["IDI / LLC",c?.l3_hit_rate!=null?`${fmt(c.l3_hit_rate)}% L3 hit`:"L3 hit not recorded"],
  ["CFI / fabric",s?.read_lat_mean_ns!=null?`${fmt(s.read_lat_mean_ns)} ns read latency`:"Latency not recorded"],
  ["Memory controller",`${t.config?.ddr||"DDR unknown"} · ${t.dram?.channels&&Object.keys(t.dram.channels).length?`${Object.keys(t.dram.channels).length} measured channels`:"channels not recorded"}`],
  ["DRAM",t.dram?.dram_bw_mean!=null?gb(t.dram.dram_bw_mean)+" mean":"Bandwidth not recorded"]
 ];
 return `<h3>Workload path across this SKU</h3><p class="muted">Conceptual request / response path, not a cycle-accurate trace. Each annotation comes from this run's workbook, trackers or bandwidth summary; missing measurements are not inferred.</p>
 <div class="flow" role="img" aria-label="${esc(path.map(([label,value])=>`${label}: ${value}`).join(" to "))}">${path.map(([label,value],i)=>`${i?'<span class="flow-arrow" aria-hidden="true">→</span>':""}<div class="flow-node"><b>${esc(label)}</b><small>${esc(value)}</small></div>`).join("")}</div>
 ${t.status!=="PASS"?'<div class="notice">This run failed. The path describes intended traffic; unavailable measurements do not establish that every block carried traffic.</div>':""}`;
}
function flowEvidenceView(t){
 const flow=state.flow[t.test_dir],stages=flow?.stages||[],includes=flow?.includes||[];
 return `<h3>Run stages</h3>${flow?.error?errorText(flow.error):flow==="loading"?'<p class="muted">Reading stage transitions from the logbook…</p>':stages.length?`<p class="muted">Stages entered in ${esc(flow.stage_source)}. “Completed” means a leaving marker was found, not that the stage passed.</p><ol class="stage-list">${stages.map(st=>`<li class="${st.completed?"":"interrupted"}"><b>${esc(st.name)}</b><small>${esc(STAGE_DESCRIPTION[st.name]||"Run-specific stage")} · ${st.completed?"Exited stage":"No exit marker"}</small></li>`).join("")}</ol>`:'<div class="notice">No stage transitions found in this run.</div>'}
 <h3>Included memory recipe and performance context</h3>${flow?.error?"":flow==="loading"?'<p class="muted">Checking applied .inc references…</p>':includes.length?includes.map(inc=>`<details open><summary>${esc(inc.path.split("/").pop())} · ${inc.registers.length} register families</summary><div class="path">${esc(inc.path)}</div>${inc.note?`<div class="notice">${esc(inc.note)}</div>`:inc.registers.length?table(inc.registers.map(reg=>[reg,REGISTER_EFFECT[reg]||"Configured; effect not established"])):'<div class="notice">No recognizable register blocks found.</div>'}</details>`).join(""):'<div class="notice">No applied .inc path recorded in this test command.</div>'}
 <p class="muted">These are configured mechanisms, not measured gains. This run area provides no controlled same-workload baseline with the recipe disabled, so no individual .inc register can be credited with the observed bandwidth or latency.</p>`;
}
async function loadFlow(path){
 if(!path||state.flow[path])return;
 const token=state.scanId;
 state.flow[path]="loading";renderFlow();
 try{
  const data=await json("/api/flow",{path});
  if(token===state.scanId)state.flow[path]=data;
 }catch(error){
  if(token===state.scanId)state.flow[path]={error:error.message};
 }
 if(token===state.scanId&&state.selected===path)renderFlow();
}
function renderFlow(){
 if(state.view==="run"&&state.tab==="Overview"&&current()){
  if($("#flowContent"))$("#flowContent").innerHTML=flowView(current());
  if($("#flowEvidence"))$("#flowEvidence").innerHTML=flowEvidenceView(current());
 }
}
function deepStatus(t){const d=state.deep[t.test_dir];return d==="loading"||d==="queued"?'<div class="notice">Loading tracker and workbook analysis…</div>':d?.error?errorText(d.error):d||null}
function tabView(t){
 const deep=deepStatus(t);
 if(state.tab==="Overview")return `<div id="flowContent">${flowView(t)}</div><h3>Run summary</h3>${metrics([["Cycles",fmt(t.cycles)],["Emulation time (d:hh:mm:ss:ms)",duration(t.emu_runtime_s)],["Exit code",fmt(t.exitstatus)]])}
  ${(t.fail_reasons||[]).length?`<div class="error">${esc(t.fail_reasons[0])}</div><details><summary>All failure messages (${t.fail_reasons.length})</summary>${t.fail_reasons.map(s=>errorText(s)).join("")}</details>`:""}
  <details><summary>Run identity and environment</summary>${kv({product:t.product,model:t.cth_model,host:t.host,user:t.user,date:t.date,tool:t.simulator_name})}</details>
  <div id="flowEvidence">${flowEvidenceView(t)}</div>
  <div class="notice">Select Bandwidth, Charts, or Loops above for detailed analysis.</div>`;
 if(state.tab==="Bandwidth"){
  const d=t.dram||{},channels=Object.entries(d.channels||{}),ranks=Object.entries(d.ranks||{});
  return `<h3>DRAM bandwidth</h3>${metrics([["Read mean",gb(d.rd_mean)],["Write mean",gb(d.wr_mean)],["Total mean",gb(d.dram_bw_mean)],["Sum of read/write peaks",gb(d.dram_bw_max)]])}
  <p class="muted">Read and write maxima may occur at different times; their sum is not a measured simultaneous peak.</p>
  <details><summary>Channels (${channels.length})</summary>${table(channels.map(([n,v])=>[n,`${gb(v.total_mean)} · read ${gb(v.rd_mean)} · write ${gb(v.wr_mean)}`]))}</details>
  <details><summary>Ranks (${ranks.length})</summary>${table(ranks.map(([n,v])=>[n,`read ${gb(v.rd_mean)} / write ${gb(v.wr_mean)}`]))}</details>
  ${d.dram_bw_mean==null?'<div class="notice">No bandwidth statistics for this run.</div>':""}
  <h3>Chainsaw bandwidth summaries</h3><p class="muted">Mean and maximum bandwidth as recorded by each Chainsaw source. Units are GB/s (1 GB/s = 1000 MB/s). Sources measure different interfaces and must not be added together.</p>
  <div id="bandwidthGallery">Loading Chainsaw summaries…</div>`;
 }
 if(state.tab==="Loops"){
  const sched=t.scheduler_loops||{},work=deep&&typeof deep==="object"?(deep.xlsx?.workload_loops||{}):{};
  return `<h3>Workload loops and iterations</h3>${deep?.xlsx?.error?errorText(deep.xlsx.error):Object.keys(work).length?kv(Object.fromEntries(Object.entries(work).map(([k,v])=>[k,fmt(v)]))):deep&&typeof deep==="object"?'<div class="notice">No workbook loop counts available for this run.</div>':deep||'<div class="notice">Loading loop counts from the workbook…</div>'}
  <details><summary>Scheduler looping settings</summary>${Object.keys(sched).length?kv(sched):'<div class="notice">No scheduler loop settings in this run.</div>'}</details>
  <div class="muted">Workload values come from the per-run xlsx; scheduler settings come from .output.bl2.gz. They describe different things.</div>`;
 }
 if(state.tab==="Cache / IDI"){
  const x=deep?.xlsx,c=deep?.cbo;
  return `<h3>Cache and IDI</h3>${deep&&typeof deep==="object"?metrics([["LLC hit rate",c?.l3_hit_rate==null?"—":c.l3_hit_rate+"%"],["LLC hits",fmt(c?.l3_hit)],["LLC misses",fmt(c?.l3_miss)],["Instructions",fmt(x?.instructions)]])+`<h3>Prefetch rates</h3>${metrics([["RFO Pref rate",percent(x?.rfo_pref_rate)],["DRd Pref rate",percent(x?.drd_pref_rate)]])}<p class="muted">RFO: C2U_RFO_PREF / (C2U_RFO_PREF + C2U_RFO). DRd: C2U_DRD_OPT_PREF / (C2U_DRD_OPT_PREF + C2U_DRD_OPT). Both counts and a nonzero total are required.</p>${c?.error?errorText(c.error):""}<details><summary>IDI opcodes</summary>${x?.error?errorText(x.error):x?.idi_opcodes?table(Object.entries(x.idi_opcodes).sort((a,b)=>b[1]-a[1]).map(([n,v])=>[n,fmt(v)])):'<div class="notice">No opcode workbook available.</div>'}</details>${c?.per_cbo?`<details><summary>Per-CBO hit and miss</summary>${table(Object.entries(c.per_cbo).map(([n,v])=>[`CBO ${n}`,`${fmt(v.hit)} hit · ${fmt(v.miss)} miss`]))}</details>`:""}`:deep||'<div class="notice">Loading cache and IDI data…</div>'}`;
 }
 if(state.tab==="Charts"){
  const sa=deep?.santa;
  return `<h3>Selected-run analysis</h3>${deep&&typeof deep==="object"?metrics([["LLC hit",deep.cbo?.l3_hit_rate==null?"—":deep.cbo.l3_hit_rate+"%"],["Read latency",sa?.read_lat_mean_ns==null?"—":sa.read_lat_mean_ns+" ns"],["p99",sa?.read_lat_p99_ns==null?"—":sa.read_lat_p99_ns+" ns"]]):deep||'<div class="notice">Loading tracker analysis…</div>'}
  <div class="grid"><div class="panel"><h3>Read latency distribution</h3><div id="hist"></div></div><div class="panel"><h3>IDI opcode mix</h3><div id="opcodes"></div></div></div>
  ${sa?.error?errorText(sa.error):""}${sa?.per_santa?`<details><summary>Per-santa CFI statistics</summary>${table(Object.entries(sa.per_santa).map(([n,v])=>[n,`${fmt(v.reads)} reads · ${fmt(v.lat_mean_ns)} ns mean`]))}</details>`:""}
  <div class="actions"><button id="original">${state.galleryOpen?"Hide":"Show"} original Chainsaw graphs &amp; metrics</button></div>
  ${state.galleryOpen?'<div id="gallery">Finding Chainsaw plots…</div>':""}`;
 }
 const knobs=t.knobs||[],groups={};for(const k of knobs)(groups[k.cat]??=[]).push(k.value);
 return `<h3>Configuration</h3>${kv({...t.config,model:t.model_path,simulator:t.simulator_name})}
 <details><summary>Memory configuration and fuse ratios</summary>${kv({memory:(t.mem_cfg||[]).join(", "),...t.fuse_ratios})}</details>
 <details><summary>MRC registers and timing</summary>${deep?.mrc?.error?errorText(deep.mrc.error):deep?.mrc?kv({...deep.mrc.xtor_config,...Object.fromEntries(Object.entries(deep.mrc.registers||{}).map(([key,value])=>[key,Object.keys(value).join(", ")]))}):'<button id="loadConfigDeep">Load MRC timing</button>'}</details>
 <details><summary>Command-line knobs (${knobs.length})</summary>${Object.entries(groups).map(([k,values])=>`<details><summary>${esc(k)} (${values.length})</summary><div class="path">${values.map(esc).join("<br>")}</div></details>`).join("")}</details>
 <details><summary>Full command line</summary><div class="path">${esc(t.cmdline)}</div></details>`;
}
function barChart(target,entries,color,unit){
 const el=$(target);if(!el)return;
 if(!entries.length){el.innerHTML='<div class="muted">No data available.</div>';return}
 const max=Math.max(1,...entries.map(x=>x[1]||0));const height=entries.length*27+10;
 el.innerHTML=`<svg viewBox="0 0 540 ${height}" role="img" aria-label="${esc(unit)} chart">${entries.map(([label,value],i)=>`<text x="4" y="${i*27+18}" font-size="12" fill="#273a4c">${esc(label.slice(0,20))}</text><rect x="155" y="${i*27+4}" width="${Math.max(0,(value||0)/max*280)}" height="17" rx="3" fill="${color}"/><text x="446" y="${i*27+18}" font-size="12" fill="#273a4c">${esc(fmt(value))} ${esc(unit)}</text>`).join("")}</svg>`;
}
function renderCharts(){
 if(state.view==="compare"){
  barChart("#compareBW",state.tests.map(t=>[name(t),t.dram?.dram_bw_mean]),"#2885c5","MB/s");
  barChart("#compareHit",state.tests.map(t=>[name(t),state.deep[t.test_dir]?.cbo?.l3_hit_rate]),"#23a16a","%");
  return;
 }
 const t=current();if(!t||state.tab!=="Charts")return;
 const d=state.deep[t.test_dir],ops=d?.xlsx?.idi_opcodes||{},hist=d?.santa?.hist;
 barChart("#opcodes",Object.entries(ops).sort((a,b)=>b[1]-a[1]).slice(0,12),"#2780bd","events");
 if(hist)barChart("#hist",hist.counts.map((n,i)=>[(hist.lo+i*hist.width).toFixed(0)+" ns",n]),"#28a78b","reads");
 else $("#hist").innerHTML='<div class="muted">No latency histogram available.</div>';
}
function bandwidthView(groups){
 const sections=groups.map(g=>{
  const overall=g.metrics.overall_bw_stats,grouped=g.metrics.bw_stats_bygrp;
  const renderRows=rows=>table(rows.filter(r=>/\b(?:Mean|Max) BW\b.*\(MBytes\/s\)/.test(r.name))
   .map(r=>[r.name.replace(/\s*\(MBytes\/s\)\s*$/,""),r.value==null?"—":gb(r.value)]));
  const overallRows=Array.isArray(overall)?overall.filter(r=>/\b(?:Mean|Max) BW\b.*\(MBytes\/s\)/.test(r.name)):[];
  const groupRows=Array.isArray(grouped)?grouped.filter(r=>/\b(?:Mean|Max) BW\b.*\(MBytes\/s\)/.test(r.name)):[];
  if(!overallRows.length&&!groupRows.length&&!overall?.error&&!grouped?.error)return "";
  return `<details><summary>${esc(g.folder==="."?"Run root / DRAM":g.folder)} · ${overallRows.length} overall, ${groupRows.length} grouped</summary>
   ${overall?.error?errorText(overall.error):overallRows.length?`<h3>Overall mean and max</h3>${renderRows(overallRows)}`:""}
   ${grouped?.error?errorText(grouped.error):groupRows.length?`<details><summary>Per-group mean and max (${groupRows.length})</summary>${renderRows(groupRows)}</details>`:""}
   </details>`;
 }).filter(Boolean);
 return sections.length?sections.join(""):'<div class="notice">No Chainsaw bandwidth summaries available for this run.</div>';
}
async function loadBandwidthGallery(path){
 const token=state.scanId;
 try{
  const data=state.gallery[path]||await json("/api/chainsaw",{path});
  if(token!==state.scanId||state.selected!==path)return;
  state.gallery[path]=data;
  if(state.tab==="Bandwidth"&&state.view==="run"&&$("#bandwidthGallery"))$("#bandwidthGallery").innerHTML=bandwidthView(data.groups||[]);
 }catch(error){
  if(token===state.scanId&&state.selected===path&&$("#bandwidthGallery"))$("#bandwidthGallery").innerHTML=errorText(error.message);
 }
}
function compareView(){return `<div class="detail-head"><h2>Compare all runs</h2><button id="back">Back to selected run</button></div><p class="muted">Comparison charts open on demand; tracker results appear as they load.</p>
 <div class="grid"><div class="panel"><h3>DRAM mean bandwidth</h3><div id="compareBW"></div></div><div class="panel"><h3>LLC hit rate</h3><div id="compareHit"></div></div></div>
 <button id="compareDeep">Load cache analysis for all runs</button>
 <details><summary>Knob differences</summary>${knobDifferences()}</details>`;
}
function knobDifferences(){
 const sets=state.tests.map(t=>new Set((t.knobs||[]).map(k=>`${k.cat}:${k.value}`)));
 const all=[...new Set(sets.flatMap(s=>[...s]))];
 const different=all.filter(k=>!sets.every(s=>s.has(k))&&!/^(env:)?(seed|rseed|predicted_runtime|list_line_num):/.test(k));
 return different.length?table(different.map(k=>[k,state.tests.filter((_,i)=>sets[i].has(k)).map(name).join(", ")])):'<p class="muted">No differing knobs after ignoring per-test seeds and list-line numbers.</p>';
}
const inflight=new Map();
let batchId=null;
function loadAllDeep(retry=false){
 const token=state.scanId;
 if(batchId===token)return;
 const todo=state.tests.filter(t=>(t.files?.cbo||t.files?.santa||t.files?.xlsx)&&
  (!state.deep[t.test_dir]||(retry&&state.deep[t.test_dir]?.error)));
 if(!todo.length){refreshSummary();return}
 batchId=token;
 for(const t of todo)state.deep[t.test_dir]="queued";
 refreshSummary();
 let index=0;
 const worker=async()=>{while(index<todo.length&&state.scanId===token){
  const t=todo[index++];
  await loadDeep(t.test_dir);
 }};
 Promise.all([worker(),worker()]).finally(()=>{if(batchId===token){batchId=null;refreshSummary()}});
}
function loadDeep(path){
 if(!path)return Promise.resolve();
 const scanId=state.scanId,requestKey=`${scanId}:${path}`;
 if(inflight.has(requestKey))return inflight.get(requestKey);
 const prior=state.deep[path];
 if(prior&&prior!=="queued"&&prior!=="loading"&&!prior.error)return Promise.resolve();
 state.deep[path]="loading";refreshSummary();
 const request=json("/api/deep",{path}).then(data=>{
  if(scanId===state.scanId)state.deep[path]=data;
 }).catch(error=>{
  if(scanId===state.scanId)state.deep[path]={error:error.message};
 }).finally(()=>{
  inflight.delete(requestKey);
  if(scanId!==state.scanId)return;
  refreshSummary();
  renderFlow();
  if(state.view==="compare")renderCharts();
  if(state.view==="run"&&state.selected===path&&state.tab!=="Overview"&&state.tab!=="Bandwidth"&&
      !(state.tab==="Charts"&&state.galleryOpen)){
   const content=$("#content");if(content){content.innerHTML=tabView(current());renderCharts()}
  }
 });
 inflight.set(requestKey,request);
 return request;
}
async function loadGallery(path){
 const scanId=state.scanId;
 try{const data=state.gallery[path]||await json("/api/chainsaw",{path});if(scanId!==state.scanId||state.selected!==path||!state.galleryOpen)return;
  state.gallery[path]=data;
  const groups=data.groups||[],$gallery=$("#gallery");
  $gallery.innerHTML=groups.length?`<p class="muted">${groups.reduce((n,g)=>n+g.images.length,0)} original plots across ${groups.length} folders. Images open full size when clicked.</p>
   <div class="grid">${groups.map(g=>`<section class="panel"><h3>${esc(g.folder==="."?"Run root":g.folder)}</h3>${Object.entries(g.metrics).map(([name,rows])=>`<details><summary>${esc(name.replaceAll("_"," "))} (${Array.isArray(rows)?rows.length:"error"})</summary>${Array.isArray(rows)?table(rows.map(r=>[r.name,fmt(r.value)])):errorText(rows.error)}</details>`).join("")}${g.images.map(file=>{const src=url("/api/chainsaw/image",{path,file});return `<figure><a href="${esc(src)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(src)}" alt="${esc(file)}"></a><figcaption>${esc(file.split("/").pop())}</figcaption></figure>`}).join("")}</section>`).join("")}</div>`:'<div class="notice">No original plots were generated for this run.</div>';
 }catch(error){if(scanId===state.scanId&&state.selected===path&&state.galleryOpen)$("#gallery").innerHTML=errorText(error.message)}
}
function initDashboard(){
 $("#scan").onclick=scan;$("#path").onkeydown=e=>{if(e.key==="Enter")scan()};
 $("#csv").onclick=()=>{if(state.root)location.href=url("/api/csv",{path:state.root,deep:"1"})};
 $("#browse").onclick=toggleBrowse;
 $("#logout").onclick=async()=>{try{await fetch("/api/logout",{method:"POST"})}finally{location.href="/login.html"}};
 json("/api/config",{}).then(c=>{state.allowedRoots=c.allowed_roots||[]}).catch(()=>{});
 json("/api/me",{}).then(me=>{if(me.is_admin)$("#setupLink").hidden=false}).catch(()=>{});
 document.addEventListener("click",e=>{if(e.target.id==="compareDeep")state.tests.forEach(t=>loadDeep(t.test_dir))});
 document.addEventListener("click",e=>{if(e.target.id==="loadConfigDeep")loadDeep(state.selected)});
 const fromQuery=new URLSearchParams(location.search).get("path");
 const initial=fromQuery||window.DASHBOARD_BASE;
 if(initial){$("#path").value=initial;scan()}
}
