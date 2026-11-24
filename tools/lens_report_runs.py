"""DwoLens interactive reports for the check lines: verify, logs, smoke.

Same shape as the analysis report (lens_report.py): one self-contained HTML file, a sidebar with search to pick
what to look at, and a view for the picked item, so long result lists stay readable.

    verify   overview (step verdicts, failed tests first) · each step · tests grouped by name prefix · the log
             groups of the compile and data validation steps
    logs     overview (counts, categories, new problems) · new problems · Fatal / Error / Warning by category ·
             each group (sample, detail lines, where) · crashes (error, call stack) · log files
    smoke    overview (one card per map) · each map (reasons, counts, groups, crashes, log) · its groups

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
from lens_report import CSS  # noqa: E402

TITLES = {"verify": "검증", "logs": "로그 분석", "smoke": "맵 스모크"}

PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>__CSS__
.bars div{display:flex;align-items:center;gap:8px;margin:3px 0}.bars i{display:inline-block;height:10px;border-radius:3px;background:var(--accent)}
.bars .lbl{width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
.card{cursor:pointer}.card:hover{border-color:var(--accent)}pre{white-space:pre-wrap;word-break:break-all}
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
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const chip = v => `<span class="chip ${esc(String(v).toLowerCase() === "success" ? "pass" : String(v).toLowerCase())}">${esc(v)}</span>`;
const SEV = {Fatal: "fail", Error: "fail", Warning: "warn"};
const kpis = items => `<div class="grid">${items.map(([k, v, cls]) => `<div class="panel kpi"><div class="k">${esc(k)}</div><div class="v ${cls || ""}">${esc(v)}</div></div>`).join("")}</div>`;
const fileName = p => String(p || "").split(/[\\/]/).pop();

// groups of a log summary: one id space per page (prefix + index)
const GROUPS = {};   // id -> {g, where}
function registerGroups(prefix, summary, where){ (summary && summary.groups || []).forEach((g, i) => GROUPS[prefix + i] = {g, where}); }
function groupRows(prefix, summary, limit){
  const gs = (summary && summary.groups) || [];
  if (!gs.length) return "<p class=muted>오류·경고 없음</p>";
  return `<input class="search" style="margin:0 0 8px;width:280px" placeholder="표 안에서 찾기" oninput="filterTable(this)"><table><tr><th>심각도</th><th>카테고리</th><th>메시지</th><th>횟수</th></tr>${gs.slice(0, limit || 500).map((g, i) =>
    `<tr><td>${chip(g.severity)}${g.new ? ' <span class="chip new">새</span>' : ""}</td><td>${esc(g.category)}</td><td><a href="#group/${prefix + i}">${esc(g.sample.length > 160 ? g.sample.slice(0, 159) + "…" : g.sample)}</a></td><td>${g.count}</td></tr>`).join("")}</table>`;
}
window.filterTable = inp => { const q = inp.value.toLowerCase(); for (const tr of inp.nextElementSibling.querySelectorAll("tr:not(:first-child)")) tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none"; };
function groupView(id){
  const x = GROUPS[id]; if (!x) return "<h1>없음</h1>";
  const g = x.g;
  return `<div class="eyebrow">${esc(x.where)}</div><h1>${esc(g.category)}</h1><p class="lead">${chip(g.severity)}${g.new ? ' <span class="chip new">이전 실행에 없던 문제</span>' : ""} ${g.count}회</p>
  <div class="panel"><pre class="mono">${esc(g.sample)}</pre></div>
  ${(g.detail || []).length ? `<h3>이어지는 줄</h3><div class="panel"><pre class="mono">${esc(g.detail.join("\n"))}</pre></div>` : ""}
  <h3>나온 곳</h3><ul>${(g.where || []).map(w => `<li class="mono">${esc(w)}</li>`).join("")}</ul>
  <p class="muted">지문 ${esc(g.fingerprint || "")} — 같은 지문은 숫자·경로가 달라도 같은 문제로 묶입니다.</p>`;
}
function crashView(c){
  return `<div class="eyebrow">크래시 · ${esc(c.folder)}</div><h1>${esc(c.error || "크래시")}</h1>
  <p class="lead">${c.new ? '<span class="chip new">새 크래시</span> ' : ""}${c.time ? new Date(c.time * 1000).toLocaleString() : ""} ${esc(c.engineVersion || "")}</p>
  <h3>콜스택</h3><div class="panel"><pre class="mono">${esc((c.callstack || []).join("\n")) || "없음"}</pre></div>`;
}
function bars(pairs){
  const max = Math.max(1, ...pairs.map(p => p[1]));
  return `<div class="bars">${pairs.map(([k, v, href]) => `<div><span class="lbl">${href ? `<a href="${href}">${esc(k)}</a>` : esc(k)}</span><i style="width:${Math.max(3, 300 * v / max)}px"></i><span class="muted">${v}</span></div>`).join("")}</div>`;
}

// ------------------------------------------------------------------ per kind
const NAV = [];
let VIEWS = {};
function prefixOf(name){ const p = String(name).split("."); return p.length > 2 ? p.slice(0, 2).join(".") : p[0]; }

function setupVerify(){
  const steps = DATA.result.steps || [];
  NAV.push({sec: "top", label: "개요", hash: "#home"});
  steps.forEach((s, i) => NAV.push({sec: "steps", label: `${s.id} ${s.name}`, hash: "#step/" + i, tag: s.verdict}));
  steps.forEach((s, i) => { if (s.summary) { registerGroups(`s${i}-`, s.summary, `${s.id} ${s.name}`); (s.summary.groups || []).forEach((g, j) => NAV.push({sec: "groups", group: `${s.id} ${s.name}`, label: g.sample, hash: `#group/s${i}-${j}`, tag: g.severity})); } });
  const tests = steps.flatMap(s => (s.automation && s.automation.tests) || []);
  tests.forEach((t, i) => NAV.push({sec: "tests", group: prefixOf(t.name), label: t.name.split(".").slice(2).join(".") || t.name, hash: "#test/" + i, tag: t.state === "Success" ? "" : t.state, full: t.name}));
  VIEWS.home = () => {
    const failed = tests.filter(t => t.state !== "Success");
    return `<div class="eyebrow">검증 · ${esc(DATA.runId)}</div><h1>${esc(DATA.project)}</h1><p class="lead">결과 ${chip(DATA.result.verdict)}</p>
    <div class="grid">${steps.map((s, i) => `<a class="panel kpi card" href="#step/${i}"><div class="k">${esc(s.id)} ${esc(s.name)}</div><div class="v">${chip(s.verdict)}</div>
      <div class="muted">${s.summary ? `Error ${s.summary.counts.Error} · Warning ${s.summary.counts.Warning}` : s.automation ? `성공 ${s.automation.summary.succeeded} · 실패 ${s.automation.summary.failed}` : esc(s.note || "실행 안 함")}</div></a>`).join("")}</div>
    <h2>실패한 테스트 (${failed.length})</h2>${failed.length ? failed.map(t => `<div><a href="#test/${tests.indexOf(t)}">${esc(t.name)}</a> ${chip(t.state)}</div>`).join("") : "<p class=muted>없음</p>"}`;
  };
  VIEWS.step = i => {
    const s = steps[+i]; if (!s) return "<h1>없음</h1>";
    let body = "";
    if (s.summary) body = kpis([["Fatal", s.summary.counts.Fatal], ["Error", s.summary.counts.Error], ["Warning", s.summary.counts.Warning]]) + `<h2>오류 · 경고 그룹</h2>` + groupRows(`s${i}-`, s.summary);
    else if (s.automation){
      const a = s.automation, sm = a.summary;
      body = kpis([["성공", sm.succeeded], ["경고 포함 성공", sm.succeededWithWarnings], ["실패", sm.failed], ["미실행", sm.notRun]]) +
        `<h2>테스트</h2><input class="search" style="margin:0 0 8px;width:280px" placeholder="표 안에서 찾기" oninput="filterTable(this)"><table><tr><th>테스트</th><th>결과</th><th>오류</th></tr>${a.tests.map(t =>
          `<tr><td><a href="#test/${tests.indexOf(t)}">${esc(t.name)}</a></td><td>${chip(t.state)}</td><td>${esc((t.errors || []).slice(0, 2).join(" / "))}</td></tr>`).join("")}</table>`;
    } else body = `<p class="muted">${esc(s.note || "실행 안 함")}</p>`;
    return `<div class="eyebrow">검증 단계</div><h1>${esc(s.id)} ${esc(s.name)}</h1><p class="lead">${chip(s.verdict)} ${esc(s.note || "")}</p>` + body;
  };
  VIEWS.test = i => {
    const t = tests[+i]; if (!t) return "<h1>없음</h1>";
    const list = (h, xs) => xs && xs.length ? `<h3>${h} (${xs.length})</h3><div class="panel"><pre class="mono">${esc(xs.join("\n"))}</pre></div>` : "";
    return `<div class="eyebrow">자동화 테스트 · ${esc(prefixOf(t.name))}</div><h1>${esc(t.name)}</h1><p class="lead">${chip(t.state)}</p>${list("오류", t.errors)}${list("경고", t.warnings)}
     ${!(t.errors || []).length && !(t.warnings || []).length ? "<p class=muted>오류·경고 없음</p>" : ""}`;
  };
}

function setupLogs(){
  const s = DATA.result;
  registerGroups("g", s, "로그");
  NAV.push({sec: "top", label: "개요", hash: "#home"});
  if (s.newGroups) NAV.push({sec: "top", label: "새 문제", hash: "#new", tag: s.newGroups});
  (s.groups || []).forEach((g, i) => NAV.push({sec: g.severity, group: g.category, label: g.sample, hash: "#group/g" + i, tag: g.new ? "새" : g.count}));
  (s.crashes || []).forEach((c, i) => NAV.push({sec: "crashes", label: c.error || c.folder, hash: "#crash/" + i, tag: c.new ? "새" : ""}));
  (s.files || []).forEach(f => NAV.push({sec: "files", label: fileName(f), hash: "#files"}));
  const cats = {};
  for (const g of s.groups || []) cats[g.category] = (cats[g.category] || 0) + g.count;
  VIEWS.home = () => `<div class="eyebrow">로그 분석 · ${esc(DATA.runId)}</div><h1>${esc(DATA.project)}</h1><p class="lead">결과 ${chip(s.verdict)} · 파일 ${(s.files || []).length}개</p>
    ${kpis([["Fatal", s.counts.Fatal], ["Error", s.counts.Error], ["Warning", s.counts.Warning], ["그룹", (s.groups || []).length], ["새 그룹", s.newGroups], ["크래시", (s.crashes || []).length]])}
    <h2>카테고리별 (상위 20)</h2>${bars(Object.entries(cats).sort((a, b) => b[1] - a[1]).slice(0, 20).map(([k, v]) => [k, v, "#cat/" + encodeURIComponent(k)]))}
    <h2>새 문제</h2>${(s.groups || []).some(g => g.new) ? (s.groups || []).map((g, i) => g.new ? `<div>${chip(g.severity)} <a href="#group/g${i}">${esc(g.category)}: ${esc(g.sample.slice(0, 140))}</a> <span class="muted">${g.count}회</span></div>` : "").join("") : "<p class=muted>이전 실행에 없던 문제는 없습니다.</p>"}
    <h2>많이 나온 문제 (상위 15)</h2>${(s.groups || []).map((g, i) => [g, i]).sort((a, b) => b[0].count - a[0].count).slice(0, 15).map(([g, i]) => `<div>${chip(g.severity)} <a href="#group/g${i}">${esc(g.category)}: ${esc(g.sample.slice(0, 140))}</a> <span class="muted">${g.count}회</span></div>`).join("") || "<p class=muted>없음</p>"}`;
  VIEWS.new = () => { const idx = (s.groups || []).map((g, i) => [g, i]).filter(x => x[0].new);
    return `<h1>새 문제</h1><p class="lead">이전 실행에 없던 오류·경고 그룹 ${idx.length}개</p><table><tr><th>심각도</th><th>카테고리</th><th>메시지</th><th>횟수</th></tr>${idx.map(([g, i]) => `<tr><td>${chip(g.severity)}</td><td>${esc(g.category)}</td><td><a href="#group/g${i}">${esc(g.sample)}</a></td><td>${g.count}</td></tr>`).join("")}</table>`; };
  VIEWS.cat = c => { const idx = (s.groups || []).map((g, i) => [g, i]).filter(x => x[0].category === c);
    return `<div class="eyebrow">카테고리</div><h1>${esc(c)}</h1><p class="lead">그룹 ${idx.length}개 · ${idx.reduce((n, x) => n + x[0].count, 0)}회</p><table><tr><th>심각도</th><th>메시지</th><th>횟수</th></tr>${idx.map(([g, i]) => `<tr><td>${chip(g.severity)}${g.new ? ' <span class="chip new">새</span>' : ""}</td><td><a href="#group/g${i}">${esc(g.sample)}</a></td><td>${g.count}</td></tr>`).join("")}</table>`; };
  VIEWS.crash = i => crashView((s.crashes || [])[+i] || {});
  VIEWS.files = () => `<h1>읽은 로그 파일</h1><ul>${(s.files || []).map(f => `<li class="mono">${esc(f)}</li>`).join("")}</ul>`;
}

function setupSmoke(){
  const r = DATA.result, maps = r.maps || [];
  NAV.push({sec: "top", label: "개요", hash: "#home"});
  maps.forEach((m, i) => {
    NAV.push({sec: "maps", label: m.map.split("/").pop(), hash: "#map/" + i, tag: m.verdict});
    registerGroups(`m${i}-`, m, m.map);
    (m.groups || []).forEach((g, j) => NAV.push({sec: "groups", group: m.map.split("/").pop(), label: g.sample, hash: `#group/m${i}-${j}`, tag: g.severity}));
  });
  VIEWS.home = () => `<div class="eyebrow">맵 스모크 · ${esc(DATA.runId)}</div><h1>${esc(DATA.project)}</h1>
    <p class="lead">결과 ${chip(r.verdict)} · 맵마다 ${r.seconds}초 실행 · 허용 오류 ${r.maxErrors} · 허용 경고 ${r.maxWarnings}</p>
    <div class="grid">${maps.map((m, i) => `<a class="panel kpi card" href="#map/${i}"><div class="k">${esc(m.map.split("/").pop())}</div><div class="v">${chip(m.verdict)}</div>
     <div class="muted">오류 ${m.errors} · 경고 ${m.warnings} · ensure ${m.ensures}${m.crashed ? " · 크래시" : ""}</div>${(m.reasons || []).length ? `<div class="muted">${esc(m.reasons.join(", "))}</div>` : ""}</a>`).join("")}</div>`;
  VIEWS.map = i => {
    const m = maps[+i]; if (!m) return "<h1>없음</h1>";
    return `<div class="eyebrow">맵 스모크</div><h1>${esc(m.map)}</h1><p class="lead">${chip(m.verdict)} ${(m.reasons || []).map(x => `<span class="chip fail">${esc(x)}</span>`).join("")}</p>
    ${kpis([["실행 초", m.seconds], ["오류", m.errors], ["경고", m.warnings], ["ensure", m.ensures], ["Fatal", m.fatal ? "있음" : "없음"], ["종료 코드", m.exitCode ?? (m.killed ? "시간 끝 (정상)" : "-")]])}
    <h2>오류 · 경고 그룹</h2>${groupRows(`m${i}-`, m)}
    ${(m.crashes || []).length ? `<h2>크래시</h2>${m.crashes.map(c => crashView(c)).join("")}` : ""}
    <p class="muted mono">로그: ${esc(m.log || "없음")}</p>`;
  };
}

// ------------------------------------------------------------------ sidebar and routing
const SECS = {verify: [["top", ""], ["steps", "단계"], ["tests", "테스트"], ["groups", "로그 문제"]],
              logs: [["top", ""], ["Fatal", "Fatal"], ["Error", "Error"], ["Warning", "Warning"], ["crashes", "크래시"], ["files", "파일"]],
              smoke: [["top", ""], ["maps", "맵"], ["groups", "맵별 문제"]]}[DATA.kind];
const closed = new Set(DATA.kind === "logs" ? ["Warning", "files"] : DATA.kind === "verify" ? ["groups"] : []), openGrp = new Set();
const itemHtml = n => `<a class="it" href="${n.hash}" title="${esc(n.full || n.label)}">${esc(n.label.length > 60 ? n.label.slice(0, 59) + "…" : n.label)}${n.tag ? `<span class="t">${esc(n.tag)}</span>` : ""}</a>`;
function renderNav(){
  const q = $("#search").value.trim().toLowerCase();
  let html = "";
  for (const [sec, title] of SECS){
    const items = NAV.filter(n => n.sec === sec && (!q || (n.label + " " + (n.group || "") + " " + (n.full || "")).toLowerCase().includes(q)));
    if (!items.length) continue;
    if (sec === "top"){ html += `<div class="top">${items.map(itemHtml).join("")}</div>`; continue; }
    const open = q || !closed.has(sec);
    html += `<div class="sec"><div class="h" data-sec="${sec}"><span>${open ? "▾" : "▸"} ${title}</span><span class="n">${items.length}</span></div>`;
    if (open){
      const groups = {};
      for (const it of items) (groups[it.group || ""] ||= []).push(it);
      for (const [g, list] of Object.entries(groups)){
        if (!g){ html += list.slice(0, 500).map(itemHtml).join(""); continue; }
        const gopen = q || openGrp.has(sec + "|" + g) || Object.keys(groups).length === 1;
        html += `<div class="grp"><div class="h" data-grp="${esc(sec + "|" + g)}"><span>${gopen ? "▾" : "▸"} ${esc(g)}</span><span class="muted">${list.length}</span></div>`;
        if (gopen) html += list.slice(0, 300).map(itemHtml).join("");
        html += `</div>`;
      }
    }
    html += `</div>`;
  }
  $("#nav").innerHTML = html || `<div class="it muted">찾는 항목이 없습니다</div>`;
  for (const a of document.querySelectorAll(".it")) a.classList.toggle("cur", a.getAttribute("href") === location.hash);
}
document.addEventListener("click", e => {
  const s = e.target.closest("[data-sec]"); if (s){ const k = s.dataset.sec; closed.has(k) ? closed.delete(k) : closed.add(k); renderNav(); return; }
  const g = e.target.closest("[data-grp]"); if (g){ const k = g.dataset.grp; openGrp.has(k) ? openGrp.delete(k) : openGrp.add(k); renderNav(); }
});
function render(){
  const m = decodeURIComponent(location.hash || "#home").match(/^#([^/]+)\/?(.*)$/) || [null, "home", ""];
  const kind = m[1], arg = m[2];
  const v = kind === "group" ? () => groupView(arg) : VIEWS[kind] || VIEWS.home;
  $("#view").innerHTML = v(arg);
  for (const a of document.querySelectorAll(".it")) a.classList.toggle("cur", a.getAttribute("href") === location.hash);
  $("#main").scrollTop = 0;
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
    """The project of a run folder (<project>/__dwo-forge/data/runs/<id>): state.json's name, else the folder."""
    for parent in run.resolve().parents:
        if parent.name == "__dwo-forge":
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
    html = (PAGE.replace("__CSS__", CSS).replace("__TITLE__", f"{data['project']} · {TITLES[kind]}".replace("<", ""))
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
