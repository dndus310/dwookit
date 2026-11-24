"""DwoLens interactive reports for the check lines: verify, logs, smoke.

Same shape and rules as the analysis report (lens_report.py, shared JS_COMMON and CSS): one self-contained HTML
file, a sidebar with search, a summary that states the verdict and the counts first, and lists filtered by
parameters kept in the address (severity, category, new only, text), so every number links to the rows behind it.

    verify   summary (verdict, failed steps and tests) · each step (its log groups, filterable) · test explorer
             (result, name prefix, text) · each test
    logs     summary (verdict, counts, new against the previous run, errors to look at first, categories) ·
             problem explorer (severity, category, new, text) · each group (message, following lines, where) ·
             crashes (error, call stack) · log files
    smoke    summary (verdict, criteria, map cards filterable by verdict) · each map (counts, groups, crashes, log)

    python lens_report_runs.py --run <run dir> --kind verify|logs|smoke     -> <run>/out/report.html

The checks call write() right after their JSON; the earlier single-page report stays as report-classic.html.
Standard library only, no network.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lens_report import CSS, JS_COMMON  # noqa: E402

TITLES = {"verify": "검증", "logs": "로그 분석", "smoke": "맵 스모크"}

PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>__CSS__
pre{white-space:pre-wrap;word-break:break-all}
.new{background:#c2413a22;color:var(--fail)}
</style></head><body>
<div class="app">
 <nav class="side"><div class="brand"><span id="sub"></span><b id="pname"></b></div>
  <input class="search" id="search" type="search" placeholder="검색: 테스트, 카테고리, 메시지, 맵">
  <div class="nav" id="nav"></div></nav>
 <main id="main"><div class="wrap" id="view"></div></main>
</div>
<button class="btn theme" id="theme" title="밝게/어둡게">◐</button>
<script>
const DATA = __DATA__;
__COMMON__
const VERDICT = {pass: "통과", warn: "경고", fail: "실패", skip: "건너뜀", skipped: "건너뜀"};
const STATE = {Success: "성공", Fail: "실패", Failed: "실패", SuccessWithWarnings: "경고 포함 성공", NotRun: "미실행", InProcess: "실행 중"};
const CLS = {pass: "pass", success: "pass", warn: "warn", successwithwarnings: "warn", fail: "fail", failed: "fail", fatal: "fail", error: "fail", warning: "warn"};
const chip = v => `<span class="chip ${CLS[String(v).toLowerCase()] || ""}">${esc(VERDICT[v] || STATE[v] || v)}</span>`;
const newChip = `<span class="chip new">새</span>`;
const fileName = p => String(p || "").split(/[\\/]/).pop();
const n = (v, unit) => `${(+v || 0).toLocaleString()}${unit}`;
const crop = (s, max) => { s = String(s || ""); return s.length > max ? s.slice(0, max - 1) + "…" : s; };

// log groups of every summary on the page: one id space (prefix + index)
const GROUPS = {};   // id -> {g, where}
function registerGroups(prefix, summary, where){ (summary && summary.groups || []).forEach((g, i) => GROUPS[prefix + i] = {g, where}); }
function groupsOf(prefix, summary){ return ((summary && summary.groups) || []).map((g, i) => ({...g, id: prefix + i, isNew: g.new ? "새" : ""})); }
// the group table with its filters: severity, category, new only, text; parameters stay in the address
function groupTable(rows, params, empty){
  const cats = [...new Set(rows.map(g => g.category))].sort();
  const shown = rows.filter(g => (!params.sev || g.severity === params.sev) && (!params.cat || g.category === params.cat) && (!params.new || g.new)
    && (!params.q || (g.sample + " " + g.category).toLowerCase().includes(params.q.toLowerCase())));
  const total = shown.reduce((s, g) => s + g.count, 0);
  return `<div class="filters">${select("sev", "심각도", [["Fatal", "Fatal"], ["Error", "Error"], ["Warning", "Warning"]], params.sev)}
    ${select("cat", "카테고리", cats.map(c => [c, c]), params.cat)}${select("new", "변화", [["1", "새 문제만"]], params.new)}${textFilter("q", "메시지", params.q)}
    <span class="muted">그룹 ${shown.length}개 · ${n(total, "회")}</span></div>` +
    dataTable(shown, [{key: "severity", label: "심각도", fmt: g => chip(g.severity) + (g.new ? " " + newChip : "")}, {key: "category", label: "카테고리"},
      {key: "sample", label: "메시지", fmt: g => esc(crop(g.sample, 160)), link: g => link("group", g.id)}, {key: "count", label: "횟수", num: true, fmt: g => n(g.count, "회")}],
      params, empty || "조건에 맞는 문제 없음.");
}
function groupView(id){
  const x = GROUPS[id]; if (!x) return "<h1>없음</h1>";
  const g = x.g;
  return `<div class="eyebrow">${esc(x.where)} · ${esc(g.category)}</div><h1>${esc(crop(g.sample, 120))}</h1>
  <p class="lead">${chip(g.severity)}${g.new ? ' <span class="chip new">이전 실행에 없던 문제</span>' : ""} ${n(g.count, "회")} 발생</p>
  <h3>메시지</h3><div class="panel"><pre class="mono">${esc(g.sample)}</pre></div>
  ${(g.detail || []).length ? `<h3>이어진 줄 ${g.detail.length}개</h3><div class="panel"><pre class="mono">${esc(g.detail.join("\n"))}</pre></div>` : ""}
  <h3>위치 ${(g.where || []).length}곳 (파일:줄)</h3><ul>${(g.where || []).map(w => `<li class="mono">${esc(w)}</li>`).join("")}</ul>
  <p class="muted">숫자·경로만 다른 같은 메시지는 한 그룹으로 묶어 횟수로 셉니다. 그룹 ID ${esc(g.fingerprint || "")}</p>`;
}
function crashView(c){
  return `<div class="eyebrow">크래시 · ${esc(c.folder)}</div><h1>${esc(c.error || "크래시")}</h1>
  <p class="lead">${c.new ? '<span class="chip new">새 크래시</span> ' : ""}${c.time ? new Date(c.time * 1000).toLocaleString() : ""} ${esc(c.engineVersion || "")}</p>
  <h3>콜스택</h3><div class="panel"><pre class="mono">${esc((c.callstack || []).join("\n")) || "콜스택 없음"}</pre></div>`;
}
const conclusion = html => `<div class="panel conclusion">${html}</div>`;

// ------------------------------------------------------------------ per kind
const NAV = [];
let VIEWS = {};
function prefixOf(name){ const p = String(name).split("."); return p.length > 2 ? p.slice(0, 2).join(".") : p[0]; }

function setupVerify(){
  const steps = DATA.result.steps || [];
  const tests = steps.flatMap(s => (s.automation && s.automation.tests) || []).map((t, i) => ({...t, idx: i, prefix: prefixOf(t.name),
    short: t.name.split(".").slice(2).join(".") || t.name, stateWord: STATE[t.state] || t.state, firstError: (t.errors || [])[0] || ""}));
  NAV.push({sec: "top", label: "요약", hash: "#home"});
  if (tests.length) NAV.push({sec: "top", label: "테스트 전체", hash: "#tests", tag: tests.length + "개"});
  steps.forEach((s, i) => NAV.push({sec: "steps", label: `${s.id} ${s.name}`, hash: "#step/" + i, tag: VERDICT[s.verdict] || s.verdict}));
  steps.forEach((s, i) => { if (s.summary) registerGroups(`s${i}-`, s.summary, `${s.id} ${s.name}`); });
  for (const [p, list] of Object.entries(tests.reduce((m, t) => ((m[t.prefix] ||= []).push(t), m), {})))
    NAV.push({sec: "tests", label: p, hash: link("tests", "", {prefix: p}), tag: list.filter(t => t.state !== "Success").length ? `실패 ${list.filter(t => t.state !== "Success").length}` : list.length + "개"});
  tests.forEach(t => NAV.push({sec: "tests", label: t.name, hash: "#test/" + t.idx, searchOnly: true}));
  VIEWS.home = () => {
    const failedSteps = steps.filter(s => s.verdict === "fail"), failed = tests.filter(t => t.state !== "Success" && t.state !== "SuccessWithWarnings");
    const errs = steps.reduce((m, s) => m + (s.summary ? s.summary.counts.Error + s.summary.counts.Fatal : 0), 0);
    return `<div class="eyebrow">검증 · ${esc(DATA.runId)}</div><h1>${esc(DATA.project || "검증")}</h1>
    ${conclusion(`<b>판정</b> ${chip(DATA.result.verdict)} 단계 ${steps.length}개 중 실패 ${failedSteps.length}개${failedSteps.length ? ` (${failedSteps.map(s => `<a href="#step/${steps.indexOf(s)}">${esc(s.id)} ${esc(s.name)}</a>`).join(", ")})` : ""}.
      ${tests.length ? `자동화 테스트 ${tests.length}개 중 실패 ${failed.length}개.` : ""} ${errs ? `빌드·데이터 검증 로그 오류 ${errs}건.` : ""}`)}
    <div class="grid">${steps.map((s, i) => kpi(`${s.id} ${s.name}`, chip(s.verdict), s.summary ? `Error ${s.summary.counts.Error} · Warning ${s.summary.counts.Warning}`
      : s.automation ? `성공 ${s.automation.summary.succeeded} · 실패 ${s.automation.summary.failed}` : esc(s.note || "실행 안 함"), "#step/" + i)).join("")}</div>
    <h2>실패한 테스트 ${failed.length}개</h2>${failed.length ? dataTable(failed, [{key: "prefix", label: "묶음"}, {key: "short", label: "테스트", link: t => "#test/" + t.idx}, {key: "firstError", label: "첫 오류", fmt: t => esc(crop(t.firstError, 140))}], {}) : `<p class="muted">실패한 테스트 0개.</p>`}`;
  };
  VIEWS.tests = (arg, params) => {
    const prefixes = [...new Set(tests.map(t => t.prefix))].sort();
    const shown = tests.filter(t => (!params.state || t.state === params.state) && (!params.prefix || t.prefix === params.prefix) && (!params.q || t.name.toLowerCase().includes(params.q.toLowerCase())));
    return `<div class="eyebrow">자동화 테스트</div><h1>${esc(params.prefix || "테스트 전체")}</h1>
    <div class="filters">${select("state", "결과", [...new Set(tests.map(t => t.state))].map(s => [s, STATE[s] || s]), params.state)}${select("prefix", "묶음", prefixes.map(p => [p, p]), params.prefix)}
      ${textFilter("q", "이름", params.q)}<span class="muted">${shown.length}개</span></div>
    ${dataTable(shown, [{key: "prefix", label: "묶음"}, {key: "short", label: "테스트", link: t => "#test/" + t.idx}, {key: "stateWord", label: "결과", fmt: t => chip(t.state)}, {key: "firstError", label: "첫 오류", fmt: t => esc(crop(t.firstError, 140))}], params, "조건에 맞는 테스트 없음.")}`;
  };
  VIEWS.step = (i, params) => {
    const s = steps[+i]; if (!s) return "<h1>없음</h1>";
    let body = "";
    if (s.summary) body = `<div class="grid">${kpi("Fatal", n(s.summary.counts.Fatal, "건"), "", link("step", i, {sev: "Fatal"}))}${kpi("Error", n(s.summary.counts.Error, "건"), "", link("step", i, {sev: "Error"}))}${kpi("Warning", n(s.summary.counts.Warning, "건"), "", link("step", i, {sev: "Warning"}))}</div>
      <h2>오류·경고 그룹</h2>` + groupTable(groupsOf(`s${i}-`, s.summary), params, "오류·경고 0건.");
    else if (s.automation){
      const sm = s.automation.summary;
      body = `<div class="grid">${kpi("성공", n(sm.succeeded, "개"), "", link("tests", "", {state: "Success"}))}${kpi("경고 포함 성공", n(sm.succeededWithWarnings, "개"), "", link("tests", "", {state: "SuccessWithWarnings"}))}${kpi("실패", n(sm.failed, "개"), "", link("tests", "", {state: "Fail"}))}${kpi("미실행", n(sm.notRun, "개"), "", "#tests")}</div>
        <p><a href="#tests">테스트 ${tests.length}개 전체 보기</a></p>`;
    } else body = `<p class="muted">${esc(s.note || "이 단계는 실행하지 않았습니다.")}</p>`;
    return `<div class="eyebrow">검증 단계</div><h1>${esc(s.id)} ${esc(s.name)}</h1><p class="lead">${chip(s.verdict)} ${esc(s.note || "")}</p>` + body;
  };
  VIEWS.test = i => {
    const t = tests[+i]; if (!t) return "<h1>없음</h1>";
    const list = (h, xs) => xs && xs.length ? `<h3>${h} ${xs.length}건</h3><div class="panel"><pre class="mono">${esc(xs.join("\n"))}</pre></div>` : "";
    return `<div class="eyebrow"><a href="${link("tests", "", {prefix: t.prefix})}">${esc(t.prefix)}</a></div><h1>${esc(t.name)}</h1><p class="lead">${chip(t.state)}</p>${list("오류", t.errors)}${list("경고", t.warnings)}
     ${!(t.errors || []).length && !(t.warnings || []).length ? `<p class="muted">오류·경고 0건.</p>` : ""}`;
  };
}

function setupLogs(){
  const s = DATA.result, rows = groupsOf("g", s), crashes = s.crashes || [];
  registerGroups("g", s, "로그");
  const cats = {};
  for (const g of rows) cats[g.category] = (cats[g.category] || 0) + g.count;
  NAV.push({sec: "top", label: "요약", hash: "#home"});
  NAV.push({sec: "top", label: "문제 전체", hash: "#groups", tag: rows.length + "개"});
  if (s.newGroups) NAV.push({sec: "top", label: "새 문제", hash: link("groups", "", {new: "1"}), tag: s.newGroups + "개"});
  for (const sev of ["Fatal", "Error", "Warning"]){ const c = rows.filter(g => g.severity === sev).length; if (c) NAV.push({sec: "sev", label: sev, hash: link("groups", "", {sev}), tag: c + "개"}); }
  for (const [c, v] of Object.entries(cats).sort((a, b) => b[1] - a[1])) NAV.push({sec: "cat", label: c, hash: link("groups", "", {cat: c}), tag: n(v, "회")});
  rows.forEach(g => NAV.push({sec: "cat", label: g.sample, hash: link("group", g.id), searchOnly: true}));
  crashes.forEach((c, i) => NAV.push({sec: "crashes", label: c.error || c.folder, hash: "#crash/" + i, tag: c.new ? "새" : ""}));
  if ((s.files || []).length) NAV.push({sec: "top", label: "읽은 로그 파일", hash: "#files", tag: s.files.length + "개"});
  VIEWS.home = () => {
    const top = Object.entries(cats).sort((a, b) => b[1] - a[1])[0];
    const first = rows.filter(g => g.severity !== "Warning").sort((a, b) => (b.new ? 1 : 0) - (a.new ? 1 : 0) || b.count - a.count).slice(0, 10);
    return `<div class="eyebrow">로그 분석 · ${esc(DATA.runId)}</div><h1>${esc(DATA.project || "로그 분석")}</h1>
    ${conclusion(`<b>판정</b> ${chip(s.verdict)} Fatal ${n(s.counts.Fatal, "건")}, Error ${n(s.counts.Error, "건")}, Warning ${n(s.counts.Warning, "건")} (같은 메시지를 묶은 그룹 ${rows.length}개).
      이전 실행 대비 새 그룹 ${s.newGroups || 0}개, 새 크래시 ${s.newCrashes || 0}건.${top ? ` 가장 많은 카테고리는 <a href="${link("groups", "", {cat: top[0]})}">${esc(top[0])}</a> (${n(top[1], "회")}).` : ""}`)}
    <div class="grid">${kpi("Fatal", n(s.counts.Fatal, "건"), "", link("groups", "", {sev: "Fatal"}), s.counts.Fatal ? "warn" : "")}${kpi("Error", n(s.counts.Error, "건"), "", link("groups", "", {sev: "Error"}), s.counts.Error ? "warn" : "")}
      ${kpi("Warning", n(s.counts.Warning, "건"), "", link("groups", "", {sev: "Warning"}))}${kpi("새 그룹", n(s.newGroups, "개"), "이전 실행 대비", link("groups", "", {new: "1"}))}
      ${kpi("크래시", n(crashes.length, "건"), s.newCrashes ? `새 ${s.newCrashes}건` : "", crashes.length ? "#crash/0" : "#home")}</div>
    <h2>먼저 확인할 오류 (새 문제 먼저, 횟수 순)</h2>${first.length ? dataTable(first, [{key: "severity", label: "심각도", fmt: g => chip(g.severity) + (g.new ? " " + newChip : "")}, {key: "category", label: "카테고리"},
      {key: "sample", label: "메시지", fmt: g => esc(crop(g.sample, 140)), link: g => link("group", g.id)}, {key: "count", label: "횟수", num: true, fmt: g => n(g.count, "회")}], {}) : `<p class="muted">Fatal·Error 0건.</p>`}
    <h2>카테고리별 발생 횟수 (상위 20)</h2>${bars(Object.entries(cats).sort((a, b) => b[1] - a[1]).slice(0, 20).map(([k, v]) => [k, v, link("groups", "", {cat: k})]), "회")}`;
  };
  VIEWS.groups = (arg, params) => `<div class="eyebrow">로그 분석</div><h1>${esc(params.cat || params.sev || (params.new ? "새 문제" : "문제 전체"))}</h1>` + groupTable(rows, params, "오류·경고 0건.");
  VIEWS.crash = i => crashView(crashes[+i] || {});
  VIEWS.files = () => `<div class="eyebrow">로그 분석</div><h1>읽은 로그 파일 ${(s.files || []).length}개</h1><ul>${(s.files || []).map(f => `<li class="mono">${esc(f)}</li>`).join("")}</ul>`;
}

function setupSmoke(){
  const r = DATA.result, maps = (r.maps || []).map((m, i) => ({...m, idx: i, name: m.map.split("/").pop()}));
  NAV.push({sec: "top", label: "요약", hash: "#home"});
  maps.forEach(m => { NAV.push({sec: "maps", label: m.name, hash: "#map/" + m.idx, tag: VERDICT[m.verdict] || m.verdict}); registerGroups(`m${m.idx}-`, m, m.name); });
  VIEWS.home = (arg, params) => {
    const failed = maps.filter(m => m.verdict === "fail"), shown = maps.filter(m => !params.verdict || m.verdict === params.verdict);
    return `<div class="eyebrow">맵 스모크 · ${esc(DATA.runId)}</div><h1>${esc(DATA.project || "맵 스모크")}</h1>
    ${conclusion(`<b>판정</b> ${chip(r.verdict)} 맵 ${maps.length}개 중 실패 ${failed.length}개${failed.length ? ` (${failed.map(m => `<a href="#map/${m.idx}">${esc(m.name)}</a>`).join(", ")})` : ""}.
      기준: 맵마다 ${r.seconds}초 실행, 오류 ${r.maxErrors}건·경고 ${r.maxWarnings}건까지 허용, 크래시·Fatal은 실패.`)}
    <div class="filters">${select("verdict", "판정", [["pass", "통과"], ["warn", "경고"], ["fail", "실패"]], params.verdict)}<span class="muted">${shown.length}개</span></div>
    <div class="grid">${shown.map(m => kpi(m.name, chip(m.verdict), `오류 ${m.errors} · 경고 ${m.warnings} · ensure&nbsp;${m.ensures}${(m.crashes || []).length ? " · 크래시" : ""}${(m.reasons || []).length ? "<br>" + esc(m.reasons.join(", ")) : ""}`, "#map/" + m.idx)).join("") || `<p class="muted">조건에 맞는 맵 없음.</p>`}</div>`;
  };
  VIEWS.map = (i, params) => {
    const m = maps[+i]; if (!m) return "<h1>없음</h1>";
    return `<div class="eyebrow">맵 스모크</div><h1>${esc(m.name)}</h1><p class="lead">${chip(m.verdict)} ${(m.reasons || []).map(x => `<span class="chip fail">${esc(x)}</span>`).join("")}<br><span class="mono muted">${esc(m.map)}</span></p>
    <div class="grid">${kpi("실행 시간", n(m.seconds, "초"), "", "#map/" + i)}${kpi("Error", n(m.errors, "건"), "", link("map", i, {sev: "Error"}))}${kpi("Warning", n(m.warnings, "건"), "", link("map", i, {sev: "Warning"}))}
     ${kpi("ensure", n(m.ensures, "건"), "", "#map/" + i)}${kpi("종료", m.fatal ? "Fatal" : m.exitCode != null ? "코드 " + m.exitCode : m.killed ? "시간 종료" : "-", m.killed ? "정해진 시간 뒤 종료 (정상)" : "", "#map/" + i)}</div>
    <h2>오류·경고 그룹</h2>${groupTable(groupsOf(`m${i}-`, m), params, "오류·경고 0건.")}
    ${(m.crashes || []).length ? `<h2>크래시 ${m.crashes.length}건</h2>${m.crashes.map(c => crashView(c)).join("")}` : ""}
    <p class="muted mono">로그: ${esc(m.log || "없음")}</p>`;
  };
}

// ------------------------------------------------------------------ sidebar and routing
const SECS = {verify: [["top", ""], ["steps", "단계"], ["tests", "테스트 묶음"]],
              logs: [["top", ""], ["sev", "심각도"], ["cat", "카테고리"], ["crashes", "크래시"]],
              smoke: [["top", ""], ["maps", "맵"]]}[DATA.kind];
const closed = new Set(), openGrp = new Set();
const itemHtml = x => `<a class="it" href="${x.hash}" title="${esc(x.label)}">${esc(crop(x.label, 60))}${x.tag ? `<span class="t">${esc(x.tag)}</span>` : ""}</a>`;
function renderNav(){
  const q = $("#search").value.trim().toLowerCase();
  let html = "";
  for (const [sec, title] of SECS){
    const items = NAV.filter(x => x.sec === sec && (q ? x.label.toLowerCase().includes(q) : !x.searchOnly));
    if (!items.length) continue;
    if (sec === "top"){ html += `<div class="top">${items.map(itemHtml).join("")}</div>`; continue; }
    const open = q || !closed.has(sec);
    html += `<div class="sec"><div class="h" data-sec="${sec}"><span>${open ? "▾" : "▸"} ${title}</span><span class="n">${items.length}</span></div>${open ? items.slice(0, 400).map(itemHtml).join("") : ""}</div>`;
  }
  $("#nav").innerHTML = html || `<div class="it muted">일치하는 항목 없음</div>`;
  markCurrent();
}
function markCurrent(){ const h = location.hash || "#home"; for (const a of document.querySelectorAll(".it")) a.classList.toggle("cur", a.getAttribute("href") === h); }
document.addEventListener("click", e => {
  const s = e.target.closest("[data-sec]"); if (s){ const k = s.dataset.sec; closed.has(k) ? closed.delete(k) : closed.add(k); renderNav(); }
});
let lastPath = "";
function render(){
  const r = route();
  const v = r.kind === "group" ? () => groupView(r.arg) : VIEWS[r.kind] || VIEWS.home;
  $("#view").innerHTML = v(r.arg, r.params);
  markCurrent();
  const p = (location.hash || "").split("?")[0];
  if (p !== lastPath) $("#main").scrollTop = 0;
  lastPath = p;
}
({verify: setupVerify, logs: setupLogs, smoke: setupSmoke})[DATA.kind]();
$("#pname").textContent = DATA.project || DATA.title;
$("#sub").textContent = `DwoLens · ${DATA.title} · ${DATA.runId}`;
$("#search").oninput = renderNav;
$("#search").onkeydown = e => { if (e.key === "Enter"){ const a = document.querySelector(".nav .it[href]"); if (a) location.hash = a.getAttribute("href"); } };
$("#theme").onclick = () => { const r = document.documentElement; const dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; r.dataset.theme = dark ? "light" : "dark"; };
window.onhashchange = render;
renderNav(); render();
</script></body></html>
"""


def project_name(run: Path) -> str:
    """The project of a run folder (<project>/__dwookit/data/runs/<id>): state.json's name, else the folder."""
    for parent in run.resolve().parents:
        if parent.name == "__dwookit":
            try:
                state = json.loads((parent / "state.json").read_text(encoding="utf-8-sig"))
                if state.get("project"):
                    return state["project"]
            except (OSError, ValueError):
                pass
            return parent.parent.name
    return ""


def write(run: Path, kind: str, result: dict | None = None) -> Path:
    out = run / "out"
    if result is None:
        result = json.loads((out / f"{kind}.json").read_text(encoding="utf-8-sig"))
    data = {"schema": "lens.report.run/1", "kind": kind, "title": TITLES[kind], "runId": run.name,
            "project": project_name(run), "result": result}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (PAGE.replace("__COMMON__", JS_COMMON).replace("__CSS__", CSS).replace("__TITLE__", f"{data['project']} · {TITLES[kind]}".replace("<", ""))
            .replace("__DATA__", payload))
    path = out / "report.html"
    path.write_text(html, encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--kind", required=True, choices=sorted(TITLES))
    args = ap.parse_args(argv)
    path = write(args.run, args.kind)
    print(f"lens_report_runs: {path} ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
