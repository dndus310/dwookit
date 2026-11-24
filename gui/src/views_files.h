// DwoLens 파일 분석: the tracked Blueprints, one at a time, as a person reads them: what the object is made of,
// what it does (behaviors), how each behavior unfolds (a step tree that goes into own functions, other tracked
// Blueprints and the project's C++), and under every step the pseudocode line (logic).
// Data: __dwookit/blueprints/<path>/<Name>.explain.json (tools/lens_explain.py), <Name>.agent.json if an agent
// wrote better wording.
#pragma once

#include "views.h"

struct FilesState
{
	std::string sel;                 // export dir of the selected Blueprint
	char filter[128] = {};
	bool showLogic = false;
	fs::path loadedPath;
	fs::file_time_type loadedTime;
	json doc, agent;
	std::string openBehavior;
};
static FilesState g_files;

struct TrackedFile
{
	std::string name, dir, package;
};

static std::vector<TrackedFile> TrackedFiles(App& app)
{
	std::vector<TrackedFile> out;
	auto manifest = ReadJson(app.ForgeDir() / "blueprints" / "export.json");
	if (!manifest) return out;
	for (auto& e : Arr(*manifest, "exported")) out.push_back({ Str(e, "name"), Str(e, "dir"), Str(e, "package") });
	std::sort(out.begin(), out.end(), [](const TrackedFile& a, const TrackedFile& b) { return a.dir < b.dir; });
	return out;
}

static fs::path FileFolder(App& app, const TrackedFile& f) { return app.ForgeDir() / "blueprints" / fs::u8path(f.dir); }

static void LoadExplain(App& app, const TrackedFile& f)
{
	fs::path p = FileFolder(app, f) / fs::u8path(f.name + ".explain.json");
	std::error_code ec;
	auto t = fs::last_write_time(p, ec);
	if (p == g_files.loadedPath && !ec && t == g_files.loadedTime) return;
	g_files.loadedPath = p;
	g_files.loadedTime = t;
	auto d = ReadJson(p);
	g_files.doc = d ? *d : json();
	auto a = ReadJson(FileFolder(app, f) / fs::u8path(f.name + ".agent.json"));
	g_files.agent = a ? *a : json();
}

static void DrawFilesNav(App& app)
{
	auto files = TrackedFiles(app);
	SectionHeader("추적 중인 파일", std::to_string(files.size()).c_str());
	if (ImGui::Button((std::string(ICON_LIST) + "  목록에서 고르기").c_str(), ImVec2(-FLT_MIN, 0))) OpenTargetPicker(app, 0);
	Tooltip("프로젝트의 블루프린트를 트리에서 골라 추적합니다. 목록: __dwookit/bp-track.txt");
	bool lens = app.lensInstalled;
	ImGui::BeginDisabled(!lens || app.runner.running);
	if (ImGui::Button((std::string(ICON_REFRESH) + "  다시 내보내기 (bp export)").c_str(), ImVec2(-FLT_MIN, 0))) app.Run("bp export");
	ImGui::EndDisabled();
	Tooltip(lens ? "에디터를 닫고 실행하세요. 고른 블루프린트만 로드해 의사코드·전체 스크립트·설명을 다시 만듭니다."
	             : "DwoLens가 설치되어 있지 않습니다. 런처에서 DwoLens를 설치하세요.");
	SearchBox("##filesfilter", g_files.filter, sizeof(g_files.filter), "필터", -FLT_MIN);
	ImGui::BeginChild("fileslist");
	if (files.empty()) TextDim("아직 내보낸 파일이 없습니다. '목록에서 고르기'로 고르고 내보내세요.");
	std::string lastFolder;
	for (auto& f : files)
	{
		if (!ContainsNoCase(f.package, g_files.filter)) continue;
		std::string folder = f.package.substr(0, f.package.find_last_of('/'));
		if (folder != lastFolder) { TextColored(col::Muted, folder.c_str()); lastFolder = folder; }
		ImGui::PushID(f.dir.c_str());
		ImGui::Indent(S(8));
		if (ImGui::Selectable((std::string(ICON_PAGE) + "  " + f.name).c_str(), g_files.sel == f.dir)) g_files.sel = f.dir;
		ImGui::Unindent(S(8));
		ImGui::PopID();
	}
	ImGui::EndChild();
}

static ImU32 StepColor(const std::string& kind)
{
	if (kind == "note") return col::Warn;
	if (kind == "if" || kind == "else" || kind == "loop" || kind == "on") return col::Info;
	if (kind == "own" || kind == "other") return col::Purple;
	if (kind == "cpp") return col::Teal;
	if (kind == "goto" || kind == "label" || kind == "more") return col::Muted;
	if (kind == "set") return col::Pass;
	return col::Text;
}

