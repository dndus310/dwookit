// DwoLens 게임 흐름: how the project runs, top down. From a map (or a GameMode) to the GameMode, the player's
// controller, pawn and HUD, and on to what each object spawns, creates, owns and uses. Each object shows its role
// and behaviors; a click opens its features (아키텍처 › 기능) or its file analysis (파일 분석).
// Data: <latest analyze run>/out/flow.json (tools/lens_flow.py).
#pragma once

#include "views_files.h"

struct FlowState
{
	fs::path loaded;
	fs::file_time_type loadedTime;
	json doc;
	std::string root;
	std::string sel;
	int depth = 3;
	bool weak = false;          // also calls, casts and plain references
	char filter[128] = {};
	GraphView graph;
	std::string graphKey;
	std::set<std::string> expanded;   // group nodes the user opened ('<from>#<kind>')
	std::map<std::string, std::vector<std::string>> groups;   // group node id -> member keys
};
static FlowState g_flow;

static ImU32 FlowRoleColor(const std::string& role)
{
	if (role == "map") return col::Teal;
	if (role == "gamemode" || role == "gamestate" || role == "gameinstance") return col::Purple;
	if (role == "controller" || role == "playerstate") return col::Accent;
	if (role == "character" || role == "pawn") return col::Warn;
	if (role == "hud" || role == "widget") return col::Info;
	if (role == "ai") return col::Fail;
	if (role == "component") return IM_COL32(0xB0, 0x86, 0x4A, 255);
	if (role == "ability" || role == "effect") return col::Minor;
	return col::Muted;
}

static const char* FlowRoleIcon(const std::string& role)
{
	if (role == "map") return ICON_PACKAGE;
	if (role == "gamemode" || role == "gamestate" || role == "gameinstance") return ICON_GAME;
	if (role == "hud" || role == "widget") return ICON_LAYERS;
	if (role == "component") return ICON_PUZZLE;
	if (role == "ability" || role == "effect") return ICON_BOLT;
	return ICON_TAG;
}

static ImU32 FlowEdgeColor(const std::string& kind)
{
	if (kind == "slot" || kind == "possesses") return col::Purple;
	if (kind == "spawns" || kind == "creates") return col::Warn;
	if (kind == "has") return IM_COL32(0xB0, 0x86, 0x4A, 255);
	if (kind == "places") return col::Teal;
	if (kind == "uses") return col::Info;
	return col::Muted;
}

static const json* FlowDoc(App& app)
{
	const RunInfo* r = app.Latest(0);
	if (!r) return nullptr;
	fs::path p = r->dir / "out" / "flow.json";
	std::error_code ec;
	auto t = fs::last_write_time(p, ec);
	if (ec) return nullptr;
	if (p != g_flow.loaded || t != g_flow.loadedTime)
	{
		g_flow.loaded = p;
		g_flow.loadedTime = t;
		auto d = ReadJson(p);
		g_flow.doc = d ? *d : json();
		g_flow.graphKey.clear();
		// keep the chosen starting point when a new analysis still has it
		bool known = false;
		if (g_flow.doc.is_object())
		{
			if (g_flow.doc.contains("nodes") && g_flow.doc["nodes"].contains(g_flow.root)) known = true;
			for (auto& r : Arr(g_flow.doc, "roots")) known |= Str(r, "key") == g_flow.root;
			for (auto& u : Arr(g_flow.doc, "unreached")) known |= Str(u, "key") == g_flow.root;
		}
		if (!known) g_flow.root.clear();
	}
	return g_flow.doc.is_object() ? &g_flow.doc : nullptr;
}

static bool FlowStrong(const std::string& kind)
{
	return kind == "slot" || kind == "possesses" || kind == "spawns" || kind == "creates" || kind == "has" || kind == "places" || kind == "uses";
}

static std::string FlowName(const json& doc, const std::string& key)
{
	if (doc.contains("nodes") && doc["nodes"].contains(key)) return Str(doc["nodes"][key], "name");
	size_t at = key.find_last_of("/.:");
	return at == std::string::npos ? key : key.substr(at + 1);
}

