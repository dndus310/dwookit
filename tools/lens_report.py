"""DwoLens interactive report: one self-contained HTML file to explore an analysis run.

Instead of one long page with every table and diagram, the report is an app: a sidebar to pick what to look at
(summary, problems by rule, classes by role, game flow by level or GameMode, quality lists, tracked files,
modules) with search, and a view for the picked item.

Design (interactive report practice: parameters, cascading filters, linkage, drill-down):
- The summary states the conclusion first: verdict, counts by severity, change against the previous analysis,
  then the problems to look at first, each with its fix. Every number links to the filtered list behind it.
- Problem and class lists are filtered by parameters kept in the address (#classes?role=..&top=..&sub=..), so a
  view can be bookmarked; the folder filter cascades (top folder -> sub folder); columns sort.
- Each problem says what, where, why it matters and how to fix it (RULE_INFO), and whether it is new.
- Diagrams are drawn only for what is picked, at a chosen depth, with leaf objects grouped and a step-by-step
  playback; clicking a node fills a detail panel below (linkage), double-click opens the class.
No network: no external fonts or libraries.

    python lens_report.py --out <run>/out [--project <dir>]      -> <run>/out/report.html

lens_analyze.py calls it after every analysis; the earlier single-page report stays as report-classic.html.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# what each rule means for the project and what to do about it: the report shows these next to every problem
RULE_INFO = {
    "tick-usage": {"name": "Tick 사용", "why": "매 프레임 실행되어 액터 수만큼 CPU 시간이 늘어납니다.",
                   "fix": "타이머나 이벤트로 바꾸거나, 필요한 동안만 Tick을 켭니다 (SetActorTickEnabled)."},
    "asset-hard-cycle": {"name": "에셋 하드 참조 순환", "why": "순환에 묶인 에셋은 하나만 로드해도 모두 메모리에 올라옵니다.",
                         "fix": "순환의 한쪽 참조를 소프트 참조(TSoftObjectPtr)나 인터페이스로 바꿉니다."},
    "hard-ref-chain": {"name": "하드 참조 체인", "why": "에셋 하나를 로드할 때 함께 로드되는 에셋이 많아 로딩 시간과 메모리가 늘어납니다.",
                       "fix": "체인 중간의 큰 에셋 참조를 소프트 참조로 바꾸고 필요할 때 비동기로 로드합니다."},
    "replication": {"name": "복제 프로퍼티", "why": "복제 프로퍼티가 많으면 네트워크 대역폭과 서버 CPU를 씁니다.",
                    "fix": "클라이언트에 필요 없는 프로퍼티는 Replicated를 끄고, 필요한 것은 COND_ 조건으로 줄입니다."},
    "naming": {"name": "이름 규칙", "why": "규칙과 다른 이름은 검색과 정리를 어렵게 합니다.",
               "fix": "규칙대로 이름을 바꿉니다. 규칙은 config의 naming 항목에서 바꿀 수 있습니다."},
    "redirector": {"name": "남은 리디렉터", "why": "이동·이름 변경 뒤 남은 리디렉터는 로드를 느리게 하고 머지 충돌을 만듭니다.",
                   "fix": "콘텐츠 브라우저에서 폴더를 우클릭해 Fix Up Redirectors를 실행합니다."},
    "unused-asset": {"name": "미사용 에셋", "why": "맵·설정에서 도달할 수 없는 에셋은 저장소와 쿠킹 시간을 늘립니다.",
                     "fix": "코드나 데이터에서 경로 문자열로 불러오는지 확인한 뒤 삭제합니다."},
    "bp-complexity": {"name": "블루프린트 복잡도", "why": "노드가 많은 그래프는 읽기 어렵고 수정할 때 실수가 잦습니다.",
                      "fix": "함수·매크로·컴포넌트로 나누거나, 반복 계산은 C++로 옮깁니다."},
    "cycle": {"name": "클래스 순환 참조", "why": "서로 참조하는 클래스는 따로 바꾸거나 테스트하기 어렵습니다.",
              "fix": "한쪽 의존을 인터페이스나 델리게이트로 바꿉니다."},
    "module-cycle": {"name": "모듈 순환", "why": "모듈끼리 서로 의존하면 빌드 순서가 꼬이고 분리가 불가능합니다.",
                     "fix": "공통 부분을 별도 모듈로 빼거나 의존 방향을 한쪽으로 정리합니다."},
    "unknown-relations": {"name": "분류하지 못한 관계", "why": "분석기가 모르는 관계라 흐름·영향 분석에서 빠집니다.",
                          "fix": "대부분 조치가 필요 없습니다. 자주 나오면 DwoLens 이슈로 알려 주세요."},
}
RULE_NAMES = {k: v["name"] for k, v in RULE_INFO.items()}


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def folder_of(key: str, path: str) -> str:
    """Content folder of a class: /Game/Audio/Blueprints for a blueprint, /Script/LyraGame for a C++ class."""
    if path:
        return path.rsplit("/", 1)[0]
    body = key.split(":", 1)[-1]
    return body.rsplit(".", 1)[0] if body.startswith("/Script/") else body.rsplit("/", 1)[0]


def item_text(x) -> str:
    """Problem targets come as paths or as small records (blueprint, graph, node); show them as one line."""
    if isinstance(x, dict):
        head = x.get("blueprint") or x.get("asset") or x.get("class") or x.get("name") or ""
        rest = [str(v) for k, v in x.items() if k not in ("blueprint", "asset", "class", "name", "node") and v not in (None, "")]
        return head + (" · " + " · ".join(rest) if rest else "")
    return str(x)


def class_of(x, by_path: dict, by_name: dict) -> str:
    if isinstance(x, dict):
        name = x.get("blueprint") or x.get("class") or ""
        return by_name.get(name, "") or by_path.get(x.get("asset", ""), "")
    s = str(x).split(" ")[0].split(":")[0]
    return by_path.get(s, "") or by_path.get(s.rsplit(".", 1)[0], "")


def problem_key(p: dict, single: bool) -> str:
    """Same problem across runs: rules with one aggregate finding match by rule; others by rule and first target."""
    if single:
        return p["rule"]
    return p["rule"] + "|" + (p["items"][0] if p["items"] else "".join(ch for ch in p["message"] if not ch.isdigit()))


def keyed(problems: list) -> dict:
    count = {}
    for p in problems:
        count[p["rule"]] = count.get(p["rule"], 0) + 1
    return {problem_key(p, count[p["rule"]] == 1): p for p in problems}


def compare_previous(out: Path, problems: list):
    """Mark each problem new or same against the latest earlier analysis run, and list the fixed ones."""
    runs = out.parent.parent
    me = out.parent.name
    earlier = sorted((d for d in runs.glob("run-*") if d.name < me and (d / "out" / "findings.json").is_file()), key=lambda d: d.name)
    if not earlier:
        return None
    prev = earlier[-1]
    old = []
    for f in (load(prev / "out" / "findings.json", {}) or {}).get("findings", []):
        old.append({"rule": f.get("rule", ""), "message": f.get("message", ""), "items": [item_text(x) for x in (f.get("items") or [])[:1]]})
    old_keys, new_keys = keyed(old), keyed(problems)
    for k, p in new_keys.items():
        p["change"] = "same" if k in old_keys else "new"
    fixed = [{"rule": p["rule"], "message": p["message"]} for k, p in old_keys.items() if k not in new_keys]
    return {"previousRun": prev.name, "new": sum(1 for p in problems if p.get("change") == "new"), "fixed": len(fixed), "fixedList": fixed[:200]}


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
        c["folder"] = folder_of(key, c["path"])
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

    by_path = {c["path"]: k for k, c in classes.items() if c.get("path")}
    by_name = {}
    for k, c in classes.items():
        by_name.setdefault(str(c.get("name") or "").removesuffix("_C"), k)
    problems = []
    for f in findings.get("findings", []):
        items = [item_text(x) for x in (f.get("items") or [])]
        raw = (f.get("items") or [])[:300]
        keys = [class_of(x, by_path, by_name) for x in raw]
        problems.append({"rule": f.get("rule", ""), "severity": f.get("severity", "info"), "message": f.get("message", ""),
                         "items": items[:300], "keys": keys, "more": max(0, len(items) - 300)})
    compare = compare_previous(out, problems)

    graphs = (quality.get("complexity") or {}).get("graphs", [])
    complexity = sorted(graphs, key=lambda g: (not g.get("over"), -(g.get("nodes") or 0)))[:300]   # over the limits first
    unused_all = (quality.get("unused") or {}).get("unused", [])
    naming_all = (quality.get("naming") or {}).get("violations", [])
    unused, naming = unused_all[:1000], naming_all[:1000]

    files = []
    if project:
        root = project / "__dwookit" / "blueprints"
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
        "ruleNames": RULE_NAMES, "rules": RULE_INFO, "compare": compare, "categories": features.get("categories", []),
        "flow": {"roots": flow.get("roots", []), "nodes": flow_nodes, "edges": flow.get("edges", []), "unreached": flow.get("unreached", [])},
        "classes": classes, "problems": problems, "complexity": complexity, "unused": unused, "naming": naming,
        "modules": modules, "files": files, "changes": changes,
        "totals": {"unused": len(unused_all), "naming": len(naming_all), "graphs": len(graphs),
                   "graphsOver": sum(1 for g in graphs if g.get("over"))},
    }


# shared by the analysis report and the verify / logs / smoke reports (lens_report_runs.py):
# escaping, routing with parameters in the address, filters, sortable tables, bars, KPI cards
JS_COMMON = r"""const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
// ------------------------------------------------------------------ routing: #view/arg?param=value
function route(){
  const h = location.hash || "#home";
  const [path, query] = h.slice(1).split("?");
  const [kind, ...rest] = path.split("/");
  const params = {};
  for (const kv of (query || "").split("&")) if (kv){ const [k, v] = kv.split("="); params[decodeURIComponent(k)] = decodeURIComponent(v || ""); }
  return {kind: kind || "home", arg: decodeURIComponent(rest.join("/")), params};
}
function link(kind, arg, params){
  const q = Object.entries(params || {}).filter(([, v]) => v !== "" && v != null).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
  return "#" + kind + (arg ? "/" + encodeURIComponent(arg) : "") + (q ? "?" + q : "");
}
function setParams(values){
  const r = route(), p = {...r.params, ...values};
  if ("top" in values) delete p.sub;                     // cascading: a new top folder resets the sub folder
  location.hash = link(r.kind, r.arg, p);
}
const setParam = (name, value) => setParams({[name]: value});
window.setParam = setParam; window.setParams = setParams;