static void DrawCppCode(const char* id, const json& c, const std::string& prefix)
{
	std::string head = std::string(ICON_CODE) + "  " + prefix + Str(c, "class") + "::" + Str(c, "function") + "  " + Str(c, "file") + ":" + std::to_string(c.value("line", 0));
	if (ImGui::TreeNodeEx(id, ImGuiTreeNodeFlags_SpanAvailWidth, "%s", head.c_str()))
	{
		std::string code;
		for (auto& l : Arr(c, "code")) if (l.is_string()) code += l.get<std::string>() + "\n";
		if (c.value("more", 0) > 0) code += Fmt("// … %d줄 더", c.value("more", 0));
		ImGui::PushStyleColor(ImGuiCol_Text, V4(col::Dim));
		ImGui::TextUnformatted(code.c_str());
		ImGui::PopStyleColor();
		ImGui::TreePop();
	}
}

static void DrawSteps(const json& steps, int depth)
{
	int i = 0;
	for (auto& s : steps)
	{
		ImGui::PushID(i++);
		std::string kind = Str(s, "kind");
		std::string text = Str(s, "text");
		std::string detail = Str(s, "detail");
		const json& kids = Arr(s, "children");
		bool hasCpp = s.contains("cpp");
		ImGui::PushStyleColor(ImGuiCol_Text, V4(StepColor(kind)));
		std::string label = (kind == "note" ? "# " : "") + text;
		if (!kids.empty() || hasCpp)
		{
			bool openDefault = kind == "if" || kind == "else" || kind == "on" || kind == "loop" || depth == 0;
			ImGui::SetNextItemOpen(openDefault, ImGuiCond_Once);
			bool open = ImGui::TreeNodeEx("##step", ImGuiTreeNodeFlags_SpanAvailWidth, "%s", label.c_str());
			ImGui::PopStyleColor();
			if (!detail.empty() && detail != text) Tooltip(("의사코드: " + detail).c_str());
			if (g_files.showLogic && !detail.empty() && detail != text) { ImGui::SameLine(); TextColored(col::Muted, detail.c_str()); }
			if (open)
			{
				if (hasCpp) DrawCppCode("##cpp", s["cpp"], "C++ ");
				DrawSteps(kids, depth + 1);
				ImGui::TreePop();
			}
		}
		else
		{
			ImGui::Bullet();
			ImGui::TextWrapped("%s", label.c_str());
			ImGui::PopStyleColor();
			if (!detail.empty() && detail != text) Tooltip(("의사코드: " + detail).c_str());
			if (g_files.showLogic && !detail.empty() && detail != text)
			{
				ImGui::Indent(S(18));
				TextColored(col::Muted, detail.c_str());
				ImGui::Unindent(S(18));
			}
		}
		ImGui::PopID();
	}
}

static void DrawCompositionTable(const char* id, const json& items, const std::vector<std::pair<const char*, const char*>>& cols)
{
	if (items.empty()) return;
	if (!ImGui::BeginTable(id, (int)cols.size(), ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_SizingStretchProp)) return;
	for (auto& c : cols) ImGui::TableSetupColumn(c.second);
	ImGui::TableHeadersRow();
	for (auto& it : items)
	{
		ImGui::TableNextRow();
		int ci = 0;
		for (auto& c : cols)
		{
			ImGui::TableSetColumnIndex(ci++);
			std::string v;
			if (it.contains(c.first))
			{
				const json& x = it[c.first];
				if (x.is_array()) { for (auto& e : x) v += (v.empty() ? "" : ", ") + (e.is_string() ? e.get<std::string>() : e.dump()); }
				else if (x.is_string()) v = x.get<std::string>();
				else if (x.is_boolean()) v = x.get<bool>() ? "예" : "";
				else v = x.dump();
			}
			ImGui::TextWrapped("%s", v.c_str());
		}
	}
	ImGui::EndTable();
}