static void BuildFlowGraph(const json& doc)
{
	std::string key = g_flow.root + "|" + std::to_string(g_flow.depth) + "|" + (g_flow.weak ? "w" : "s") + "|" + PathUtf8(g_flow.loaded) + "|" + std::to_string(g_flow.expanded.size());
	if (key == g_flow.graphKey) return;
	g_flow.graphKey = key;
	std::map<std::string, std::vector<const json*>> adj;
	for (auto& e : Arr(doc, "edges"))
		if (g_flow.weak || FlowStrong(Str(e, "kind"))) adj[Str(e, "from")].push_back(&e);
	// breadth first from the root, depth limited. More than kGroup targets of one kind from one object (a level's
	// placed props, a HUD's widgets) collapse into one group node until the user opens it.
	const size_t kGroup = 6;
	g_flow.groups.clear();
	std::map<std::string, int> level{ { g_flow.root, 0 } };
	std::vector<std::string> order{ g_flow.root };
	std::vector<std::pair<std::string, const json*>> used;     // (from node id, edge)
	for (size_t i = 0; i < order.size(); ++i)
	{
		const std::string k = order[i];
		if (level[k] >= g_flow.depth || g_flow.groups.count(k)) continue;
		std::map<std::string, std::vector<const json*>> byKind;
		for (const json* e : adj[k]) byKind[Str(*e, "kind")].push_back(e);
		for (auto& [kind, kindList] : byKind)
		{
			std::vector<const json*> list = kindList;
			std::string gid = k + "#" + kind;
			// only targets that lead nowhere further (placed props, leaf widgets) are grouped; the ones the flow
			// continues through stay visible
			std::vector<const json*> leaves, through;
			for (const json* e : list) (adj.count(Str(*e, "to")) && !adj[Str(*e, "to")].empty() ? through : leaves).push_back(e);
			if (list.size() > kGroup && leaves.size() >= 3 && !g_flow.expanded.count(gid))
			{
				for (const json* e : leaves) g_flow.groups[gid].push_back(Str(*e, "to"));
				used.push_back({ k, leaves.front() });
				if (!level.count(gid)) { level[gid] = level[k] + 1; order.push_back(gid); }
				list = through;
			}
			for (const json* e : list)
			{
				std::string to = Str(*e, "to");
				used.push_back({ k, e });
				if (!level.count(to)) { level[to] = level[k] + 1; order.push_back(to); }
			}
		}
	}
	std::vector<GraphNode> nodes;
	std::map<std::string, int> index;
	const json& all = doc.contains("nodes") ? doc["nodes"] : json::object();
	for (auto& k : order)
	{
		GraphNode n;
		n.id = k;
		if (g_flow.groups.count(k))
		{
			const auto& members = g_flow.groups[k];
			std::string kind = k.substr(k.rfind('#') + 1);
			const json* any = nullptr;
			for (auto& e : Arr(doc, "edges")) if (Str(e, "kind") == kind) { any = &e; break; }
			n.title = (any ? Str(*any, "words") : kind) + Fmt(" %d개", (int)members.size());
			n.subtitle = "더블클릭하면 펼침";
			n.accent = FlowEdgeColor(kind);
			n.icon = ICON_LIST;
			for (size_t i = 0; i < members.size() && i < 8; ++i) { GraphRow row; row.value = FlowName(doc, members[i]); n.rows.push_back(row); }
			if (members.size() > 8) n.footer = Fmt("외 %d개", (int)members.size() - 8);
			n.layer = level[k];
			index[k] = (int)nodes.size();
			nodes.push_back(std::move(n));
			continue;
		}
		const json& d = all.contains(k) ? all[k] : json::object();
		std::string role = Str(d, "role", "object");
		n.title = FlowName(doc, k);
		std::string src = Str(d, "source");
		n.subtitle = Str(d, "roleWords") + (src == "bp" ? "  · BP" : src == "cpp" ? "  · C++" : "");
		n.accent = FlowRoleColor(role);
		n.icon = FlowRoleIcon(role);
		int shown = 0;
		for (auto& b : Arr(d, "behaviors"))
		{
			if (!b.is_string() || shown >= 4) continue;
			GraphRow row;
			row.key = shown == 0 ? "하는 일" : "";
			row.value = b.get<std::string>();
			n.rows.push_back(row);
			++shown;
		}
		if (Arr(d, "behaviors").size() > 4) n.footer = Fmt("하는 일 %d가지", (int)Arr(d, "behaviors").size());
		if (d.value("tracked", false)) n.footer += (n.footer.empty() ? "" : " · ") + std::string("파일 분석 있음");
		if (d.contains("inPlay")) n.footer += (n.footer.empty() ? "" : " · ") + Fmt("플레이에서 확인 (%d세션)", d["inPlay"].value("sessions", 0));
		n.layer = level[k];
		index[k] = (int)nodes.size();
		nodes.push_back(std::move(n));
	}
	std::vector<GraphLink> links;
	std::set<std::pair<int, int>> seen;
	for (auto& [from, e] : used)
	{
		std::string to = Str(*e, "to");
		std::string gid = from + "#" + Str(*e, "kind");
		if (g_flow.groups.count(gid) && std::find(g_flow.groups[gid].begin(), g_flow.groups[gid].end(), to) != g_flow.groups[gid].end()) to = gid;
		int a = index.count(from) ? index[from] : -1;
		int b = index.count(to) ? index[to] : -1;
		if (a < 0 || b < 0 || !seen.insert({ a, b }).second) continue;
		GraphLink l;
		l.from = a;
		l.to = b;
		std::string kind = Str(*e, "kind");
		const json& labels = Arr(*e, "labels");
		l.label = to == gid ? Str(*e, "words")
		                    : !labels.empty() && labels[0].is_string() && !labels[0].get<std::string>().empty() ? labels[0].get<std::string>() : Str(*e, "words");
		if (l.label.size() > 40) l.label = l.label.substr(0, 37) + "…";
		l.dashed = !(kind == "slot" || kind == "possesses" || kind == "spawns" || kind == "creates" || kind == "has" || kind == "places");
		l.color = FlowEdgeColor(kind);
		links.push_back(l);
	}
	g_flow.graph.horizontal = true;
	g_flow.graph.rowLimit = 4;
	g_flow.graph.minFrameZoom = 0.55f;      // keep names readable; the rest is reached by panning
	g_flow.graph.Set(std::move(nodes), std::move(links));
	g_flow.graph.RequestLayout();
}

