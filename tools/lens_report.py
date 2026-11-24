"""DwoLens interactive report: one self-contained HTML file to explore an analysis run.

Instead of one long page with every table and diagram, the report is an app: a sidebar to pick what to look at
(overview, game flow by level or GameMode, classes by role, problems by rule, quality lists, tracked files,
modules) with search, and a view for the picked item. Diagrams are drawn in the browser from the embedded data,
only for what is picked, at a chosen depth, with leaf objects grouped and a step-by-step playback, so large
projects stay readable. No network: no external fonts or libraries.

    python lens_report.py --out <run>/out [--project <dir>]      -> <run>/out/report.html

lens_analyze.py calls it after every analysis; the earlier single-page report stays as report-classic.html.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RULE_NAMES = {
    "tick-usage": "Tick 사용", "asset-hard-cycle": "에셋 하드 참조 순환", "hard-ref-chain": "하드 참조 체인",
    "replication": "복제 설정", "naming": "이름 규칙", "redirector": "남은 리디렉터", "unused-asset": "미사용 에셋",
    "bp-complexity": "블루프린트 복잡도", "cycle": "순환 참조", "module-cycle": "모듈 순환", "unknown-relations": "알 수 없는 관계",
}


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def build_data(out: Path, project: Path | None) -> dict:
    run = load(out.parent / "run.json", {}) or {}
    graph = load(out / "graph.json", {}) or {}
    findings = load(out / "findings.json", {}) or {}
    quality = load(out / "quality.json", {}) or {}
    features = load(out / "features.json", {}) or {}
    flow = load(out / "flow.json", {}) or {}
    changes = load(out / "changes.json")
    meta = graph.get("meta", {})
    symbols = graph.get("symbols", [])
    project_name = Path(str(meta.get("project", ""))).stem or run.get("project", "")

    classes = {}
    for c in features.get("classes", []):
        feats = []
        for f in c.get("features", []):
            feats.append({k: f.get(k) for k in ("id", "name", "category", "trigger", "source", "where", "steps") if f.get(k) is not None})
        behaviors = []
        for b in c.get("behaviors", []):
            behaviors.append({k: b.get(k) for k in ("name", "nameEn", "features", "primary", "cases", "writes", "reaches", "player") if b.get(k)})
        classes[c["key"]] = {"name": c.get("name"), "kind": c.get("kind"), "parent": c.get("parent"), "features": feats, "behaviors": behaviors}
    # every project class appears, with its role from the flow model even when it has no features
    for s in symbols:
        if s.get("kind") in ("blueprint", "class") and not s.get("external") and s["key"] not in classes:
            classes[s["key"]] = {"name": s.get("name"), "kind": s.get("kind"), "features": [], "behaviors": []}
    flow_nodes = flow.get("nodes", {})
    for key, c in classes.items():
        n = flow_nodes.get(key, {})
        c["role"] = n.get("role") or next((u.get("role") for u in flow.get("unreached", []) if u.get("key") == key), "object")
        c["roleWords"] = n.get("roleWords") or next((u.get("roleWords") for u in flow.get("unreached", []) if u.get("key") == key), "")
        c["path"] = n.get("path", "")
        c["chain"] = n.get("chain", [])
        if n.get("inPlay"):
            c["inPlay"] = n["inPlay"]
        if n.get("tracked"):
            c["tracked"] = True

    modules = []
    deps = {}
    for r in graph.get("relations", []):
        if r.get("type") == "DependsOnModule":
            deps.setdefault(r["source"], []).append(r["target"])
    for s in symbols:
        if s.get("kind") == "module":
            modules.append({"key": s["key"], "name": s.get("name"), "external": s.get("external", False), "deps": sorted(deps.get(s["key"], []))})

    problems = []
    for f in findings.get("findings", []):
        items = f.get("items") or []
        problems.append({"rule": f.get("rule", ""), "severity": f.get("severity", "info"), "message": f.get("message", ""),
                         "items": items[:300], "more": max(0, len(items) - 300)})

    complexity = sorted((quality.get("complexity") or {}).get("graphs", []), key=lambda g: -(g.get("nodes") or 0))[:300]
    unused = (quality.get("unused") or {}).get("unused", [])[:1000]
    naming = (quality.get("naming") or {}).get("violations", [])[:1000]

    files = []
    if project:
        root = project / "__dwo-forge" / "blueprints"
        for p in sorted(root.rglob("*.explain.json")) if root.is_dir() else []:
            doc = load(p)
            if doc:
                files.append(doc)

    return {
        "schema": "lens.report/1", "project": project_name, "engine": str(meta.get("engineVersion", "")).split("-")[0],
        "runId": run.get("runId") or out.parent.name, "source": run.get("source", "analyze"),
        "finishedAt": run.get("finishedAt", ""), "verdict": findings.get("verdict") or run.get("final", ""),
        "counts": {"symbols": len(symbols), "relations": len(graph.get("relations", [])),
                   "classes": sum(1 for s in symbols if s.get("kind") == "class" and not s.get("external")),
                   "blueprints": sum(1 for s in symbols if s.get("kind") == "blueprint" and not s.get("external")),
                   "assets": sum(1 for s in symbols if s.get("kind") == "asset" and not s.get("external"))},
        "ruleNames": RULE_NAMES, "categories": features.get("categories", []),
        "flow": {"roots": flow.get("roots", []), "nodes": flow_nodes, "edges": flow.get("edges", []), "unreached": flow.get("unreached", [])},
        "classes": classes, "problems": problems, "complexity": complexity, "unused": unused, "naming": naming,
        "modules": modules, "files": files, "changes": changes,
    }


TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f3f5f8;--panel:#fff;--sunk:#e9edf3;--fg:#18202c;--muted:#5a6577;--line:#d3dae4;--accent:#2f5bd3;--accent-soft:#e3eafb;
--ok:#1f8a4c;--warn:#b7791f;--fail:#c2413a;--info:#4b6a88;
--r-map:#0f8a7e;--r-mode:#7a3fc4;--r-ctrl:#2f5bd3;--r-pawn:#c06a12;--r-ui:#c2417a;--r-comp:#8a6a3a;--r-gas:#a08a12;--r-data:#557a1f;--r-ai:#c2413a;--r-other:#6b7280;
--font:"Malgun Gothic","Apple SD Gothic Neo","Segoe UI",system-ui,sans-serif;--mono:"Cascadia Code",Consolas,ui-monospace,monospace;--side:310px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f141c;--panel:#161d28;--sunk:#1d2633;--fg:#e4e9f1;--muted:#93a0b4;--line:#2a3546;--accent:#7d9dff;--accent-soft:#1f2b47;
--r-map:#3cc4b4;--r-mode:#b48bf2;--r-ctrl:#7d9dff;--r-pawn:#f0a24f;--r-ui:#f07aab;--r-comp:#c9a46a;--r-gas:#e2cf5a;--r-data:#9ccc5a;--r-ai:#f58b62;--r-other:#a0a8b6;color-scheme:dark}}
:root[data-theme=dark]{--bg:#0f141c;--panel:#161d28;--sunk:#1d2633;--fg:#e4e9f1;--muted:#93a0b4;--line:#2a3546;--accent:#7d9dff;--accent-soft:#1f2b47;
--r-map:#3cc4b4;--r-mode:#b48bf2;--r-ctrl:#7d9dff;--r-pawn:#f0a24f;--r-ui:#f07aab;--r-comp:#c9a46a;--r-gas:#e2cf5a;--r-data:#9ccc5a;--r-ai:#f58b62;--r-other:#a0a8b6;color-scheme:dark}
*{box-sizing:border-box}html,body{height:100%;margin:0}body{background:var(--bg);color:var(--fg);font:14px/1.55 var(--font)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.app{display:grid;grid-template-columns:var(--side) 1fr;height:100%}
.side{background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.brand{padding:16px 16px 10px;border-bottom:1px solid var(--line)}.brand b{display:block;font-size:16px}.brand span{color:var(--muted);font-size:12px}
.search{margin:10px 12px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--sunk);color:var(--fg);font:inherit}
.nav{overflow:auto;padding:0 6px 20px;flex:1}
.sec{margin-top:10px}.sec>.h{display:flex;justify-content:space-between;align-items:center;padding:6px 10px;font-weight:700;font-size:12px;color:var(--muted);cursor:pointer;user-select:none;letter-spacing:.02em}
.sec>.h .n{font-weight:400}.grp>.h{padding:4px 10px 4px 16px;font-size:13px;cursor:pointer;display:flex;justify-content:space-between;color:var(--fg)}
.grp>.h:hover,.it:hover{background:var(--sunk)}.it{display:block;padding:4px 10px 4px 26px;color:var(--fg);border-radius:6px;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.it.cur{background:var(--accent-soft);color:var(--accent);font-weight:700}.it .t{color:var(--muted);font-size:11px;margin-left:6px}
.top{padding:6px 10px}.top .it{padding-left:10px}
main{overflow:auto;min-width:0}.wrap{max-width:1180px;margin:0 auto;padding:24px 28px 80px}
.eyebrow{color:var(--muted);font-size:12px}h1{font-size:24px;margin:2px 0 4px}h2{font-size:17px;margin:26px 0 10px}h3{font-size:14px;margin:16px 0 6px}
.lead{color:var(--muted);margin:0 0 14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}.kpi .v{font-size:24px;font-weight:700}.kpi .k{color:var(--muted);font-size:12px}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;background:var(--sunk);color:var(--muted);margin:0 4px 2px 0}
.chip.warn{background:#b7791f22;color:var(--warn)}.chip.fail,.chip.error{background:#c2413a22;color:var(--fail)}.chip.pass,.chip.ok{background:#1f8a4c22;color:var(--ok)}.chip.info{color:var(--info)}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;font-size:13px}th{color:var(--muted);font-weight:600;cursor:pointer}
code,.mono{font-family:var(--mono);font-size:12.5px}.muted{color:var(--muted)}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:7px;padding:5px 10px;font:inherit;cursor:pointer}.btn:hover{background:var(--sunk)}.btn.on{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
.diag{background:var(--panel);border:1px solid var(--line);border-radius:10px;height:560px;position:relative;overflow:hidden;cursor:grab}
.diag svg{width:100%;height:100%;display:block}.diag .hint{position:absolute;left:10px;bottom:8px;color:var(--muted);font-size:11px;pointer-events:none}
.node rect{fill:var(--panel);stroke:var(--line);stroke-width:1.2}.node .stripe{stroke:none}.node text{fill:var(--fg);font:12px var(--font)}.node .sub{fill:var(--muted);font-size:10.5px}
.node .beh{fill:var(--muted);font-size:10.5px}.node{cursor:pointer}.node:hover rect.box{stroke:var(--accent);stroke-width:2}
.node.future{opacity:.12}.edge{fill:none;stroke-width:1.4;opacity:.75}.edge.dash{stroke-dasharray:5 4}.edge.future{opacity:.05}.elabel{font:10.5px var(--font);fill:var(--muted)}
.edge.hl{stroke-width:2.6;opacity:1}.node.dim{opacity:.25}
.legend span{margin-right:12px;font-size:12px;color:var(--muted)}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}
ul.steps{list-style:none;padding-left:16px;margin:4px 0}ul.steps li{margin:2px 0}ul.steps details>summary{cursor:pointer}
.step-call{color:var(--fg)}.step-branch>summary,.step-if>summary{color:var(--accent)}.k-note{color:var(--warn)}
.beh{border-left:3px solid var(--accent);padding:6px 12px;margin:10px 0;background:var(--panel);border-radius:0 8px 8px 0}
.split{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:900px){.app{grid-template-columns:1fr}.side{max-height:45vh}.split{grid-template-columns:1fr}}
.theme{position:fixed;right:14px;top:10px}
</style></head><body>
<div class="app">
 <nav class="side"><div class="brand"><span id="sub"></span><b id="pname"></b></div>
  <input class="search" id="search" type="search" placeholder="검색: 클래스, 문제, 파일, 맵">
  <div class="nav" id="nav"></div></nav>
 <main id="main"><div class="wrap" id="view"></div></main>
</div>
<button class="btn theme" id="theme" title="밝게/어둡게">◐</button>
<script>
const DATA = __DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const short = k => { const n = String(k).split(/[:/.]/).pop(); return n.endsWith("_C") ? n.slice(0, -2) : n; };
const ROLE_COLOR = {map:"--r-map",gamemode:"--r-mode",gamestate:"--r-mode",gameinstance:"--r-mode",controller:"--r-ctrl",playerstate:"--r-ctrl",
  character:"--r-pawn",pawn:"--r-pawn",hud:"--r-ui",widget:"--r-ui",component:"--r-comp",ability:"--r-gas",effect:"--r-gas",data:"--r-data",ai:"--r-ai"};
const roleColor = r => `var(${ROLE_COLOR[r] || "--r-other"})`;
const EDGE_COLOR = {slot:"--r-mode",possesses:"--r-mode",spawns:"--r-pawn",creates:"--r-pawn",has:"--r-comp",places:"--r-map",uses:"--r-ctrl"};
const STRONG = new Set(["slot","possesses","spawns","creates","has","places","uses"]);
const nodeOf = k => DATA.flow.nodes[k] || {name: short(k), role: (DATA.classes[k]||{}).role || "object", roleWords: (DATA.classes[k]||{}).roleWords || ""};
const sevRank = s => ({fail:3,error:3,warn:2,info:1}[s] || 0);

// ------------------------------------------------------------------ sidebar
const NAV = [];   // {sec, group, label, hash, tag, text}
function buildNav(){
  NAV.push({sec:"top", label:"개요", hash:"#home"});
  if (DATA.changes) NAV.push({sec:"top", label:"이번 세션의 변경 (Live Lens)", hash:"#changes"});
  for (const r of DATA.flow.roots) NAV.push({sec:"flow", group: r.kind === "map" ? "레벨" : r.kind === "gamemode" ? "게임 모드" : "게임 인스턴스",
    label: r.name + (r.default ? " (기본)" : ""), hash:"#flow/" + encodeURIComponent(r.key)});
  const byRole = {};
  for (const [k, c] of Object.entries(DATA.classes)) (byRole[c.roleWords || "객체"] ||= []).push([k, c]);
  for (const [role, list] of Object.entries(byRole).sort((a, b) => b[1].length - a[1].length))
    for (const [k, c] of list.sort((a, b) => a[1].name.localeCompare(b[1].name)))
      NAV.push({sec:"class", group: role, label: short(c.name), hash:"#class/" + encodeURIComponent(k), tag: c.kind === "blueprint" ? "BP" : "C++"});
  const rules = {};
  for (const p of DATA.problems) (rules[p.rule] ||= []).push(p);
  for (const [rule, list] of Object.entries(rules).sort((a, b) => sevRank(b[1][0].severity) - sevRank(a[1][0].severity) || b[1].length - a[1].length))
    NAV.push({sec:"problems", label: (DATA.ruleNames[rule] || rule), hash:"#problems/" + encodeURIComponent(rule), tag: list.length});
  if (DATA.complexity.length) NAV.push({sec:"quality", label:"블루프린트 복잡도", hash:"#quality/complexity", tag: DATA.complexity.length});
  if (DATA.unused.length) NAV.push({sec:"quality", label:"미사용 에셋", hash:"#quality/unused", tag: DATA.unused.length});
  if (DATA.naming.length) NAV.push({sec:"quality", label:"이름 규칙", hash:"#quality/naming", tag: DATA.naming.length});
  for (const f of DATA.files) NAV.push({sec:"files", label: f.name, hash:"#file/" + encodeURIComponent(f.name), tag: f.kind});
  if (DATA.modules.length) NAV.push({sec:"modules", label:"모듈 의존성", hash:"#modules", tag: DATA.modules.filter(m => !m.external).length});
}
const SECS = [["top",""],["flow","게임 흐름"],["class","클래스 (역할별)"],["problems","문제"],["quality","품질"],["files","파일 분석"],["modules","모듈"]];
const openSec = new Set(["flow","problems","files"]), openGrp = new Set();
function renderNav(){
  const q = $("#search").value.trim().toLowerCase();
  let html = "";
  for (const [sec, title] of SECS){
    const items = NAV.filter(n => n.sec === sec && (!q || (n.label + " " + (n.group || "")).toLowerCase().includes(q)));
    if (!items.length) continue;
    if (sec === "top"){ html += `<div class="top">${items.map(itemHtml).join("")}</div>`; continue; }
    const open = q || openSec.has(sec);
    html += `<div class="sec"><div class="h" data-sec="${sec}"><span>${open ? "▾" : "▸"} ${title}</span><span class="n">${items.length}</span></div>`;
    if (open){
      const groups = {};
      for (const it of items) (groups[it.group || ""] ||= []).push(it);
      for (const [g, list] of Object.entries(groups)){
        if (!g){ html += list.slice(0, q ? 200 : 2000).map(itemHtml).join(""); continue; }
        const gopen = q || openGrp.has(sec + "|" + g) || list.length <= 6 && sec !== "class";
        html += `<div class="grp"><div class="h" data-grp="${esc(sec + "|" + g)}"><span>${gopen ? "▾" : "▸"} ${esc(g)}</span><span class="muted">${list.length}</span></div>`;
        if (gopen) html += list.slice(0, 400).map(itemHtml).join("") + (list.length > 400 ? `<div class="it muted">… ${list.length - 400}개 더 (검색으로 좁히세요)</div>` : "");
        html += `</div>`;
      }
    }
    html += `</div>`;
  }
  $("#nav").innerHTML = html || `<div class="it muted">찾는 항목이 없습니다</div>`;
  markCurrent();
}
const itemHtml = n => `<a class="it" href="${n.hash}">${esc(n.label)}${n.tag != null ? `<span class="t">${esc(n.tag)}</span>` : ""}</a>`;
function markCurrent(){ for (const a of document.querySelectorAll(".it")) a.classList.toggle("cur", a.getAttribute("href") === location.hash); }
document.addEventListener("click", e => {
  const s = e.target.closest("[data-sec]"); if (s){ const k = s.dataset.sec; openSec.has(k) ? openSec.delete(k) : openSec.add(k); renderNav(); return; }
  const g = e.target.closest("[data-grp]"); if (g){ const k = g.dataset.grp; openGrp.has(k) ? openGrp.delete(k) : openGrp.add(k); renderNav(); }
});

// ------------------------------------------------------------------ views
function home(){
  const c = DATA.counts, rules = {};
  for (const p of DATA.problems) rules[p.rule] = (rules[p.rule] || 0) + 1;
  const def = DATA.flow.roots.find(r => r.default && r.kind === "map") || DATA.flow.roots[0];
  const busiest = Object.entries(DATA.classes).sort((a, b) => b[1].behaviors.length - a[1].behaviors.length).slice(0, 8);
  return `<div class="eyebrow">DwoLens 분석 · ${esc(DATA.runId)} · ${esc(DATA.source === "live" ? "Live Lens" : "analyze")}</div>
  <h1>${esc(DATA.project)}</h1><p class="lead">UE ${esc(DATA.engine)} · 결과 <span class="chip ${esc(DATA.verdict)}">${esc(DATA.verdict)}</span></p>
  <div class="grid">${[["C++ 클래스", c.classes], ["블루프린트", c.blueprints], ["에셋", c.assets], ["문제", DATA.problems.length], ["흐름 시작점", DATA.flow.roots.length], ["관계", c.relations]]
    .map(([k, v]) => `<div class="panel kpi"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
  <h2>어디부터 볼까</h2><div class="split">
   <div class="panel"><h3>게임이 어떻게 돌아가나</h3>${def ? `<p><a href="#flow/${encodeURIComponent(def.key)}">${esc(def.name)}에서 시작하는 흐름</a> — 맵 → 게임 모드 → 컨트롤러·폰·HUD → 그들이 만들고 쓰는 객체</p>` : "<p class=muted>흐름 데이터가 없습니다</p>"}
   <p class="muted">왼쪽 '게임 흐름'에서 다른 레벨이나 게임 모드를 고르세요.</p></div>
   <div class="panel"><h3>하는 일이 많은 클래스</h3>${busiest.map(([k, c]) => `<div><a href="#class/${encodeURIComponent(k)}">${esc(short(c.name))}</a> <span class="muted">${esc(c.roleWords || "")} · 행동 ${c.behaviors.length}</span></div>`).join("")}</div>
  </div>
  <p class="muted">이전 형식의 한 페이지 리포트(모든 표와 다이어그램): <a href="report-classic.html">report-classic.html</a></p>
  <div class="panel"><h3>문제 요약</h3>${Object.entries(rules).sort((a, b) => b[1] - a[1]).map(([r, n]) => `<a class="chip" href="#problems/${encodeURIComponent(r)}">${esc(DATA.ruleNames[r] || r)} ${n}</a>`).join("") || "<span class=muted>없음</span>"}</div>`;
}

function changesView(){
  const c = DATA.changes, a = c.activity || {};
  const list = (t, xs) => xs && xs.length ? `<h3>${t} (${xs.length})</h3><ul>${xs.slice(0, 200).map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  return `<div class="eyebrow">Live Lens · 세션 ${esc(c.session)}</div><h1>이번 세션의 변경</h1>
  <p class="lead">수집: ${esc(c.collected)} · 종료 지연 ${(+c.exitSeconds || 0).toFixed(2)}초 · 이전 실행 ${esc(c.previousRun || "없음")}</p>
  <div class="grid">${[["저장한 에셋", (a.saved || []).length], ["연 에셋", (a.opened || []).length], ["PIE", a.pieRuns || 0], ["로그 오류", a.logErrors || 0]].map(([k, v]) => `<div class="panel kpi"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
  ${list("추가된 클래스", c.classesAdded)}${list("사라진 클래스", c.classesRemoved)}${list("새 흐름 연결", c.flowAdded)}${list("끊긴 흐름 연결", c.flowRemoved)}
  ${list("저장한 에셋", a.saved)}${list("플레이에서 확인된 클래스", Object.keys(a.inPlay || {}).map(short))}${list("처음 오류", a.firstErrors)}`;
}

// ---- flow diagram
let FLOW = {root:null, depth:3, weak:false, open:new Set(), step:-1, timer:null, view:null};
function flowModel(root){
  const adj = {};
  for (const e of DATA.flow.edges) if (FLOW.weak || STRONG.has(e.kind)) (adj[e.from] ||= []).push(e);
  const level = new Map([[root, 0]]), order = [root], edges = [], groups = {};
  for (let i = 0; i < order.length; i++){
    const k = order[i];
    if (level.get(k) >= FLOW.depth || groups[k]) continue;
    const byKind = {};
    for (const e of adj[k] || []) (byKind[e.kind] ||= []).push(e);
    for (const [kind, list] of Object.entries(byKind)){
      const gid = k + "#" + kind;
      const leaves = list.filter(e => !(adj[e.to] || []).length), through = list.filter(e => (adj[e.to] || []).length);
      let rest = list;
      if (list.length > 6 && leaves.length >= 3 && !FLOW.open.has(gid)){
        groups[gid] = {kind, members: leaves.map(e => e.to), from: k};
        edges.push({from:k, to:gid, kind, labels:[], words: leaves[0].words});
        if (!level.has(gid)){ level.set(gid, level.get(k) + 1); order.push(gid); }
        rest = through;
      }
      for (const e of rest){ edges.push(e); if (!level.has(e.to)){ level.set(e.to, level.get(k) + 1); order.push(e.to); } }
    }
  }
  return {order, level, edges, groups};
}
function layout(m){
  const W = 210, H = 58, GX = 170, GY = 18, cols = {};
  for (const k of m.order) (cols[m.level.get(k)] ||= []).push(k);
  const pos = {};
  for (const [lv, keys] of Object.entries(cols)){
    // keep children near their parents: sort by the parent's row
    if (+lv > 0) keys.sort((a, b) => (parentRow(a) - parentRow(b)));
    const nodeH = k => m.groups[k] ? H + Math.min(6, m.groups[k].members.length) * 14 : H + Math.min(2, behaviorsOf(k).length) * 14;
    let y = 0;
    for (const k of keys){ pos[k] = {x: +lv * (W + GX), y, w: W, h: nodeH(k)}; y += nodeH(k) + GY; }
  }
  function parentRow(k){ const e = m.edges.find(e => e.to === k && pos[e.from]); return e ? pos[e.from].y : 0; }
  return pos;
}
const behaviorsOf = k => (DATA.classes[k] || {}).behaviors ? DATA.classes[k].behaviors.map(b => b.name) : (nodeOf(k).behaviors || []);
function flowView(root){
  if (FLOW.root !== root){
    FLOW = {...FLOW, root, open: new Set(), step: -1, view: null, depth: 3};
    // a first view that can be read: fewer levels when the flow fans out a lot (more is one click away)
    while (FLOW.depth > 1 && flowModel(root).order.length > 45) FLOW.depth--;
  }
  const n = nodeOf(root);
  const maxDepth = FLOW.depth;
  return `<div class="eyebrow">게임 흐름</div><h1>${esc(n.name)}에서 시작</h1>
  <p class="lead">${esc(n.roleWords || "")} · 객체 ${flowModel(root).order.length}개 (깊이 ${FLOW.depth}) — 실선: 만들고 가지고 배치하고 지정하는 관계, 점선: 사용·호출·참조. 노드를 누르면 그 객체를 자세히 봅니다.</p>
  <div class="bar">깊이 ${[1,2,3,4,5,6].map(d => `<button class="btn ${d === maxDepth ? "on" : ""}" data-depth="${d}">${d}</button>`).join("")}
   <button class="btn ${FLOW.weak ? "on" : ""}" id="weak">호출·참조 포함</button>
   <span style="flex:1"></span><button class="btn" id="prev">◀</button><button class="btn" id="play">단계 재생 ▶</button><button class="btn" id="next">▶</button><button class="btn" id="fit">전체 보기</button></div>
  <div class="diag" id="diag"><svg id="svg"></svg><div class="hint">휠: 위아래 · Ctrl+휠: 확대 · 드래그: 이동 · 묶음 노드 클릭: 펼치기 · 노드 클릭: 자세히</div></div>
  <div class="legend bar">${[["map","레벨"],["gamemode","게임 규칙·상태"],["controller","컨트롤러"],["character","캐릭터·폰"],["widget","UI"],["component","컴포넌트"],["ability","GAS"],["data","데이터"],["object","기타"]].map(([r, t]) => `<span><i style="background:${roleColor(r)}"></i>${t}</span>`).join("")}</div>
  <div id="stepnote" class="panel muted">단계 재생: 시작점에서 한 단계씩 어떤 객체로 이어지는지 차례로 보여 줍니다.</div>
  <h2>이 흐름의 연결</h2><div id="edgelist"></div>`;
}
function drawFlow(){
  const svg = $("#svg"); if (!svg) return;
  const m = flowModel(FLOW.root), pos = layout(m);
  const parts = [];
  const defs = `<defs>${Object.entries({...EDGE_COLOR, other:"--r-other"}).map(([k, v]) => `<marker id="ar-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="var(${v})"/></marker>`).join("")}</defs>`;
  m.edges.forEach((e, i) => {
    const a = pos[e.from], b = pos[e.to]; if (!a || !b) return;
    const x1 = a.x + a.w, y1 = a.y + 22, x2 = b.x, y2 = b.y + 22, mx = (x1 + x2) / 2;
    const col = EDGE_COLOR[e.kind] || "--r-other", lv = m.level.get(e.to);
    const label = (e.labels && e.labels[0]) || e.words || "";
    parts.push(`<path class="edge ${STRONG.has(e.kind) && !["uses"].includes(e.kind) ? "" : "dash"}" data-lv="${lv}" data-from="${esc(e.from)}" data-to="${esc(e.to)}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" stroke="var(${col})" marker-end="url(#ar-${EDGE_COLOR[e.kind] ? e.kind : "other"})"/>`);
    if (label) parts.push(`<text class="elabel" data-lv="${lv}" x="${mx}" y="${(y1 + y2) / 2 - 4}" text-anchor="middle">${esc(label.length > 22 ? label.slice(0, 21) + "…" : label)}</text>`);
  });
  for (const k of m.order){
    const p = pos[k], lv = m.level.get(k), g = m.groups[k];
    if (g){
      const words = DATA.flow.edges.find(e => e.kind === g.kind)?.words || g.kind;
      parts.push(`<g class="node" data-lv="${lv}" data-group="${esc(k)}" transform="translate(${p.x},${p.y})"><rect class="box" width="${p.w}" height="${p.h}" rx="8" stroke-dasharray="4 3"/>
       <text x="12" y="20" font-weight="700">${esc(words)} ${g.members.length}개</text><text class="sub" x="12" y="36">눌러서 펼치기</text>
       ${g.members.slice(0, 6).map((mk, i) => `<text class="beh" x="12" y="${54 + i * 14}">· ${esc(nodeOf(mk).name)}</text>`).join("")}</g>`);
      continue;
    }
    const n = nodeOf(k), beh = behaviorsOf(k).slice(0, 2), src = n.source === "bp" ? "BP" : n.source === "cpp" ? "C++" : "";
    parts.push(`<g class="node" data-lv="${lv}" data-key="${esc(k)}" transform="translate(${p.x},${p.y})"><rect class="box" width="${p.w}" height="${p.h}" rx="8"/>
      <rect class="stripe" width="5" height="${p.h}" rx="2" fill="${roleColor(n.role)}"/>
      <text x="14" y="20" font-weight="700">${esc(n.name.length > 26 ? n.name.slice(0, 25) + "…" : n.name)}</text>
      <text class="sub" x="14" y="37">${esc((n.roleWords || "") + (src ? " · " + src : "") + (n.inPlay ? " · 플레이 확인" : ""))}</text>
      ${beh.map((b, i) => `<text class="beh" x="14" y="${54 + i * 14}">하는 일: ${esc(b.length > 24 ? b.slice(0, 23) + "…" : b)}</text>`).join("")}</g>`);
  }
  const xs = Object.values(pos), maxX = Math.max(...xs.map(p => p.x + p.w)) + 20, maxY = Math.max(...xs.map(p => p.y + p.h)) + 20;
  svg.innerHTML = defs + `<g id="world">${parts.join("")}</g>`;
  FLOW.extent = {w: maxX, h: maxY};
  if (!FLOW.view) fit();
  applyView(); applyStep(m);
  $("#edgelist").innerHTML = `<table><tr><th>에서</th><th>관계</th><th>로</th><th>근거</th></tr>${m.edges.filter(e => !m.groups[e.to]).slice(0, 600).map(e =>
    `<tr><td><a href="#class/${encodeURIComponent(e.from)}">${esc(nodeOf(e.from).name)}</a></td><td>${esc(e.words || "지정")}${e.labels && e.labels[0] ? ` <span class="muted">${esc(e.labels[0])}</span>` : ""}</td>
     <td><a href="#class/${encodeURIComponent(e.to)}">${esc(nodeOf(e.to).name)}</a></td><td class="muted">${esc((e.evidence || []).slice(0, 2).join(" · "))}</td></tr>`).join("")}</table>`;
}
function fit(){
  // readable first: fit the width (never smaller than 0.75), start at the top; tall flows scroll with the wheel
  const d = $("#diag").getBoundingClientRect(), e = FLOW.extent;
  const s = Math.min(1.1, Math.max(0.75, Math.min(d.width / e.w, d.height / e.h)));
  FLOW.view = {s, x: 12, y: e.h * s < d.height ? (d.height - e.h * s) / 2 : 12};
}
function applyView(){ const w = $("#world"); if (w && FLOW.view) w.setAttribute("transform", `translate(${FLOW.view.x},${FLOW.view.y}) scale(${FLOW.view.s})`); }
function applyStep(m){
  const max = Math.max(...m.order.map(k => m.level.get(k)));
  for (const el of document.querySelectorAll("#svg [data-lv]")) el.classList.toggle("future", FLOW.step >= 0 && +el.dataset.lv > FLOW.step);
  const note = $("#stepnote");
  if (FLOW.step < 0){ note.textContent = "단계 재생: 시작점에서 한 단계씩 어떤 객체로 이어지는지 차례로 보여 줍니다."; return; }
  const here = m.order.filter(k => m.level.get(k) === FLOW.step && !m.groups[k]).map(k => nodeOf(k).name);
  note.innerHTML = `<b>${FLOW.step}단계</b> (${FLOW.step}/${max}) — ${FLOW.step === 0 ? "시작점" : esc(here.slice(0, 12).join(", ")) + (here.length > 12 ? ` 외 ${here.length - 12}개` : "")}`;
}
function wireFlow(){
  const diag = $("#diag"); if (!diag) return;
  for (const b of document.querySelectorAll("[data-depth]")) b.onclick = () => { FLOW.depth = +b.dataset.depth; FLOW.view = null; render(); };
  $("#weak").onclick = () => { FLOW.weak = !FLOW.weak; FLOW.view = null; render(); };
  const m = () => flowModel(FLOW.root), maxLv = () => { const mm = m(); return Math.max(...mm.order.map(k => mm.level.get(k))); };
  $("#next").onclick = () => { FLOW.step = Math.min(maxLv(), FLOW.step + 1); applyStep(m()); };
  $("#prev").onclick = () => { FLOW.step = Math.max(-1, FLOW.step - 1); applyStep(m()); };
  $("#fit").onclick = () => { fit(); applyView(); };
  $("#play").onclick = () => {
    clearInterval(FLOW.timer); FLOW.step = 0; applyStep(m());
    FLOW.timer = setInterval(() => { if (FLOW.step >= maxLv()) { clearInterval(FLOW.timer); return; } FLOW.step++; applyStep(m()); }, 1100);
  };
  let drag = null;
  diag.onmousedown = e => { drag = {x: e.clientX, y: e.clientY, vx: FLOW.view.x, vy: FLOW.view.y, moved: false}; diag.style.cursor = "grabbing"; };
  window.onmousemove = e => { if (!drag) return; const dx = e.clientX - drag.x, dy = e.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true; FLOW.view.x = drag.vx + dx; FLOW.view.y = drag.vy + dy; applyView(); };
  window.onmouseup = e => {
    if (drag && !drag.moved){
      const g = e.target.closest && e.target.closest(".node");
      if (g && g.dataset.group){ FLOW.open.add(g.dataset.group); drawFlow(); }
      else if (g && g.dataset.key) location.hash = "#class/" + encodeURIComponent(g.dataset.key);
    }
    drag = null; diag.style.cursor = "grab";
  };
  diag.onwheel = e => { e.preventDefault();
    if (!e.ctrlKey){ FLOW.view.y -= e.deltaY; FLOW.view.x -= e.deltaX; applyView(); return; }
    const r = diag.getBoundingClientRect(), k = e.deltaY < 0 ? 1.12 : 1 / 1.12, s = Math.min(2.5, Math.max(0.15, FLOW.view.s * k));
    const px = e.clientX - r.left, py = e.clientY - r.top; FLOW.view.x = px - (px - FLOW.view.x) * s / FLOW.view.s; FLOW.view.y = py - (py - FLOW.view.y) * s / FLOW.view.s; FLOW.view.s = s; applyView(); };
  diag.onmouseover = e => { const g = e.target.closest && e.target.closest(".node[data-key]"); if (!g) return; const k = g.dataset.key;
    for (const p of document.querySelectorAll("#svg .edge")) p.classList.toggle("hl", p.dataset.from === k || p.dataset.to === k); };
  drawFlow();
}

// ---- class
function stepsHtml(steps){
  if (!steps || !steps.length) return "";
  return `<ul class="steps">${steps.map(s => {
    const target = s.target && DATA.classes[s.target] ? ` <a href="#class/${encodeURIComponent(s.target)}">↗</a>` : "";
    const text = `<span class="${s.kind === "note" ? "k-note" : ""}">${esc(s.text)}</span>${target}`;
    const kids = (s.branches || []).map(b => `<li class="step-branch"><details open><summary>${esc(b.label)}</summary>${stepsHtml(b.steps)}</details></li>`).join("") + (s.steps ? stepsHtml(s.steps) : "");
    return kids ? `<li class="step-if"><details open><summary>${text}</summary><ul class="steps">${kids}</ul></details></li>` : `<li>${text}</li>`;
  }).join("")}</ul>`;
}
function classView(key){
  const c = DATA.classes[key], n = nodeOf(key);
  if (!c && !DATA.flow.nodes[key]) return `<h1>찾을 수 없음</h1><p class=muted>${esc(key)}</p>`;
  const cls = c || {behaviors: [], features: []};
  const out = DATA.flow.edges.filter(e => e.from === key), inn = DATA.flow.edges.filter(e => e.to === key);
  const featById = Object.fromEntries((cls.features || []).map(f => [f.id, f]));
  const allFeat = id => featById[id] || Object.values(DATA.classes).flatMap(x => x.features || []).find(f => f.id === id);
  const file = DATA.files.find(f => key.includes("/" + f.name + "."));
  const edgeRow = (e, other) => `<div><a href="#class/${encodeURIComponent(other)}">${esc(nodeOf(other).name)}</a> <span class="muted">${esc(e.words || "지정")}${e.labels && e.labels[0] ? " · " + esc(e.labels[0]) : ""}</span></div>`;
  return `<div class="eyebrow">${esc(cls.kind === "blueprint" ? "블루프린트" : "C++ 클래스")}${key.startsWith("map:") ? "레벨" : ""}</div><h1>${esc(n.name || short(key))}</h1>
  <p class="lead"><span class="chip" style="color:${roleColor(n.role)}">${esc(n.roleWords || cls.roleWords || "")}</span>${(cls.chain || n.chain || []).length ? "부모: " + esc((cls.chain || n.chain).join(" < ")) : ""}
   ${cls.inPlay ? ` <span class="chip pass">플레이에서 확인 ${cls.inPlay.sessions}세션</span>` : ""}<br><span class="mono muted">${esc(cls.path || n.path || key)}</span></p>
  <div class="bar"><a class="btn" href="#flow/${encodeURIComponent(key)}">이 객체에서 흐름 보기</a>${file ? `<a class="btn" href="#file/${encodeURIComponent(file.name)}">파일 분석 보기</a>` : ""}</div>
  <div class="split"><div class="panel"><h3>이 객체가 (${out.length})</h3>${out.slice(0, 80).map(e => edgeRow(e, e.to)).join("") || "<span class=muted>없음</span>"}</div>
  <div class="panel"><h3>이 객체를 (${inn.length})</h3>${inn.slice(0, 80).map(e => edgeRow(e, e.from)).join("") || "<span class=muted>없음</span>"}</div></div>
  <h2>하는 일 (${(cls.behaviors || []).length})</h2>
  ${(cls.behaviors || []).map(b => `<div class="beh"><b>${esc(b.name)}</b> <span class="muted">${esc(b.nameEn || "")}${b.primary ? " · 핵심 " + esc(b.primary) : ""}</span>
     ${(b.cases || []).length ? `<div>${b.cases.slice(0, 8).map(cs => `<div><span class="muted">${esc(cs.when)}</span> → ${esc((cs.does || []).join(", "))}</div>`).join("")}</div>` : ""}
     ${(b.features || []).map(id => { const f = allFeat(id); return f ? `<details><summary>${esc(f.name)} <span class="muted">${esc(f.trigger || "")} · ${esc(f.source === "bp" ? "BP" : "C++")}</span></summary>${stepsHtml(f.steps)}</details>` : ""; }).join("")}</div>`).join("")
   || (cls.features || []).map(f => `<div class="beh"><details><summary><b>${esc(f.name)}</b> <span class="muted">${esc(f.trigger || "")}</span></summary>${stepsHtml(f.steps)}</details></div>`).join("")
   || "<p class=muted>진입점이 없는 클래스입니다 (데이터·설정 전용이거나 다른 클래스가 호출하는 함수만 있음).</p>"}`;
}

// ---- problems, quality, files, modules
function problemsView(rule){
  const list = DATA.problems.filter(p => p.rule === rule);
  return `<div class="eyebrow">문제</div><h1>${esc(DATA.ruleNames[rule] || rule)}</h1><p class="lead">${list.length}건</p>
  ${list.map(p => `<div class="panel"><span class="chip ${esc(p.severity)}">${esc(p.severity)}</span> ${esc(p.message)}
   ${p.items.length ? `<details ${p.items.length <= 8 ? "open" : ""}><summary class="muted">대상 ${p.items.length + p.more}개</summary><ul>${p.items.map(i => `<li class="mono">${esc(i)}</li>`).join("")}</ul>${p.more ? `<p class=muted>… ${p.more}개 더</p>` : ""}</details>` : ""}</div>`).join("")}`;
}
function table(rows, cols){
  if (!rows.length) return "<p class=muted>없음</p>";
  return `<input class="search" style="margin:0 0 8px;width:280px" placeholder="표 안에서 찾기" oninput="filterTable(this)"><table class="data"><tr>${cols.map(c => `<th>${esc(c[1])}</th>`).join("")}</tr>${rows.map(r => `<tr>${cols.map(c => `<td>${esc(typeof c[0] === "function" ? c[0](r) : (r[c[0]] ?? ""))}</td>`).join("")}</tr>`).join("")}</table>`;
}
window.filterTable = inp => { const q = inp.value.toLowerCase(); for (const tr of inp.nextElementSibling.querySelectorAll("tr:not(:first-child)")) tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none"; };
function qualityView(kind){
  if (kind === "complexity") return `<h1>블루프린트 복잡도</h1><p class="lead">노드 수가 많은 그래프부터</p>` + table(DATA.complexity, [[r => short(r.blueprint || r.bp || r.asset || r.key || ""), "블루프린트"], ["graph", "그래프"], ["nodes", "노드"], ["links", "연결"]]);
  if (kind === "unused") return `<h1>미사용 에셋</h1><p class="lead">어디에서도 참조되지 않는 에셋</p>` + table(DATA.unused.map(u => typeof u === "string" ? {a: u} : u), [[r => r.a || r.asset || r.package || JSON.stringify(r), "에셋"]]);
  return `<h1>이름 규칙</h1><p class="lead">프로젝트 규칙(config)에 맞지 않는 이름</p>` + table(DATA.naming, [[r => r.asset || r.name || "", "대상"], [r => short(r.class || ""), "종류"], [r => (r.problems || [r.expected || r.message || ""]).join(", "), "문제"]]);
}
function fileView(name){
  const d = DATA.files.find(f => f.name === name); if (!d) return "<h1>없음</h1>";
  const steps = ss => !ss || !ss.length ? "" : `<ul class="steps">${ss.map(s => s.children && s.children.length
      ? `<li><details ${["if","else","on","loop"].includes(s.kind) ? "open" : ""}><summary class="${s.kind === "note" ? "k-note" : ""}">${esc(s.text)}</summary>${s.cpp ? `<pre class="mono panel">${esc((s.cpp.code || []).join("\n"))}</pre>` : ""}${steps(s.children)}</details></li>`
      : `<li class="${s.kind === "note" ? "k-note" : ""}" title="${esc(s.detail || "")}">${esc(s.text)}${s.cpp ? ` <details style="display:inline"><summary class="muted">C++</summary><pre class="mono panel">${esc((s.cpp.code || []).join("\n"))}</pre></details>` : ""}</li>`).join("")}</ul>`;
  const c = d.composition || {};
  return `<div class="eyebrow">파일 분석 · ${esc(d.package)}</div><h1>${esc(d.name)}</h1><p class="lead">${esc(d.kind)} · ${esc((d.parentChain || []).join(" < "))}</p>
  <div class="panel">${esc(d.summary)}</div>
  <h2>구성</h2>${table(c.components || [], [["name", "이름"], ["role", "역할"], ["class", "클래스"], ["parent", "붙은 곳"]])}
  ${(c.settings || []).length ? "<h3>설정값</h3>" + table(c.settings, [["name", "이름"], ["type", "타입"], ["default", "기본값"], [r => (r.changedBy || []).join(", "), "바꾸는 행동"]]) : ""}
  ${(c.state || []).length ? "<h3>상태</h3>" + table(c.state, [["name", "이름"], ["type", "타입"], ["default", "기본값"], [r => (r.changedBy || []).join(", "), "바꾸는 행동"]]) : ""}
  <h2>행동과 흐름</h2>${(d.behaviors || []).map(b => `<div class="beh"><b>${esc(b.title)}</b><div class="muted">${esc(b.trigger)}</div>${(b.notes || []).map(x => `<div class="k-note">“${esc(x)}”</div>`).join("")}${steps(b.steps)}</div>`).join("")}`;
}
function modulesView(){
  const mods = DATA.modules.filter(m => !m.external);
  return `<h1>모듈 의존성</h1><p class="lead">프로젝트 모듈과 그 모듈이 쓰는 모듈</p>` + table(mods, [["name", "모듈"], [m => m.deps.map(short).join(", "), "의존"]]);
}

// ------------------------------------------------------------------ routing
function render(){
  const h = decodeURIComponent(location.hash || "#home"), [, kind, ...rest] = h.match(/^#([^/]+)\/?(.*)$/) || [null, "home"], arg = rest.join("/");
  const view = $("#view");
  clearInterval(FLOW.timer);
  if (kind === "flow") { view.innerHTML = flowView(arg); wireFlow(); }
  else if (kind === "class") view.innerHTML = classView(arg);
  else if (kind === "problems") view.innerHTML = problemsView(arg);
  else if (kind === "quality") view.innerHTML = qualityView(arg);
  else if (kind === "file") view.innerHTML = fileView(arg);
  else if (kind === "modules") view.innerHTML = modulesView();
  else if (kind === "changes" && DATA.changes) view.innerHTML = changesView();
  else view.innerHTML = home();
  markCurrent();
  $("#main").scrollTop = 0;
}
$("#pname").textContent = DATA.project;
$("#sub").textContent = `DwoLens · UE ${DATA.engine} · ${DATA.runId}`;
$("#search").oninput = renderNav;
$("#search").onkeydown = e => { if (e.key === "Enter"){ const a = document.querySelector(".nav .it[href]"); if (a) location.hash = a.getAttribute("href"); } };
$("#theme").onclick = () => { const r = document.documentElement; const dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; r.dataset.theme = dark ? "light" : "dark"; };
window.onhashchange = render;
buildNav(); renderNav(); render();
</script></body></html>
"""


def write(out: Path, project: Path | None) -> Path:
    data = build_data(out, project)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = f"{data['project']} · DwoLens 리포트"
    html = TEMPLATE.replace("__TITLE__", title.replace("<", "")).replace("__DATA__", payload)
    path = out / "report.html"
    path.write_text(html, encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--project", type=Path)
    args = ap.parse_args(argv)
    path = write(args.out, args.project)
    print(f"lens_report: {path} ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