// ------------------------------------------------------------------ components
function select(name, label, options, value){
  return `<label class="fl">${esc(label)} <select onchange="setParam('${name}', this.value)"><option value="">전체</option>${options.map(([v, t]) =>
    `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`;
}
function textFilter(name, label, value){
  return `<label class="fl">${esc(label)} <input type="search" value="${esc(value || "")}" onchange="setParam('${name}', this.value)" placeholder="포함하는 글자"></label>`;
}
// sortable table: cols [{key, label, num, fmt, link}]; sort state lives in the address (?sort=key&dir=desc)
function dataTable(rows, cols, params, empty){
  const sort = params.sort, dir = params.dir === "asc" ? 1 : -1;
  if (sort){ const c = cols.find(c => c.key === sort); if (c) rows = [...rows].sort((a, b) => (c.num ? (a[sort] - b[sort]) : String(a[sort]).localeCompare(String(b[sort]))) * dir); }
  if (!rows.length) return `<p class="muted">${esc(empty || "해당 항목 없음")}</p>`;
  const head = cols.map(c => `<th onclick="setParams({sort: '${c.key}', dir: '${sort === c.key && dir === -1 ? "asc" : "desc"}'})" title="눌러서 정렬">${esc(c.label)}${sort === c.key ? (dir === -1 ? " ▼" : " ▲") : ""}</th>`).join("");
  const shown = rows.slice(0, 1000);
  return `<table class="data"><tr>${head}</tr>${shown.map(r => `<tr>${cols.map(c => {
      const v = c.fmt ? c.fmt(r) : esc(r[c.key]);
      return `<td class="${c.num ? "num" : ""}">${c.link ? `<a href="${c.link(r)}">${v}</a>` : v}</td>`;
    }).join("")}</tr>`).join("")}</table>${rows.length > 1000 ? `<p class="muted">처음 1,000개만 표시. 필터로 좁히세요.</p>` : ""}`;
}
function bars(pairs, unit){
  const max = Math.max(1, ...pairs.map(p => p[1]));
  return `<div class="bars">${pairs.map(([label, value, href, color]) => `<a class="barrow" href="${href}" title="${esc(label)}: ${value}${unit}"><span class="lbl">${esc(label)}</span>
    <i style="width:${Math.max(3, 260 * value / max)}px;${color ? "background:" + color : ""}"></i><span class="muted">${value}${unit}</span></a>`).join("")}</div>`;
}
const kpi = (label, value, sub, href, cls) => `<a class="panel kpi card ${cls || ""}" href="${href}"><div class="k">${esc(label)}</div><div class="v">${value}</div><div class="muted">${sub || ""}</div></a>`;
"""


CSS = r""":root{--bg:#f3f5f8;--panel:#fff;--sunk:#e9edf3;--fg:#18202c;--muted:#5a6577;--line:#d3dae4;--accent:#2f5bd3;--accent-soft:#e3eafb;
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
.chip{display:inline-block;white-space:nowrap;padding:1px 8px;border-radius:10px;font-size:12px;background:var(--sunk);color:var(--muted);margin:0 4px 2px 0}
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
.conclusion{font-size:15px;border-left:4px solid var(--accent)}.panel.warn{border-left:4px solid var(--warn)}
.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0;padding:10px 12px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
.fl{font-size:12px;color:var(--muted);display:flex;gap:6px;align-items:center}
.fl select,.fl input{font:13px var(--font);color:var(--fg);background:var(--sunk);border:1px solid var(--line);border-radius:6px;padding:4px 6px;max-width:260px}
table.data td{overflow-wrap:anywhere}table.data th{white-space:nowrap}table.data td.num{white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}table.data th:hover{color:var(--accent)}
a.card{display:block;color:var(--fg);text-decoration:none}a.card:hover{border-color:var(--accent);text-decoration:none}.kpi.warn .v{color:var(--warn)}
.bars .barrow{display:flex;align-items:center;gap:8px;margin:2px 0;padding:2px 4px;border-radius:6px;color:var(--fg)}.bars .barrow:hover{background:var(--sunk);text-decoration:none}
.bars .barrow i{display:inline-block;height:10px;border-radius:3px;background:var(--accent)}.bars .barrow .lbl{width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
.node.sel rect.box{stroke:var(--accent);stroke-width:2.5}table.cases td{font-size:12.5px;padding:3px 8px}table.cases td:first-child{white-space:nowrap}.bars .barrow .muted{white-space:nowrap}
"""

TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
__CSS__</style></head><body>
<div class="app">
 <nav class="side"><div class="brand"><span id="sub"></span><b id="pname"></b></div>
  <input class="search" id="search" type="search" placeholder="검색: 클래스, 문제, 파일, 맵">
  <div class="nav" id="nav"></div></nav>
 <main id="main"><div class="wrap" id="view"></div></main>
</div>
<button class="btn theme" id="theme" title="밝게/어둡게">◐</button>
<script>
const DATA = __DATA__;
__COMMON__
const short = k => { const n = String(k).split(/[:/.]/).pop(); return n.endsWith("_C") ? n.slice(0, -2) : n; };
const ROLE_COLOR = {map:"--r-map",gamemode:"--r-mode",gamestate:"--r-mode",gameinstance:"--r-mode",controller:"--r-ctrl",playerstate:"--r-ctrl",
  character:"--r-pawn",pawn:"--r-pawn",hud:"--r-ui",widget:"--r-ui",component:"--r-comp",ability:"--r-gas",effect:"--r-gas",data:"--r-data",ai:"--r-ai"};
const roleColor = r => `var(${ROLE_COLOR[r] || "--r-other"})`;
const EDGE_COLOR = {slot:"--r-mode",possesses:"--r-mode",spawns:"--r-pawn",creates:"--r-pawn",has:"--r-comp",places:"--r-map",uses:"--r-ctrl"};
const STRONG = new Set(["slot","possesses","spawns","creates","has","places","uses"]);
const SOLID = new Set(["slot","possesses","spawns","creates","has","places"]);
const SEV_NAME = {fail:"실패", error:"오류", warn:"경고", info:"정보"};
const sevRank = s => ({fail:3,error:3,warn:2,info:1}[s] || 0);
const sevChip = s => `<span class="chip ${esc(s)}">${esc(SEV_NAME[s] || s)}</span>`;
const nodeOf = k => DATA.flow.nodes[k] || {name: short(k), role: (DATA.classes[k]||{}).role || "object", roleWords: (DATA.classes[k]||{}).roleWords || ""};
const ruleInfo = r => DATA.rules[r] || {name: r, why: "", fix: ""};
const EDGE_WORDS = {slot:"지정", possesses:"조종", spawns:"생성 (Spawn)", creates:"만들기", has:"가짐 (컴포넌트)", places:"배치", uses:"사용", refers:"참조", calls:"호출", casts:"형변환해 사용"};
const edgeWord = e => e.words || EDGE_WORDS[e.kind] || e.kind;
const OUT = {}, IN = {};
for (const e of DATA.flow.edges){ (OUT[e.from] ||= []).push(e); (IN[e.to] ||= []).push(e); }
const CLASS_LIST = Object.entries(DATA.classes).map(([k, c]) => ({key: k, name: short(c.name), role: c.role || "object", roleWords: c.roleWords || "기타",
  kind: c.kind === "blueprint" ? "BP" : "C++", folder: c.folder || "", top: (c.folder || "").split("/").slice(0, 3).join("/"),
  behaviors: (c.behaviors || []).length, out: (OUT[k] || []).length, in: (IN[k] || []).length, inPlay: c.inPlay ? c.inPlay.sessions : 0}));


// ------------------------------------------------------------------ sidebar
const NAV = [];
function buildNav(){
  NAV.push({sec:"top", label:"요약", hash:"#home"});
  if (DATA.changes) NAV.push({sec:"top", label:"이번 세션 변경 (Live Lens)", hash:"#changes"});
  NAV.push({sec:"top", label:"문제 전체", hash:"#problems", tag: DATA.problems.length + "건"});
  NAV.push({sec:"top", label:"클래스 전체", hash:"#classes", tag: CLASS_LIST.length + "개"});
  const rules = {};
  for (const p of DATA.problems) (rules[p.rule] ||= []).push(p);
  for (const [rule, list] of Object.entries(rules).sort((a, b) => sevRank(b[1][0].severity) - sevRank(a[1][0].severity) || b[1].length - a[1].length))
    NAV.push({sec:"problems", label: ruleInfo(rule).name, hash: link("problems", "", {rule}), tag: list.length + "건"});
  const roles = {};
  for (const c of CLASS_LIST) roles[c.role] = roles[c.role] || {words: c.roleWords, n: 0}, roles[c.role].n++;
  for (const [role, r] of Object.entries(roles).sort((a, b) => b[1].n - a[1].n))
    NAV.push({sec:"class", label: r.words, hash: link("classes", "", {role}), tag: r.n + "개"});
  for (const c of CLASS_LIST) NAV.push({sec:"class", label: c.name, hash: link("class", c.key), tag: c.kind, searchOnly: true});
  for (const r of DATA.flow.roots) NAV.push({sec:"flow", group: r.kind === "map" ? "레벨" : r.kind === "gamemode" ? "게임 모드" : "게임 인스턴스",
    label: r.name + (r.default ? " (기본)" : ""), hash: link("flow", r.key)});
  if (DATA.complexity.length) NAV.push({sec:"quality", label:"블루프린트 복잡도", hash:"#quality/complexity", tag: `기준 초과 ${DATA.totals.graphsOver}개`});
  if (DATA.unused.length) NAV.push({sec:"quality", label:"미사용 에셋", hash:"#quality/unused", tag: DATA.totals.unused + "개"});
  if (DATA.naming.length) NAV.push({sec:"quality", label:"이름 규칙 위반", hash:"#quality/naming", tag: DATA.totals.naming + "개"});
  for (const f of DATA.files) NAV.push({sec:"files", label: f.name, hash: link("file", f.name), tag: f.kind});
  if (DATA.modules.some(m => !m.external)) NAV.push({sec:"modules", label:"모듈 의존성", hash:"#modules", tag: DATA.modules.filter(m => !m.external).length + "개"});
}
const SECS = [["top",""],["problems","문제 (규칙별)"],["class","클래스 (역할별)"],["flow","게임 흐름"],["quality","품질"],["files","파일 분석"],["modules","모듈"]];
const closedSec = new Set(), openGrp = new Set();
function renderNav(){
  const q = $("#search").value.trim().toLowerCase();
  let html = "";
  for (const [sec, title] of SECS){
    const items = NAV.filter(n => n.sec === sec && (q ? (n.label + " " + (n.group || "")).toLowerCase().includes(q) : !n.searchOnly));
    if (!items.length) continue;
    if (sec === "top"){ html += `<div class="top">${items.map(itemHtml).join("")}</div>`; continue; }
    const open = q || !closedSec.has(sec);
    html += `<div class="sec"><div class="h" data-sec="${sec}"><span>${open ? "▾" : "▸"} ${title}</span><span class="n">${items.length}</span></div>`;
    if (open){
      const groups = {};
      for (const it of items) (groups[it.group || ""] ||= []).push(it);
      for (const [g, list] of Object.entries(groups)){
        if (!g){ html += list.slice(0, 300).map(itemHtml).join(""); continue; }
        const gopen = q || openGrp.has(sec + "|" + g) || list.length <= 8;
        html += `<div class="grp"><div class="h" data-grp="${esc(sec + "|" + g)}"><span>${gopen ? "▾" : "▸"} ${esc(g)}</span><span class="muted">${list.length}</span></div>${gopen ? list.map(itemHtml).join("") : ""}</div>`;
      }
    }
    html += `</div>`;
  }
  $("#nav").innerHTML = html || `<div class="it muted">일치하는 항목 없음</div>`;
  markCurrent();
}
const itemHtml = n => `<a class="it" href="${n.hash}">${esc(n.label)}${n.tag != null ? `<span class="t">${esc(n.tag)}</span>` : ""}</a>`;
function markCurrent(){ const h = location.hash || "#home"; for (const a of document.querySelectorAll(".it")) a.classList.toggle("cur", a.getAttribute("href") === h); }
document.addEventListener("click", e => {
  const s = e.target.closest("[data-sec]"); if (s){ const k = s.dataset.sec; closedSec.has(k) ? closedSec.delete(k) : closedSec.add(k); renderNav(); return; }
  const g = e.target.closest("[data-grp]"); if (g){ const k = g.dataset.grp; openGrp.has(k) ? openGrp.delete(k) : openGrp.add(k); renderNav(); }
});

// ------------------------------------------------------------------ summary (conclusion first)
function home(){
  const c = DATA.counts, probs = DATA.problems, cmp = DATA.compare;
  const bySev = {}; for (const p of probs) bySev[p.severity] = (bySev[p.severity] || 0) + 1;
  const rules = {}; for (const p of probs) rules[p.rule] = (rules[p.rule] || 0) + (p.items.length + p.more || 1);
  const topRule = Object.entries(rules).sort((a, b) => b[1] - a[1])[0];
  const sevText = ["fail", "warn", "info"].filter(s => bySev[s]).map(s => `${SEV_NAME[s]} ${bySev[s]}건`).join(", ");
  const changeText = cmp ? `이전 분석(${esc(cmp.previousRun)}) 대비 새 문제 ${cmp.new}건, 해결 ${cmp.fixed}건.` : "비교할 이전 분석 없음.";
  const def = DATA.flow.roots.find(r => r.default && r.kind === "map") || DATA.flow.roots[0];
  const first = [...probs].sort((a, b) => sevRank(b.severity) - sevRank(a.severity) || (b.items.length + b.more) - (a.items.length + a.more)).slice(0, 5);
  const roles = {}; for (const x of CLASS_LIST) roles[x.role] = roles[x.role] || {words: x.roleWords, n: 0}, roles[x.role].n++;
  const heavy = CLASS_LIST.filter(x => x.behaviors >= 8).sort((a, b) => b.behaviors - a.behaviors).slice(0, 8);
  return `<div class="eyebrow">아키텍처 분석 · ${esc(DATA.runId)} · ${DATA.source === "live" ? "Live Lens" : "analyze"} · UE ${esc(DATA.engine)}</div>
  <h1>${esc(DATA.project)}</h1>
  <div class="panel conclusion"><b>판정</b> ${verdictChip(DATA.verdict)} 문제 ${probs.length}건${sevText ? ` (${sevText})` : ""}. ${topRule ? `가장 많은 문제는 <a href="${link("problems", "", {rule: topRule[0]})}">${esc(ruleInfo(topRule[0]).name)}</a> (대상 ${topRule[1]}개).` : ""} ${changeText}</div>
  <div class="grid">
   ${kpi("문제", probs.length + "건", cmp ? `새 ${cmp.new}건 · 해결 ${cmp.fixed}건` : (sevText || "없음"), "#problems", probs.length ? "warn" : "")}
   ${kpi("클래스", CLASS_LIST.length + "개", `C++ ${c.classes}개 · 블루프린트 ${c.blueprints}개`, "#classes")}
   ${kpi("게임 흐름 시작점", DATA.flow.roots.length + "곳", def ? `기본: ${esc(def.name)}` : "", def ? link("flow", def.key) : "#home")}
   ${kpi("미사용 에셋", DATA.totals.unused + "개", `전체 에셋 ${c.assets}개`, "#quality/unused")}
  </div>
  <h2>먼저 확인할 문제</h2>
  ${first.length ? dataTable(first.map((p, i) => ({...p, idx: DATA.problems.indexOf(p), n: p.items.length + p.more, name: ruleInfo(p.rule).name})),
     [{key: "severity", label: "심각도", fmt: r => sevChip(r.severity)}, {key: "name", label: "규칙", link: r => link("problem", String(r.idx))},
      {key: "message", label: "내용"}, {key: "n", label: "대상", num: true, fmt: r => r.n + "개"}, {key: "fix", label: "조치", fmt: r => esc(ruleInfo(r.rule).fix)}], {}) : `<p class="muted">문제 0건.</p>`}
  <div class="split"><div><h2>역할별 클래스</h2>${bars(Object.entries(roles).sort((a, b) => b[1].n - a[1].n).map(([role, r]) => [r.words, r.n, link("classes", "", {role}), roleColor(role)]), "개")}</div>
  <div><h2>행동이 많은 클래스 (8개 이상)</h2>${heavy.length ? heavy.map(x => `<div><a href="${link("class", x.key)}">${esc(x.name)}</a> <span class="muted">${esc(x.roleWords)} · 행동 ${x.behaviors}개</span></div>`).join("") : `<p class="muted">해당 없음.</p>`}
  ${def ? `<h2>게임 흐름</h2><p><a href="${link("flow", def.key)}">${esc(def.name)}</a> 시작 (기본 맵). 레벨 → 게임 모드 → 컨트롤러·폰·HUD 순서.</p>` : ""}</div></div>
  <p class="muted">이전 형식(한 페이지에 모든 표와 다이어그램): <a href="report-classic.html">report-classic.html</a></p>`;
}
const verdictChip = v => `<span class="chip ${esc(v)}">${esc({pass:"통과", warn:"경고", fail:"실패"}[v] || v)}</span>`;

function changesView(){
  const c = DATA.changes, a = c.activity || {};
  const list = (t, xs) => xs && xs.length ? `<h3>${t} ${xs.length}개</h3><ul>${xs.slice(0, 200).map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  return `<div class="eyebrow">Live Lens · 세션 ${esc(c.session)}</div><h1>세션 변경 내역</h1>
  <p class="lead">수집 ${esc(c.collected)} · 에디터 종료 지연 ${(+c.exitSeconds || 0).toFixed(2)}초 · 비교 대상 ${esc(c.previousRun || "없음")}</p>
  <div class="grid">${kpi("저장한 에셋", (a.saved || []).length + "개", "", "#changes")}${kpi("연 에셋", (a.opened || []).length + "개", "", "#changes")}${kpi("PIE 실행", (a.pieRuns || 0) + "회", "", "#changes")}${kpi("로그 오류", (a.logErrors || 0) + "건", "", "#changes")}</div>
  ${list("추가된 클래스", c.classesAdded)}${list("삭제된 클래스", c.classesRemoved)}${list("추가된 흐름 연결", c.flowAdded)}${list("삭제된 흐름 연결", c.flowRemoved)}
  ${list("저장한 에셋", a.saved)}${list("PIE에서 확인된 클래스", Object.keys(a.inPlay || {}).map(short))}${list("첫 로그 오류", a.firstErrors)}`;
}

// ------------------------------------------------------------------ problems
function problemsView(params){
  const rows = DATA.problems.map((p, i) => ({...p, idx: i, n: p.items.length + p.more, name: ruleInfo(p.rule).name,
    change: p.change === "new" ? "새 문제" : p.change === "same" ? "이전부터" : ""}))
    .filter(p => (!params.sev || p.severity === params.sev) && (!params.rule || p.rule === params.rule) && (!params.change || p.change === params.change)
      && (!params.q || (p.message + " " + p.items.join(" ")).toLowerCase().includes(params.q.toLowerCase())));
  const rules = [...new Set(DATA.problems.map(p => p.rule))].map(r => [r, ruleInfo(r).name]);
  const info = params.rule ? ruleInfo(params.rule) : null;
  return `<div class="eyebrow">문제</div><h1>${info ? esc(info.name) : "문제 전체"}</h1>
  ${info ? `<div class="panel"><div><b>영향</b> ${esc(info.why)}</div><div><b>조치</b> ${esc(info.fix)}</div></div>` : ""}
  <div class="filters">${select("sev", "심각도", [["fail", "실패"], ["warn", "경고"], ["info", "정보"]], params.sev)}${select("rule", "규칙", rules, params.rule)}
   ${DATA.compare ? select("change", "변화", [["새 문제", "새 문제"], ["이전부터", "이전부터"]], params.change) : ""}${textFilter("q", "내용·대상", params.q)}
   <span class="muted">${rows.length}건</span></div>
  ${dataTable(rows, [{key: "severity", label: "심각도", fmt: r => sevChip(r.severity)}, {key: "name", label: "규칙"},
     {key: "message", label: "내용", link: r => link("problem", String(r.idx))}, {key: "n", label: "대상", num: true, fmt: r => r.n + "개"},
     ...(DATA.compare ? [{key: "change", label: "변화"}] : [])], params, "조건에 맞는 문제 없음.")}
  ${DATA.compare && DATA.compare.fixedList.length ? `<h2>해결된 문제 ${DATA.compare.fixed}건</h2><ul>${DATA.compare.fixedList.map(f => `<li>${esc(ruleInfo(f.rule).name)}: ${esc(f.message)}</li>`).join("")}</ul>` : ""}`;
}
function problemView(i){
  const p = DATA.problems[+i]; if (!p) return "<h1>없음</h1>";
  const info = ruleInfo(p.rule);
  return `<div class="eyebrow"><a href="${link("problems", "", {rule: p.rule})}">${esc(info.name)}</a></div><h1>${esc(p.message)}</h1>
  <p class="lead">${sevChip(p.severity)} ${p.change === "new" ? '<span class="chip fail">새 문제</span>' : p.change === "same" ? '<span class="chip">이전부터</span>' : ""} 대상 ${p.items.length + p.more}개</p>
  <div class="panel"><div><b>영향</b> ${esc(info.why)}</div><div><b>조치</b> ${esc(info.fix)}</div></div>
  <h2>대상 ${p.items.length + p.more}개</h2>${p.items.length ? `<ul>${p.items.map((x, j) => { const k = (p.keys || [])[j]; return `<li class="mono">${k ? `<a href="${link("class", k)}">${esc(x)}</a>` : esc(x)}</li>`; }).join("")}</ul>` : `<p class="muted">대상 목록 없음.</p>`}
  ${p.more ? `<p class="muted">외 ${p.more}개는 생략.</p>` : ""}`;
}

// ------------------------------------------------------------------ classes (filters + table)
function classesView(params){
  const tops = [...new Set(CLASS_LIST.map(x => x.top).filter(Boolean))].sort();
  const subs = params.top ? [...new Set(CLASS_LIST.filter(x => x.top === params.top).map(x => x.folder))].sort() : [];
  const roles = [...new Map(CLASS_LIST.map(x => [x.role, x.roleWords]))].sort((a, b) => a[1].localeCompare(b[1]));
  const rows = CLASS_LIST.filter(x => (!params.role || x.role === params.role) && (!params.kind || x.kind === params.kind)
    && (!params.top || x.top === params.top) && (!params.sub || x.folder === params.sub)
    && (!params.q || x.name.toLowerCase().includes(params.q.toLowerCase())) && (!params.play || x.inPlay));
  const roleName = params.role ? (roles.find(r => r[0] === params.role) || [, params.role])[1] : "";
  return `<div class="eyebrow">클래스</div><h1>${roleName ? esc(roleName) : "클래스 전체"}</h1>
  <div class="filters">${select("role", "역할", roles, params.role)}${select("kind", "종류", [["BP", "블루프린트"], ["C++", "C++"]], params.kind)}
   ${select("top", "폴더", tops.map(t => [t, t]), params.top)}${params.top ? select("sub", "하위 폴더", subs.map(s => [s, s.slice(params.top.length) || "(바로 아래)"]), params.sub) : ""}
   ${textFilter("q", "이름", params.q)}<span class="muted">${rows.length}개</span></div>
  ${dataTable(rows, [{key: "name", label: "이름", link: r => link("class", r.key)}, {key: "roleWords", label: "역할"}, {key: "kind", label: "종류"},
    {key: "folder", label: "폴더", fmt: r => `<span class="mono muted">${esc(r.folder)}</span>`}, {key: "behaviors", label: "행동", num: true},
    {key: "out", label: "참조하는 대상", num: true}, {key: "in", label: "참조되는 곳", num: true},
    {key: "inPlay", label: "PIE 확인", num: true, fmt: r => r.inPlay ? r.inPlay + "세션" : ""}], params, "조건에 맞는 클래스 없음.")}`;
}

// ------------------------------------------------------------------ game flow
let FLOW = {root:null, depth:3, weak:false, open:new Set(), step:-1, timer:null, view:null, sel:null};
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
const behaviorsOf = k => (DATA.classes[k] || {}).behaviors ? DATA.classes[k].behaviors.map(b => b.name) : (nodeOf(k).behaviors || []);
function layout(m){
  const W = 210, H = 58, GX = 170, GY = 18, cols = {};
  for (const k of m.order) (cols[m.level.get(k)] ||= []).push(k);
  const pos = {};
  const parentRow = k => { const e = m.edges.find(e => e.to === k && pos[e.from]); return e ? pos[e.from].y : 0; };
  for (const [lv, keys] of Object.entries(cols)){
    if (+lv > 0) keys.sort((a, b) => parentRow(a) - parentRow(b));
    const nodeH = k => m.groups[k] ? H + Math.min(6, m.groups[k].members.length) * 14 : H + Math.min(2, behaviorsOf(k).length) * 14;
    let y = 0;
    for (const k of keys){ pos[k] = {x: +lv * (W + GX), y, w: W, h: nodeH(k)}; y += nodeH(k) + GY; }
  }
  return pos;
}
function flowView(root){
  if (FLOW.root !== root){
    FLOW = {...FLOW, root, open: new Set(), step: -1, view: null, depth: 3, sel: null};
    while (FLOW.depth > 1 && flowModel(root).order.length > 45) FLOW.depth--;
  }
  const n = nodeOf(root), m = flowModel(root);
  return `<div class="eyebrow">게임 흐름</div><h1>${esc(n.name)} 시작</h1>
  <p class="lead">${esc(n.roleWords || "")} · 객체 ${m.order.length}개 · 깊이 ${FLOW.depth}단계</p>
  <div class="bar">깊이 ${[1,2,3,4,5,6].map(d => `<button class="btn ${d === FLOW.depth ? "on" : ""}" data-depth="${d}">${d}</button>`).join("")}
   <button class="btn ${FLOW.weak ? "on" : ""}" id="weak" title="함수 호출, 형변환, 변수·에셋 참조도 선으로 표시">호출·참조 포함</button>
   <span style="flex:1"></span><button class="btn" id="prev" title="이전 단계">◀</button><button class="btn" id="play">단계별 재생</button><button class="btn" id="next" title="다음 단계">▶</button><button class="btn" id="fit">전체 보기</button></div>
  <div class="diag" id="diag"><svg id="svg"></svg><div class="hint">휠: 상하 이동 · Ctrl+휠: 확대 · 드래그: 이동 · 클릭: 선택 · 더블클릭: 클래스 열기</div></div>
  <div class="legend bar"><span><b>실선</b> 지정·배치·생성·소유</span><span><b>점선</b> 사용·호출·참조</span>${[["map","레벨"],["gamemode","게임 모드·상태"],["controller","컨트롤러"],["character","캐릭터·폰"],["widget","UI"],["component","컴포넌트"],["ability","GAS"],["data","데이터"],["object","기타"]].map(([r, t]) => `<span><i style="background:${roleColor(r)}"></i>${t}</span>`).join("")}</div>
  <div id="stepnote" class="panel muted"></div>
  <div id="selpanel"></div>
  <h2>연결 목록</h2><div id="edgelist"></div>`;
}
function drawFlow(){
  const svg = $("#svg"); if (!svg) return;
  const m = flowModel(FLOW.root), pos = layout(m), parts = [];
  const defs = `<defs>${Object.entries({...EDGE_COLOR, other:"--r-other"}).map(([k, v]) => `<marker id="ar-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="var(${v})"/></marker>`).join("")}</defs>`;
  for (const e of m.edges){
    const a = pos[e.from], b = pos[e.to]; if (!a || !b) continue;
    const x1 = a.x + a.w, y1 = a.y + 22, x2 = b.x, y2 = b.y + 22, mx = (x1 + x2) / 2, lv = m.level.get(e.to);
    const label = (e.labels && e.labels[0]) || e.words || "";
    parts.push(`<path class="edge ${SOLID.has(e.kind) ? "" : "dash"}" data-lv="${lv}" data-from="${esc(e.from)}" data-to="${esc(e.to)}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" stroke="var(${EDGE_COLOR[e.kind] || "--r-other"})" marker-end="url(#ar-${EDGE_COLOR[e.kind] ? e.kind : "other"})"><title>${esc(nodeOf(e.from).name)} → ${esc(nodeOf(e.to).name)}: ${esc(edgeWord(e))}${label ? " (" + esc(label) + ")" : ""}</title></path>`);
    if (label) parts.push(`<text class="elabel" data-lv="${lv}" x="${mx}" y="${(y1 + y2) / 2 - 4}" text-anchor="middle">${esc(label.length > 22 ? label.slice(0, 21) + "…" : label)}</text>`);
  }
  for (const k of m.order){
    const p = pos[k], lv = m.level.get(k), g = m.groups[k];
    if (g){
      const words = (DATA.flow.edges.find(e => e.kind === g.kind) || {}).words || g.kind;
      parts.push(`<g class="node" data-lv="${lv}" data-group="${esc(k)}" transform="translate(${p.x},${p.y})"><title>${esc(words)} ${g.members.length}개: 클릭하면 펼침</title><rect class="box" width="${p.w}" height="${p.h}" rx="8" stroke-dasharray="4 3"/>
       <text x="12" y="20" font-weight="700">${esc(words)} ${g.members.length}개</text><text class="sub" x="12" y="36">클릭하면 펼침</text>
       ${g.members.slice(0, 6).map((mk, i) => `<text class="beh" x="12" y="${54 + i * 14}">· ${esc(nodeOf(mk).name)}</text>`).join("")}</g>`);
      continue;
    }
    const n = nodeOf(k), beh = behaviorsOf(k), src = n.source === "bp" ? "BP" : n.source === "cpp" ? "C++" : "";
    const tip = `${n.name}\n${n.roleWords || ""}${src ? " · " + src : ""}${beh.length ? "\n행동: " + beh.slice(0, 6).join(", ") : ""}\n참조하는 대상 ${(OUT[k] || []).length}개 · 참조되는 곳 ${(IN[k] || []).length}개`;
    parts.push(`<g class="node ${FLOW.sel === k ? "sel" : ""}" data-lv="${lv}" data-key="${esc(k)}" transform="translate(${p.x},${p.y})"><title>${esc(tip)}</title><rect class="box" width="${p.w}" height="${p.h}" rx="8"/>
      <rect class="stripe" width="5" height="${p.h}" rx="2" fill="${roleColor(n.role)}"/>
      <text x="14" y="20" font-weight="700">${esc(n.name.length > 26 ? n.name.slice(0, 25) + "…" : n.name)}</text>
      <text class="sub" x="14" y="37">${esc((n.roleWords || "") + (src ? " · " + src : "") + (n.inPlay ? " · PIE 확인" : ""))}</text>
      ${beh.slice(0, 2).map((b, i) => `<text class="beh" x="14" y="${54 + i * 14}">· ${esc(b.length > 26 ? b.slice(0, 25) + "…" : b)}</text>`).join("")}</g>`);
  }
  const xs = Object.values(pos);
  FLOW.extent = {w: Math.max(...xs.map(p => p.x + p.w)) + 20, h: Math.max(...xs.map(p => p.y + p.h)) + 20};
  svg.innerHTML = defs + `<g id="world">${parts.join("")}</g>`;
  if (!FLOW.view) fit();
  applyView(); applyStep(m); selPanel();
  const real = m.edges.filter(e => !m.groups[e.to]).map(e => ({...e, fromName: nodeOf(e.from).name, toName: nodeOf(e.to).name, word: edgeWord(e) + (e.labels && e.labels[0] ? " · " + e.labels[0] : ""), ev: (e.evidence || []).slice(0, 2).join(" · ")}));
  $("#edgelist").innerHTML = dataTable(real, [{key: "fromName", label: "출발", link: r => link("class", r.from)}, {key: "word", label: "관계"},
    {key: "toName", label: "도착", link: r => link("class", r.to)}, {key: "ev", label: "근거", fmt: r => `<span class="muted">${esc(r.ev)}</span>`}], {}, "연결 없음.");
}
function selPanel(){
  const el = $("#selpanel"); if (!el) return;
  const k = FLOW.sel;
  if (!k){ el.innerHTML = ""; return; }
  const n = nodeOf(k), c = DATA.classes[k] || {}, outs = OUT[k] || [], ins = IN[k] || [];
  el.innerHTML = `<div class="panel"><b>${esc(n.name)}</b> <span class="muted">${esc(n.roleWords || "")}</span>
   <div class="muted">${(c.behaviors || []).length ? "행동: " + esc(c.behaviors.slice(0, 8).map(b => b.name).join(", ")) : "행동 없음"}</div>
   <div>참조하는 대상 ${outs.length}개 · 참조되는 곳 ${ins.length}개</div>
   <div class="bar">${DATA.classes[k] ? `<a class="btn" href="${link("class", k)}">클래스 열기</a>` : ""}<a class="btn" href="${link("flow", k)}">여기서 시작</a></div></div>`;
}
function fit(){
  const d = $("#diag").getBoundingClientRect(), e = FLOW.extent;
  const s = Math.min(1.1, Math.max(0.75, Math.min(d.width / e.w, d.height / e.h)));
  FLOW.view = {s, x: 12, y: e.h * s < d.height ? (d.height - e.h * s) / 2 : 12};
}
function applyView(){ const w = $("#world"); if (w && FLOW.view) w.setAttribute("transform", `translate(${FLOW.view.x},${FLOW.view.y}) scale(${FLOW.view.s})`); }
function applyStep(m){
  const max = Math.max(...m.order.map(k => m.level.get(k)));
  for (const el of document.querySelectorAll("#svg [data-lv]")) el.classList.toggle("future", FLOW.step >= 0 && +el.dataset.lv > FLOW.step);
  const note = $("#stepnote");
  if (FLOW.step < 0){ note.textContent = "단계별 재생: 시작점부터 한 단계씩 연결을 표시합니다."; return; }
  const here = m.order.filter(k => m.level.get(k) === FLOW.step && !m.groups[k]).map(k => nodeOf(k).name);
  note.innerHTML = `<b>${FLOW.step} / ${max}단계</b> ${FLOW.step === 0 ? "시작점" : esc(here.slice(0, 12).join(", ")) + (here.length > 12 ? ` 외 ${here.length - 12}개` : "")}`;
}
function wireFlow(){
  const diag = $("#diag"); if (!diag) return;
  for (const b of document.querySelectorAll("[data-depth]")) b.onclick = () => { FLOW.depth = +b.dataset.depth; FLOW.view = null; render(); };
  $("#weak").onclick = () => { FLOW.weak = !FLOW.weak; FLOW.view = null; render(); };
  const m = () => flowModel(FLOW.root), maxLv = () => { const mm = m(); return Math.max(...mm.order.map(k => mm.level.get(k))); };
  $("#next").onclick = () => { FLOW.step = Math.min(maxLv(), FLOW.step + 1); applyStep(m()); };
  $("#prev").onclick = () => { FLOW.step = Math.max(-1, FLOW.step - 1); applyStep(m()); };
  $("#fit").onclick = () => { fit(); applyView(); };
  $("#play").onclick = () => { clearInterval(FLOW.timer); FLOW.step = 0; applyStep(m());
    FLOW.timer = setInterval(() => { if (FLOW.step >= maxLv()){ clearInterval(FLOW.timer); return; } FLOW.step++; applyStep(m()); }, 1100); };
  let drag = null;
  diag.onmousedown = e => { drag = {x: e.clientX, y: e.clientY, vx: FLOW.view.x, vy: FLOW.view.y, moved: false}; diag.style.cursor = "grabbing"; };
  window.onmousemove = e => { if (!drag) return; const dx = e.clientX - drag.x, dy = e.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true; FLOW.view.x = drag.vx + dx; FLOW.view.y = drag.vy + dy; applyView(); };
  window.onmouseup = e => {
    if (drag && !drag.moved){
      const g = e.target.closest && e.target.closest(".node");
      if (g && g.dataset.group){ FLOW.open.add(g.dataset.group); drawFlow(); }
      else if (g && g.dataset.key){ FLOW.sel = g.dataset.key;                                    // linkage: the panel below follows the selection
        for (const n of document.querySelectorAll("#svg .node")) n.classList.toggle("sel", n.dataset.key === FLOW.sel);
        for (const p of document.querySelectorAll("#svg .edge")) p.classList.toggle("hl", p.dataset.from === FLOW.sel || p.dataset.to === FLOW.sel);
        selPanel(); }
    }
    drag = null; diag.style.cursor = "grab";
  };
  diag.ondblclick = e => { const g = e.target.closest && e.target.closest(".node[data-key]"); if (g && DATA.classes[g.dataset.key]) location.hash = link("class", g.dataset.key); };
  diag.onwheel = e => { e.preventDefault();
    if (!e.ctrlKey){ FLOW.view.y -= e.deltaY; FLOW.view.x -= e.deltaX; applyView(); return; }
    const r = diag.getBoundingClientRect(), k = e.deltaY < 0 ? 1.12 : 1 / 1.12, s = Math.min(2.5, Math.max(0.15, FLOW.view.s * k));
    const px = e.clientX - r.left, py = e.clientY - r.top; FLOW.view.x = px - (px - FLOW.view.x) * s / FLOW.view.s; FLOW.view.y = py - (py - FLOW.view.y) * s / FLOW.view.s; FLOW.view.s = s; applyView(); };
  drawFlow();
}

// ------------------------------------------------------------------ class detail
function stepsHtml(steps){
  if (!steps || !steps.length) return "";
  return `<ul class="steps">${steps.map(s => {
    const target = s.target && DATA.classes[s.target] ? ` <a href="${link("class", s.target)}" title="${esc(short(s.target))} 열기">↗</a>` : "";
    const text = `<span class="${s.kind === "note" ? "k-note" : ""}">${esc(s.text)}</span>${target}`;
    const kids = (s.branches || []).map(b => `<li class="step-branch"><details open><summary>${esc(b.label)}</summary>${stepsHtml(b.steps)}</details></li>`).join("") + (s.steps ? stepsHtml(s.steps) : "");
    return kids ? `<li class="step-if"><details open><summary>${text}</summary><ul class="steps">${kids}</ul></details></li>` : `<li>${text}</li>`;
  }).join("")}</ul>`;
}
function classView(key){
  const c = DATA.classes[key], n = nodeOf(key);
  if (!c && !DATA.flow.nodes[key]) return `<h1>찾을 수 없음</h1><p class=muted>${esc(key)}</p>`;
  const cls = c || {behaviors: [], features: []};
  const outs = OUT[key] || [], ins = IN[key] || [];
  const allFeat = id => (cls.features || []).find(f => f.id === id) || Object.values(DATA.classes).flatMap(x => x.features || []).find(f => f.id === id);
  const file = DATA.files.find(f => key.includes("/" + f.name + "."));
  const probs = DATA.problems.map((p, i) => [p, i]).filter(([p]) => (p.keys || []).includes(key));
  const edgeRows = (list, other) => list.map(e => ({k: other(e), name: nodeOf(other(e)).name, word: edgeWord(e) + (e.labels && e.labels[0] ? " · " + e.labels[0] : "")}));
  return `<div class="eyebrow"><a href="${link("classes", "", {role: cls.role})}">${esc(n.roleWords || cls.roleWords || "클래스")}</a> · ${cls.kind === "blueprint" ? "블루프린트" : "C++"}</div><h1>${esc(n.name || short(key))}</h1>
  <p class="lead">${(cls.chain || n.chain || []).length ? "부모 " + esc((cls.chain || n.chain).join(" < ")) : ""}${cls.inPlay ? ` · <span class="chip pass">PIE 확인 ${cls.inPlay.sessions}세션</span>` : ""}<br><span class="mono muted">${esc(cls.path || n.path || key)}</span></p>
  <div class="bar"><a class="btn" href="${link("flow", key)}">이 클래스부터 흐름 보기</a>${file ? `<a class="btn" href="${link("file", file.name)}">파일 분석</a>` : ""}</div>
  ${probs.length ? `<div class="panel warn">관련 문제 ${probs.length}건: ${probs.map(([p, i]) => `<a href="${link("problem", String(i))}">${esc(ruleInfo(p.rule).name)}</a>`).join(", ")}</div>` : ""}
  <div class="split"><div><h3>참조하는 대상 ${outs.length}개</h3>${dataTable(edgeRows(outs, e => e.to), [{key: "name", label: "대상", link: r => link("class", r.k)}, {key: "word", label: "관계"}], {}, "없음.")}</div>
  <div><h3>참조되는 곳 ${ins.length}개</h3>${dataTable(edgeRows(ins, e => e.from), [{key: "name", label: "클래스", link: r => link("class", r.k)}, {key: "word", label: "관계"}], {}, "없음.")}</div></div>
  <h2>행동 ${(cls.behaviors || []).length}개</h2>
  ${(cls.behaviors || []).map(b => `<div class="beh"><b>${esc(b.name)}</b> <span class="muted">${esc(b.nameEn || "")}${b.primary ? " · 주 함수 " + esc(b.primary) : ""}</span>
     ${(b.cases || []).length ? `<table class="cases">${b.cases.slice(0, 8).map(cs => `<tr><td class="muted">${esc(cs.when)}</td><td>${esc((cs.does || []).join(", "))}</td></tr>`).join("")}</table>` : ""}
     ${(b.features || []).map(id => { const f = allFeat(id); return f ? `<details><summary>${esc(f.name)} <span class="muted">${esc(f.trigger || "")} · ${f.source === "bp" ? "BP" : "C++"}</span></summary>${stepsHtml(f.steps)}</details>` : ""; }).join("")}</div>`).join("")
   || (cls.features || []).map(f => `<div class="beh"><details><summary><b>${esc(f.name)}</b> <span class="muted">${esc(f.trigger || "")}</span></summary>${stepsHtml(f.steps)}</details></div>`).join("")
   || `<p class="muted">진입점 없음. 데이터·설정용 클래스이거나, 다른 클래스가 호출하는 함수만 있습니다.</p>`}`;
}

// ------------------------------------------------------------------ quality, files, modules
function qualityView(kind, params){
  if (kind === "complexity"){
    const rows = DATA.complexity.map(r => ({name: short(r.blueprint || r.bp || r.asset || r.key || ""), graph: r.graph || "", nodes: r.nodes || 0, links: r.links || 0, over: (r.over || []).join(", ")}));
    return `<div class="eyebrow">품질</div><h1>블루프린트 복잡도</h1><p class="lead">그래프 ${DATA.totals.graphs}개 중 기준 초과 ${DATA.totals.graphsOver}개. 표는 기준 초과를 먼저, 나머지는 노드 수 순으로 ${rows.length}개까지 싣습니다. ${esc(ruleInfo("bp-complexity").fix)}</p>
     <div class="filters">${select("over", "기준", [["1", "초과만"]], params.over)}${textFilter("q", "이름", params.q)}</div>` + dataTable(rows.filter(r => (!params.over || r.over) && (!params.q || r.name.toLowerCase().includes(params.q.toLowerCase()))),
      [{key: "name", label: "블루프린트"}, {key: "graph", label: "그래프"}, {key: "nodes", label: "노드", num: true}, {key: "links", label: "연결", num: true}, {key: "over", label: "넘은 기준"}], params);
  }
  if (kind === "unused"){
    const rows = DATA.unused.map(u => ({asset: typeof u === "string" ? u : (u.asset || u.package || "")})).map(r => ({...r, folder: r.asset.split("/").slice(0, -1).join("/")}));
    return `<div class="eyebrow">품질</div><h1>미사용 에셋</h1><p class="lead">다른 에셋·맵·설정이 참조하지 않는 에셋 ${DATA.totals.unused}개${DATA.totals.unused > rows.length ? ` (처음 ${rows.length}개 수록)` : ""}. 코드나 데이터에서 경로 문자열로 불러오는지 확인한 뒤 삭제하세요.</p>
     <div class="filters">${textFilter("q", "경로", params.q)}</div>` + dataTable(rows.filter(r => !params.q || r.asset.toLowerCase().includes(params.q.toLowerCase())),
      [{key: "asset", label: "에셋", fmt: r => `<span class="mono">${esc(r.asset)}</span>`}, {key: "folder", label: "폴더"}], params);
  }
  const rows = DATA.naming.map(r => ({asset: r.asset || r.name || "", cls: short(r.class || ""), problem: (r.problems || [r.expected || r.message || ""]).join(", ")}));
  return `<div class="eyebrow">품질</div><h1>이름 규칙 위반</h1><p class="lead">위반 ${DATA.totals.naming}개${DATA.totals.naming > rows.length ? ` (처음 ${rows.length}개 수록)` : ""}. 규칙은 config의 naming 항목에서 바꿉니다.</p>
   <div class="filters">${textFilter("q", "이름", params.q)}</div>` + dataTable(rows.filter(r => !params.q || r.asset.toLowerCase().includes(params.q.toLowerCase())),
    [{key: "asset", label: "대상", fmt: r => `<span class="mono">${esc(r.asset)}</span>`}, {key: "cls", label: "종류"}, {key: "problem", label: "위반"}], params);
}
function fileView(name){
  const d = DATA.files.find(f => f.name === name); if (!d) return "<h1>없음</h1>";
  const steps = ss => !ss || !ss.length ? "" : `<ul class="steps">${ss.map(s => s.children && s.children.length
      ? `<li><details ${["if","else","on","loop"].includes(s.kind) ? "open" : ""}><summary class="${s.kind === "note" ? "k-note" : ""}">${esc(s.text)}</summary>${s.cpp ? `<pre class="mono panel">${esc((s.cpp.code || []).join("\n"))}</pre>` : ""}${steps(s.children)}</details></li>`
      : `<li class="${s.kind === "note" ? "k-note" : ""}" title="${esc(s.detail || "")}">${esc(s.text)}${s.cpp ? ` <details style="display:inline"><summary class="muted">C++</summary><pre class="mono panel">${esc((s.cpp.code || []).join("\n"))}</pre></details>` : ""}</li>`).join("")}</ul>`;
  const c = d.composition || {};
  const tbl = (rows, cols) => dataTable(rows.map(r => ({...r, changedBy: (r.changedBy || []).join(", ")})), cols, {}, "없음.");
  return `<div class="eyebrow">파일 분석 · ${esc(d.package)}</div><h1>${esc(d.name)}</h1><p class="lead">${esc(d.kind)} · ${esc((d.parentChain || []).join(" < "))}</p>
  <div class="panel">${esc(d.summary)}</div>
  <h2>컴포넌트 ${(c.components || []).length}개</h2>${tbl(c.components || [], [{key: "name", label: "이름"}, {key: "role", label: "역할"}, {key: "class", label: "클래스"}, {key: "parent", label: "부모"}])}
  ${(c.settings || []).length ? `<h2>설정값 ${c.settings.length}개</h2>` + tbl(c.settings, [{key: "name", label: "이름"}, {key: "type", label: "타입"}, {key: "default", label: "기본값"}, {key: "changedBy", label: "변경하는 행동"}]) : ""}
  ${(c.state || []).length ? `<h2>상태 변수 ${c.state.length}개</h2>` + tbl(c.state, [{key: "name", label: "이름"}, {key: "type", label: "타입"}, {key: "default", label: "기본값"}, {key: "changedBy", label: "변경하는 행동"}]) : ""}
  <h2>행동 ${(d.behaviors || []).length}개</h2>${(d.behaviors || []).map(b => `<div class="beh"><b>${esc(b.title)}</b><div class="muted">${esc(b.trigger)}</div>${(b.notes || []).map(x => `<div class="k-note">${esc(x)}</div>`).join("")}${steps(b.steps)}</div>`).join("")}`;
}
function modulesView(params){
  const rows = DATA.modules.filter(m => !m.external).map(m => ({name: m.name, deps: m.deps.map(short).join(", "), n: m.deps.length}));
  return `<div class="eyebrow">모듈</div><h1>모듈 의존성</h1><p class="lead">프로젝트 모듈 ${rows.length}개와 각 모듈이 의존하는 모듈.</p>` +
    dataTable(rows, [{key: "name", label: "모듈"}, {key: "n", label: "의존 수", num: true}, {key: "deps", label: "의존하는 모듈"}], params);
}

// ------------------------------------------------------------------ render
function render(){
  const r = route(), view = $("#view");
  clearInterval(FLOW.timer);
  if (r.kind === "flow"){ view.innerHTML = flowView(r.arg); wireFlow(); }
  else if (r.kind === "class") view.innerHTML = classView(r.arg);
  else if (r.kind === "classes") view.innerHTML = classesView(r.params);
  else if (r.kind === "problems") view.innerHTML = problemsView(r.params);
  else if (r.kind === "problem") view.innerHTML = problemView(r.arg);
  else if (r.kind === "quality") view.innerHTML = qualityView(r.arg, r.params);
  else if (r.kind === "file") view.innerHTML = fileView(r.arg);
  else if (r.kind === "modules") view.innerHTML = modulesView(r.params);
  else if (r.kind === "changes" && DATA.changes) view.innerHTML = changesView();
  else view.innerHTML = home();
  markCurrent();
}
let lastPath = "";
window.onhashchange = () => { const p = (location.hash || "").split("?")[0]; render(); if (p !== lastPath) $("#main").scrollTop = 0; lastPath = p; };
$("#pname").textContent = DATA.project;
$("#sub").textContent = `아키텍처 분석 · UE ${DATA.engine} · ${DATA.runId}`;
$("#search").oninput = renderNav;
$("#search").onkeydown = e => { if (e.key === "Enter"){ const a = document.querySelector(".nav .it[href]"); if (a) location.hash = a.getAttribute("href"); } };
$("#theme").onclick = () => { const r = document.documentElement; const dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; r.dataset.theme = dark ? "light" : "dark"; };
buildNav(); renderNav(); render(); lastPath = (location.hash || "").split("?")[0];
</script></body></html>
"""


def write(out: Path, project: Path | None) -> Path:
    data = build_data(out, project)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = f"{data['project']} · DwoLens 리포트"
    html = TEMPLATE.replace("__COMMON__", JS_COMMON).replace("__CSS__", CSS).replace("__TITLE__", title.replace("<", "")).replace("__DATA__", payload)
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