static void DrawFlowNav(App& app)
{
	const json* doc = FlowDoc(app);
	if (!doc)
	{
		TextDim("분석 기록이 없거나, 이 기능이 생기기 전의 분석입니다. 분석을 다시 실행하세요.");
		ImGui::BeginDisabled(!app.attached || app.runner.running);
		if (ImGui::Button((std::string(ICON_PLAY) + "  분석 실행").c_str(), ImVec2(-FLT_MIN, 0))) app.Run("analyze");
		ImGui::EndDisabled();
		return;
	}
	const json& roots = Arr(*doc, "roots");
	if (g_flow.root.empty() && !roots.empty()) g_flow.root = Str(roots[0], "key");
	auto rootList = [&](const char* kind, const char* title, const char* tip) {
		std::vector<const json*> list;
		for (auto& r : roots) if (Str(r, "kind") == kind) list.push_back(&r);
		if (list.empty()) return;
		SectionHeader(title, std::to_string(list.size()).c_str());
		if (ImGui::IsItemHovered()) Tooltip(tip);
		for (auto* r : list)
		{
			std::string key = Str(*r, "key");
			std::string label = Str(*r, "name") + (r->value("default", false) ? "  (기본)" : "");
			if (ImGui::Selectable((label + "##" + key).c_str(), g_flow.root == key)) { g_flow.root = key; g_flow.sel.clear(); }
			if (r->value("default", false) && ImGui::IsItemHovered()) Tooltip("프로젝트 설정의 기본값");
		}
	};
	rootList("map", "레벨에서 시작", "맵 → 그 맵의 게임 모드와 배치된 객체");
	rootList("gamemode", "게임 모드에서 시작", "게임 모드 → 컨트롤러·폰·HUD·상태 → 그들이 만들고 쓰는 것");
	rootList("gameinstance", "게임 인스턴스", "게임 전체에서 살아 있는 객체");

	SectionHeader("보기");
	ImGui::SetNextItemWidth(-FLT_MIN);
	ImGui::SliderInt("##depth", &g_flow.depth, 1, 8, "깊이 %d단계");
	Tooltip("시작점에서 몇 단계까지 따라갈지");
	ImGui::PushStyleColor(ImGuiCol_FrameBg, IM_COL32(0x3A, 0x3C, 0x41, 255));
	ImGui::Checkbox("호출·형변환·참조도 보기", &g_flow.weak);
	ImGui::PopStyleColor();
	Tooltip("끄면 '누가 무엇을 만들고 가지고 쓰는지'만, 켜면 함수 호출, Cast To, 변수·에셋 참조까지 선으로 그립니다.");

	const json& un = Arr(*doc, "unreached");
	SectionHeader("흐름에서 안 보이는 클래스", std::to_string(un.size()).c_str());
	if (ImGui::IsItemHovered()) Tooltip("어떤 시작점에서도 이어지지 않는 클래스: 다른 방식(데이터, 리플렉션, 맵 밖 스폰, 서브시스템 자동 생성)으로 쓰이거나 쓰이지 않는 것");
	SearchBox("##flowfilter", g_flow.filter, sizeof(g_flow.filter), "필터", -FLT_MIN);
	ImGui::BeginChild("flowun");
	std::map<std::string, std::vector<const json*>> byRole;
	for (auto& u : un) if (ContainsNoCase(Str(u, "name"), g_flow.filter)) byRole[Str(u, "roleWords")].push_back(&u);
	for (auto& [role, items] : byRole)
	{
		if (ImGui::TreeNodeEx((role + Fmt("  (%d)", (int)items.size()) + "##" + role).c_str(), ImGuiTreeNodeFlags_SpanAvailWidth | (g_flow.filter[0] ? ImGuiTreeNodeFlags_DefaultOpen : 0)))
		{
			for (auto* u : items)
			{
				std::string key = Str(*u, "key");
				if (ImGui::Selectable((Str(*u, "name") + (Str(*u, "source") == "bp" ? "  BP" : "  C++") + "##" + key).c_str(), g_flow.root == key))
				{
					g_flow.root = key;
					g_flow.sel = key;
				}
			}
			ImGui::TreePop();
		}
	}
	ImGui::EndChild();
}