static void DrawFilesMain(App& app)
{
	auto files = TrackedFiles(app);
	if (files.empty())
	{
		TextDim("추적 중인 블루프린트가 없습니다.");
		ImGui::Spacing();
		ImGui::TextWrapped("왼쪽 '목록에서 고르기'로 볼 블루프린트를 고르고 내보내면, 파일마다 무엇으로 이루어졌는지(구성), "
		                   "무엇을 하는지(행동), 어떻게 이어지는지(흐름), 그 아래 논리(의사코드)를 이 화면에서 봅니다.");
		return;
	}
	auto it = std::find_if(files.begin(), files.end(), [](const TrackedFile& f) { return f.dir == g_files.sel; });
	if (it == files.end()) { g_files.sel = files.front().dir; it = files.begin(); }
	LoadExplain(app, *it);
	const json& d = g_files.doc;
	if (d.is_null())
	{
		TextDim("설명 파일이 없습니다. '다시 내보내기'를 실행하세요 (이 버전부터 만들어집니다).");
		return;
	}
	ImGui::PushFont(nullptr);
	ImGui::SetWindowFontScale(1.35f);
	ImGui::TextUnformatted(Str(d, "name").c_str());
	ImGui::SetWindowFontScale(1.0f);
	ImGui::PopFont();
	std::string chain;
	for (auto& c : Arr(d, "parentChain")) chain += (chain.empty() ? "" : " < ") + c.get<std::string>();
	TextDim(Str(d, "kind") + "  ·  " + (chain.empty() ? Str(d, "parent") : chain) + "  ·  " + Str(d, "package"));
	ImGui::Spacing();

	// 1. summary
	ImGui::PushStyleColor(ImGuiCol_ChildBg, V4(col::Panel));
	ImGui::BeginChild("summary", ImVec2(0, 0), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
	ImGui::TextWrapped("%s", Str(d, "summary").c_str());
	if (g_files.agent.is_object() && !Str(g_files.agent, "summary").empty())
	{
		ImGui::Spacing();
		TextColored(col::Accent, "에이전트 설명");
		ImGui::TextWrapped("%s", Str(g_files.agent, "summary").c_str());
		for (auto& n : Arr(g_files.agent, "notes")) if (n.is_string()) { ImGui::Bullet(); ImGui::TextWrapped("%s", n.get<std::string>().c_str()); }
	}
	ImGui::EndChild();
	ImGui::PopStyleColor();

	// 2. composition
	const json& c = d.contains("composition") ? d["composition"] : json::object();
	if (ImGui::CollapsingHeader("구성 — 무엇으로 이루어졌나", ImGuiTreeNodeFlags_DefaultOpen))
	{
		if (!Arr(c, "components").empty())
		{
			TextColored(col::Dim, "컴포넌트");
			DrawCompositionTable("comps", c["components"], { { "name", "이름" }, { "role", "역할" }, { "class", "클래스" }, { "parent", "붙은 곳" }, { "changed", "바꾼 값" } });
		}
		if (!Arr(c, "settings").empty())
		{
			TextColored(col::Dim, "설정값 (에디터에서 바꾸는 값)");
			DrawCompositionTable("settings", c["settings"], { { "name", "이름" }, { "type", "타입" }, { "default", "기본값" }, { "changedBy", "바꾸는 행동" }, { "tooltip", "설명" } });
		}
		if (!Arr(c, "state").empty())
		{
			TextColored(col::Dim, "상태 (실행 중 바뀌는 값)");
			DrawCompositionTable("state", c["state"], { { "name", "이름" }, { "type", "타입" }, { "default", "기본값" }, { "changedBy", "바꾸는 행동" } });
		}
		static const std::pair<const char*, const char*> lists[] = { { "signals", "밖으로 보내는 신호" }, { "timelines", "타임라인" },
		                                                              { "interfaces", "구현한 인터페이스" }, { "defaults", "바꾼 기본값" }, { "widgets", "위젯" } };
		for (auto& [key, label] : lists)
		{
			std::string v;
			for (auto& x : Arr(c, key)) if (x.is_string()) v += (v.empty() ? "" : ", ") + x.get<std::string>();
			if (v.empty()) continue;
			TextColored(col::Dim, label);
			ImGui::SameLine();
			ImGui::TextWrapped("%s", v.c_str());
		}
	}

	// 3-5. behaviors, flow, logic
	if (ImGui::CollapsingHeader("행동과 흐름 — 무엇을 하고 어떻게 이어지나", ImGuiTreeNodeFlags_DefaultOpen))
	{
		ImGui::PushStyleColor(ImGuiCol_FrameBg, IM_COL32(0x3A, 0x3C, 0x41, 255));
		ImGui::Checkbox("단계마다 의사코드 보기 (논리)", &g_files.showLogic);
		ImGui::PopStyleColor();
		Tooltip("각 단계가 나온 의사코드 줄을 함께 보여 줍니다. 마우스를 올려도 볼 수 있습니다.");
		for (auto& b : Arr(d, "behaviors"))
		{
			std::string id = Str(b, "id");
			ImGui::PushID(id.c_str());
			ImGui::Spacing();
			ImGui::PushStyleColor(ImGuiCol_Header, V4(col::Panel2));
			bool open = ImGui::CollapsingHeader((Str(b, "title") + "###beh").c_str(), ImGuiTreeNodeFlags_DefaultOpen);
			ImGui::PopStyleColor();
			if (open)
			{
				ImGui::Indent(S(10));
				TextColored(col::Info, (std::string(ICON_BOLT) + "  " + Str(b, "trigger")).c_str());
				for (auto& n : Arr(b, "notes")) if (n.is_string()) TextColored(col::Warn, ("“" + n.get<std::string>() + "”").c_str());
				std::string agentText = g_files.agent.is_object() && g_files.agent.contains("behaviors") ? Str(g_files.agent["behaviors"], id.c_str()) : "";
				if (!agentText.empty()) { TextColored(col::Accent, "에이전트 설명"); ImGui::TextWrapped("%s", agentText.c_str()); }
				std::string meta;
				auto join = [](const json& a) { std::string s; for (auto& x : a) if (x.is_string()) s += (s.empty() ? "" : ", ") + x.get<std::string>(); return s; };
				if (!Arr(b, "writes").empty()) meta += "바꾸는 값: " + join(b["writes"]) + "   ";
				if (!Arr(b, "calls").empty()) meta += "이어서 실행: " + join(b["calls"]) + "   ";
				if (!Arr(b, "calledBy").empty()) meta += "부르는 곳: " + join(b["calledBy"]);
				if (!meta.empty()) TextDim(meta);
				int oi = 0;
				for (auto& o : Arr(b, "origins")) { ImGui::PushID(oi++); DrawCppCode("##origin", o, "이 행동을 부르는 C++ "); ImGui::PopID(); }
				DrawSteps(Arr(b, "steps"), 0);
				ImGui::Unindent(S(10));
			}
			ImGui::PopID();
		}
	}
}

static void DrawFilesInspector(App& app)
{
	auto files = TrackedFiles(app);
	auto it = std::find_if(files.begin(), files.end(), [](const TrackedFile& f) { return f.dir == g_files.sel; });
	if (it == files.end()) { TextDim("파일을 고르세요."); return; }
	fs::path folder = FileFolder(app, *it);
	SectionHeader("열기");
	if (ImGui::Button((std::string(ICON_OPEN) + "  HTML 페이지").c_str(), ImVec2(-FLT_MIN, 0))) OpenWithShell(folder / fs::u8path(it->name + ".html"));
	Tooltip("같은 내용을 브라우저에서 봅니다. 공유할 때 씁니다.");
	if (ImGui::Button((std::string(ICON_PAGE) + "  의사코드 (.md)").c_str(), ImVec2(-FLT_MIN, 0))) OpenWithShell(folder / fs::u8path(it->name + ".md"));
	if (ImGui::Button((std::string(ICON_FOLDER) + "  폴더").c_str(), ImVec2(-FLT_MIN, 0))) RevealInExplorer(folder / fs::u8path(it->name + ".json"));
	SectionHeader("에이전트 설명");
	ImGui::TextWrapped("Claude Code나 Copilot에게 더 자연스러운 설명을 맡길 수 있습니다. 요청 문장을 복사해 에이전트에 붙여 넣으면 "
	                   "<이름>.agent.json을 만들고, 이 화면과 HTML에 함께 표시됩니다.");
	std::string rel = "__dwookit/blueprints/" + it->dir + "/" + it->name + ".explain-prompt.md";
	if (ImGui::Button((std::string(ICON_COPY) + "  요청 문장 복사").c_str(), ImVec2(-FLT_MIN, 0)))
	{
		CopyToClipboard("Read " + rel + " and do exactly what it says.");
		PushToast("복사됨", "에이전트 대화창에 붙여 넣으세요.", col::Pass);
	}
	if (g_files.agent.is_object()) TextColored(col::Pass, "에이전트 설명 있음");
	const json& d = g_files.doc;
	if (d.is_object())
	{
		SectionHeader("요약");
		const json& c = d.contains("composition") ? d["composition"] : json::object();
		KeyValue("컴포넌트", std::to_string(Arr(c, "components").size()));
		KeyValue("설정값", std::to_string(Arr(c, "settings").size()));
		KeyValue("상태", std::to_string(Arr(c, "state").size()));
		KeyValue("행동", std::to_string(Arr(d, "behaviors").size()));
	}
}