static void DrawFlowMain(App& app)
{
	const json* doc = FlowDoc(app);
	if (!doc)
	{
		if (EmptyState(ICON_LAYERS, "게임 흐름이 아직 없습니다", "분석을 실행하면 맵 → 게임 모드 → 컨트롤러·폰·HUD → 그들이 만들고 쓰는 객체 순서로 프로젝트가 어떻게 돌아가는지 그립니다.",
		               app.attached ? "분석 실행" : nullptr))
			app.Run("analyze");
		return;
	}
	ViewHeader(ICON_LAYERS, "게임 흐름", FlowName(*doc, g_flow.root) + "에서 시작  ·  프로젝트 객체 " + std::to_string(doc->contains("nodes") ? doc->at("nodes").size() : 0) + " · 연결 " + std::to_string(Arr(*doc, "edges").size()));
	BuildFlowGraph(*doc);
	if (ToolButton(ICON_REFRESH, "자동 배치")) g_flow.graph.RequestLayout();
	ImGui::SameLine();
	if (ToolButton(ICON_SEARCH, "전체 보기 (F)")) g_flow.graph.RequestFrame();
	ImGui::SameLine();
	TextDim(Fmt("노드 %d · 연결 %d  ·  클릭: 자세히 · 더블클릭: 거기서 시작 · 실선: 만들고 가지고 배치 · 점선: 사용·호출·참조",
	            (int)g_flow.graph.nodes.size(), (int)g_flow.graph.links.size()));
	if (g_flow.graph.nodes.size() <= 1)
		TextDim("이 시작점에서 이어지는 객체가 없습니다. 깊이를 늘리거나 '호출·형변환·참조도 보기'를 켜 보세요.");
	int clicked = g_flow.graph.Draw("##flowcanvas", ImVec2(0, 0));
	if (clicked >= 0) g_flow.sel = g_flow.graph.nodes[clicked].id;
	if (g_flow.graph.hovered >= 0 && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left))
	{
		const std::string id = g_flow.graph.nodes[g_flow.graph.hovered].id;
		if (g_flow.groups.count(id)) g_flow.expanded.insert(id);       // open the group in place
		else { g_flow.root = id; g_flow.sel = id; }
	}
}

static void DrawFlowInspector(App& app)
{
	const json* doc = FlowDoc(app);
	std::string key = g_flow.sel.empty() ? g_flow.root : g_flow.sel;
	if (!doc || key.empty()) { TextDim("객체를 고르세요."); return; }
	const json& all = doc->contains("nodes") ? doc->at("nodes") : json::object();
	if (g_flow.groups.count(key))
	{
		const auto& members = g_flow.groups.at(key);
		ImGui::PushFont(g_fonts.bold);
		ImGui::TextUnformatted((FlowName(*doc, key.substr(0, key.rfind('#'))) + Fmt("에서 %d개", (int)members.size())).c_str());
		ImGui::PopFont();
		if (ImGui::Button((std::string(ICON_ADD) + "  그래프에 펼치기").c_str(), ImVec2(-FLT_MIN, 0))) g_flow.expanded.insert(key);
		SectionHeader("묶인 객체");
		for (auto& m : members)
		{
			const json& md = all.contains(m) ? all[m] : json::object();
			if (ImGui::Selectable((FlowName(*doc, m) + "  " + Str(md, "roleWords") + "##" + m).c_str(), false)) g_flow.sel = m;
		}
		return;
	}
	const json& d = all.contains(key) ? all[key] : json::object();
	ImGui::PushFont(g_fonts.bold);
	ImGui::TextUnformatted(FlowName(*doc, key).c_str());
	ImGui::PopFont();
	TextColored(FlowRoleColor(Str(d, "role")), (Str(d, "roleWords") + (Str(d, "source") == "bp" ? "  · 블루프린트" : Str(d, "source") == "cpp" ? "  · C++" : "")).c_str());
	std::string chain;
	for (auto& c : Arr(d, "chain")) chain += (chain.empty() ? "" : " < ") + c.get<std::string>();
	if (!chain.empty()) TextDim("부모: " + chain);
	if (!Str(d, "path").empty()) TextDim(Str(d, "path"));

	bool isClass = key.rfind("bp:", 0) == 0 || key.rfind("uclass:", 0) == 0;
	if (isClass)
	{
		ImGui::Spacing();
		if (ImGui::Button((std::string(ICON_BOLT) + "  기능 보기").c_str(), ImVec2(-FLT_MIN, 0)))
		{
			ui.archMode = 1;
			ui.archSel = key;
			ui.featSel.clear();
			ui.archNode.clear();
			app.requestActivity = Activity::Architecture;
		}
		Tooltip("아키텍처 › 기능: 이 클래스의 진입점과 각 진입점이 펼쳐지는 순서");
		std::string package = Str(d, "path");
		if (key.rfind("bp:", 0) == 0 && package.rfind("/", 0) == 0)
		{
			if (d.value("tracked", false))
			{
				if (ImGui::Button((std::string(ICON_PAGE) + "  파일 분석 열기").c_str(), ImVec2(-FLT_MIN, 0)))
				{
					g_files.sel = package.substr(1);
					app.requestActivity = Activity::Files;
				}
			}
			else
			{
				ImGui::BeginDisabled(!app.lensInstalled || app.runner.running);
				if (ImGui::Button((std::string(ICON_ADD) + "  추적하고 파일 분석").c_str(), ImVec2(-FLT_MIN, 0)))
				{
					auto list = ReadTargetList(BpTrackPath(app));
					list.push_back(package);
					WriteTargetList(BpTrackPath(app), "Blueprints followed by dwookit (bp add / bp remove). One package path or folder (ending with /) per line.", list);
					g_files.sel = package.substr(1);
					app.Run("bp export");
				}
				ImGui::EndDisabled();
				Tooltip("이 블루프린트를 추적 목록에 넣고 내보냅니다(에디터를 닫고). 끝나면 파일 분석에서 구성·행동·흐름·논리를 봅니다.");
			}
		}
	}
	if (ImGui::Button((std::string(ICON_RIGHT) + "  여기서 시작").c_str(), ImVec2(-FLT_MIN, 0))) g_flow.root = key;
	if (isClass)
	{
		// the runtime class name DwoStateMonitor matches: BP_X_C for Blueprints, the class without A/U for C++
		std::string cls = key.substr(key.find_last_of('.') + 1);
		bool watched = std::find(app.liveSettings.watch.begin(), app.liveSettings.watch.end(), cls) != app.liveSettings.watch.end();
		ImGui::BeginDisabled(watched || app.stateMonitor == 0);
		if (ImGui::Button((std::string(ICON_EYE) + (watched ? "  실행 중 관찰 중" : "  실행 중 관찰에 추가")).c_str(), ImVec2(-FLT_MIN, 0)))
		{
			std::vector<std::string> next = app.liveSettings.watch;
			next.push_back(cls);
			SetSmWatch(app, next);
			PushToast("실행 중 관찰에 추가", cls + " · 게임을 실행하면 값이 바뀌는 대로 보입니다", col::Pass);
		}
		ImGui::EndDisabled();
		Tooltip(app.stateMonitor == 0 ? "DwoStateMonitor 플러그인을 설치하면 실행 중인 게임에서 이 객체의 값을 볼 수 있습니다."
		                              : "게임(PIE)이 실행되는 동안 이 클래스의 객체와 그것이 가리키는 객체의 값을 '실행 중 관찰' 화면에서 봅니다.");
	}

	if (!Arr(d, "behaviors").empty())
	{
		SectionHeader("하는 일");
		for (auto& b : Arr(d, "behaviors")) if (b.is_string()) { ImGui::Bullet(); ImGui::TextWrapped("%s", b.get<std::string>().c_str()); }
	}
	auto edgeList = [&](bool outgoing) {
		std::vector<const json*> list;
		for (auto& e : Arr(*doc, "edges")) if (Str(e, outgoing ? "from" : "to") == key) list.push_back(&e);
		if (list.empty()) return;
		SectionHeader(outgoing ? "이 객체가" : "이 객체를", std::to_string(list.size()).c_str());
		for (auto* e : list)
		{
			std::string other = Str(*e, outgoing ? "to" : "from");
			std::string words = Str(*e, "words");
			const json& labels = Arr(*e, "labels");
			std::string label = !labels.empty() && labels[0].is_string() ? labels[0].get<std::string>() : "";
			std::string line = outgoing ? (FlowName(*doc, other) + "  " + (words.empty() ? label : words + (label.empty() ? "" : " · " + label)))
			                            : (FlowName(*doc, other) + "이(가) " + (words.empty() ? label : words + (label.empty() ? "" : " · " + label)));
			ImGui::PushID(e);
			ImGui::PushStyleColor(ImGuiCol_Text, V4(FlowEdgeColor(Str(*e, "kind"))));
			if (ImGui::Selectable(line.c_str(), false)) g_flow.sel = other;
			ImGui::PopStyleColor();
			std::string ev;
			for (auto& x : Arr(*e, "evidence")) if (x.is_string()) ev += x.get<std::string>() + "\n";
			if (!ev.empty()) Tooltip(("근거\n" + ev).c_str());
			ImGui::PopID();
		}
	};
	edgeList(true);
	edgeList(false);
}
