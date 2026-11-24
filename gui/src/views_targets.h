// Picking what to follow from lists instead of typing names: the project's Blueprints (DwoLens tracking,
// __dwookit/bp-track.txt) and Blueprint/C++ classes (DwoStateMonitor watching, __dwookit/sm-watch.txt).
// The candidates come from tools/lens_targets.py, which reads the project files (no editor needed).
#pragma once

#include "app.h"

struct TargetsProbe
{
	std::mutex mutex;
	json doc;
	fs::path docProject;
	std::string error;
	std::atomic<bool> busy{ false };
	std::thread worker;
	~TargetsProbe() { if (worker.joinable()) worker.join(); }

	void Start(const fs::path& home, const fs::path& project)
	{
		if (busy || project.empty()) return;
		if (worker.joinable()) worker.join();
		busy = true;
		std::wstring cmd = L"python \"" + (home / "tools" / "lens_targets.py").wstring() + L"\" --project \"" + project.wstring() + L"\"";
		worker = std::thread([this, cmd, project] {
			std::string out;
			DWORD code = 1;
			json parsed;
			std::string err;
			if (!CaptureOutput(cmd, project, out, code)) err = "python을 실행할 수 없습니다 (PATH 확인).";
			else
			{
				size_t at = out.find('{');
				try { parsed = json::parse(out.substr(at == std::string::npos ? 0 : at)); }
				catch (...) { err = ConsoleToUtf8(out); }
			}
			{
				std::lock_guard<std::mutex> lock(mutex);
				if (err.empty()) { doc = parsed; docProject = project; }
				error = err;
			}
			busy = false;
		});
	}

	json Get(const fs::path& project)
	{
		std::lock_guard<std::mutex> lock(mutex);
		return docProject == project ? doc : json();
	}
};
static TargetsProbe g_targets;

// ---------------------------------------------------------------- list files

static std::vector<std::string> ReadTargetList(const fs::path& path)
{
	std::vector<std::string> out;
	std::ifstream in(path, std::ios::binary);
	std::string line;
	while (std::getline(in, line))
	{
		if (!line.empty() && line.back() == '\r') line.pop_back();
		if (line.size() >= 3 && (unsigned char)line[0] == 0xEF && (unsigned char)line[1] == 0xBB && (unsigned char)line[2] == 0xBF) line = line.substr(3);
		size_t a = line.find_first_not_of(" \t");
		if (a == std::string::npos || line[a] == '#') continue;
		line = line.substr(a);
		size_t b = line.find_last_not_of(" \t");
		out.push_back(line.substr(0, b + 1));
	}
	return out;
}

static void WriteTargetList(const fs::path& path, const std::string& header, std::vector<std::string> entries)
{
	std::sort(entries.begin(), entries.end());
	entries.erase(std::unique(entries.begin(), entries.end()), entries.end());
	std::error_code ec;
	fs::create_directories(path.parent_path(), ec);
	std::ofstream out(path, std::ios::binary);
	out << "# " << header << "\n";
	for (auto& e : entries) out << e << "\n";
}

static fs::path BpTrackPath(App& app) { return app.ForgeDir() / "bp-track.txt"; }
static fs::path SmWatchPath(App& app) { return app.ForgeDir() / "sm-watch.txt"; }

static std::vector<std::string> SmWatchClasses(App& app)
{
	std::vector<std::string> out;
	for (auto& line : ReadTargetList(SmWatchPath(app))) out.push_back(line.substr(0, line.find_first_of(" \t")));
	return out;
}

// Applies the picked classes to running games right away and keeps the project list in sync.
static void SetSmWatch(App& app, const std::vector<std::string>& classes)
{
	auto& cur = app.liveSettings.watch;
	for (auto& c : cur)
		if (std::find(classes.begin(), classes.end(), c) == classes.end()) app.live.Broadcast({ { "t", "unwatch" }, { "class", c } });
	for (auto& c : classes)
		if (std::find(cur.begin(), cur.end(), c) == cur.end()) app.live.Broadcast({ { "t", "watch" }, { "class", c }, { "max", 16 } });
	cur = classes;
	app.SaveSettings();
	if (!app.project.empty() && fs::exists(app.ForgeDir()))
		WriteTargetList(SmWatchPath(app), "Classes DwoStateMonitor watches while the game runs (picked in the dwookit window). One class per line, optional max count.", classes);
}

// ---------------------------------------------------------------- picker

struct TargetRow
{
	std::string key;        // what goes into the list: package path (Lens) or runtime class (StateMonitor)
	std::string name;
	std::string folder;     // tree path
	std::string kind;       // blueprint, widget, anim, rig, cpp
	std::string detail;
};

struct TargetPicker
{
	bool open = false;
	int mode = 0;                       // 0 Lens (Blueprints to track), 1 StateMonitor (classes to watch)
	std::set<std::string> picked;
	char filter[128] = {};
	int kindFilter = 0;                 // 0 all, 1 Blueprint actors/objects, 2 widgets, 3 C++
	bool exportAfter = true;
	bool seeded = false;
};
static TargetPicker g_picker;

static void OpenTargetPicker(App& app, int mode)
{
	g_picker = TargetPicker();
	g_picker.open = true;
	g_picker.mode = mode;
	g_targets.Start(app.home, app.project);
}

static std::vector<TargetRow> TargetRows(const json& doc, int mode)
{
	std::vector<TargetRow> rows;
	for (auto& b : Arr(doc, "blueprints"))
	{
		std::string kind = Str(b, "kind");
		if (mode == 1 && (kind == "anim" || kind == "rig")) continue;          // not watchable state
		std::string package = Str(b, "package");
		size_t slash = package.find_last_of('/');
		rows.push_back({ mode == 0 ? package : Str(b, "runtimeClass"), Str(b, "name"),
		                 (slash == std::string::npos ? package : package.substr(0, slash)), kind, package });
	}
	if (mode == 1)
		for (auto& c : Arr(doc, "classes"))
		{
			if (c.value("abstract", false)) continue;
			std::string file = Str(c, "file");
			size_t slash = file.find_last_of('/');
			rows.push_back({ Str(c, "runtimeClass"), Str(c, "name"), "C++  " + (slash == std::string::npos ? file : file.substr(0, slash)), "cpp",
			                 Str(c, "name") + " : " + Str(c, "parent") + "  ·  " + file });
		}
	return rows;
}

static bool KindVisible(const std::string& kind, int filter)
{
	if (filter == 1) return kind == "blueprint";
	if (filter == 2) return kind == "widget";
	if (filter == 3) return kind == "cpp";
	return true;
}

static const char* KindLabel(const std::string& kind)
{
	if (kind == "widget") return "위젯";
	if (kind == "anim") return "애님";
	if (kind == "rig") return "리그";
	if (kind == "cpp") return "C++";
	return "BP";
}

// Folder tree with a checkbox per item and per folder (all visible items below it).
static void DrawTargetTree(std::vector<const TargetRow*>& rows, std::set<std::string>& picked, bool filtering)
{
	std::map<std::string, std::vector<const TargetRow*>> byFolder;
	for (auto* r : rows) byFolder[r->folder].push_back(r);
	for (auto& [folder, items] : byFolder)
	{
		int on = 0;
		for (auto* r : items) on += picked.count(r->key) ? 1 : 0;
		ImGui::PushID(folder.c_str());
		bool all = on == (int)items.size();
		if (on > 0 && !all) ImGui::PushItemFlag(ImGuiItemFlags_MixedValue, true);
		bool toggle = all;
		if (ImGui::Checkbox("##folder", &toggle))
			for (auto* r : items) { if (toggle) picked.insert(r->key); else picked.erase(r->key); }
		if (on > 0 && !all) ImGui::PopItemFlag();
		ImGui::SameLine();
		ImGui::SetNextItemOpen(filtering || on > 0, filtering ? ImGuiCond_Always : ImGuiCond_Once);
		bool open = ImGui::TreeNodeEx("##node", ImGuiTreeNodeFlags_SpanAvailWidth, "%s %s  (%d/%d)", ICON_FOLDER, folder.c_str(), on, (int)items.size());
		if (open)
		{
			for (auto* r : items)
			{
				ImGui::PushID(r->key.c_str());
				bool v = picked.count(r->key) > 0;
				if (ImGui::Checkbox("##item", &v)) { if (v) picked.insert(r->key); else picked.erase(r->key); }
				ImGui::SameLine();
				TextColored(col::Muted, KindLabel(r->kind));
				ImGui::SameLine();
				ImGui::TextUnformatted(r->name.c_str());
				Tooltip(r->detail.c_str());
				ImGui::PopID();
			}
			ImGui::TreePop();
		}
		ImGui::PopID();
	}
}

static void DrawTargetPicker(App& app)
{
	if (g_picker.open) { ImGui::OpenPopup("##targetpicker"); g_picker.open = false; }
	ImGui::SetNextWindowSize(ImVec2(S(760), S(620)), ImGuiCond_Appearing);
	if (!ImGui::BeginPopupModal("##targetpicker", nullptr, ImGuiWindowFlags_NoTitleBar)) return;
	bool lens = g_picker.mode == 0;
	ImGui::TextUnformatted(lens ? "추적할 블루프린트 고르기 (DwoLens)" : "실행 중 관찰할 클래스 고르기 (DwoStateMonitor)");
	TextDim(lens ? "고른 블루프린트마다 의사코드(.md)와 전체 스크립트(.json), 사람이 읽는 설명을 만듭니다. 목록: __dwookit/bp-track.txt"
	             : "게임이 돌 때 이 클래스의 객체(액터·컴포넌트·위젯)와 그 객체가 가리키는 객체를 관찰합니다. 목록: __dwookit/sm-watch.txt");
	ImGui::Separator();

	json doc = g_targets.Get(app.project);
	if (!g_picker.seeded && !doc.is_null())
	{
		if (lens) for (auto& e : ReadTargetList(BpTrackPath(app))) g_picker.picked.insert(e);
		else for (auto& c : app.liveSettings.watch) g_picker.picked.insert(c);
		g_picker.seeded = true;
	}
	if (g_targets.busy) TextDim("프로젝트 파일을 읽는 중...");
	else if (!g_targets.error.empty()) TextColored(col::Fail, g_targets.error.c_str());

	SearchBox("##pickfilter", g_picker.filter, sizeof(g_picker.filter), "이름·폴더 필터", S(260));
	if (!lens)
	{
		ImGui::SameLine();
		static const char* const kinds[] = { "전체", "블루프린트", "위젯", "C++" };
		Segmented("##pickkind", kinds, 4, g_picker.kindFilter);
	}
	std::vector<TargetRow> all = TargetRows(doc, g_picker.mode);
	std::vector<const TargetRow*> rows;
	for (auto& r : all)
		if (KindVisible(r.kind, g_picker.kindFilter) && ContainsNoCase(r.name + " " + r.folder, g_picker.filter)) rows.push_back(&r);
	// Picked entries that no longer exist (renamed, or a folder entry from bp add) stay visible so they can be removed.
	std::vector<TargetRow> missing;
	for (auto& k : g_picker.picked)
		if (std::none_of(all.begin(), all.end(), [&](const TargetRow& r) { return r.key == k; }))
			missing.push_back({ k, k, "(목록에 없음: 폴더 항목이거나 이름이 바뀜)", lens ? "blueprint" : "cpp", k });
	for (auto& m : missing) rows.push_back(&m);

	ImGui::BeginChild("##picklist", ImVec2(0, ImGui::GetContentRegionAvail().y - S(44)), ImGuiChildFlags_Borders);
	ImGui::PushStyleColor(ImGuiCol_FrameBg, IM_COL32(0x3A, 0x3C, 0x41, 255));
	ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, IM_COL32(0x46, 0x49, 0x4F, 255));
	ImGui::PushStyleColor(ImGuiCol_Border, IM_COL32(0x5A, 0x5E, 0x66, 255));
	ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 1.0f);
	if (!g_targets.busy && rows.empty()) TextDim(all.empty() ? "고를 수 있는 항목이 없습니다." : "필터에 맞는 항목이 없습니다.");
	DrawTargetTree(rows, g_picker.picked, g_picker.filter[0] != 0);
	ImGui::PopStyleVar();
	ImGui::PopStyleColor(3);
	ImGui::EndChild();

	ImGui::Text("%d개 선택", (int)g_picker.picked.size());
	ImGui::SameLine();
	if (lens) { ImGui::Checkbox("적용 후 바로 내보내기 (에디터를 닫고)", &g_picker.exportAfter); ImGui::SameLine(); }
	float w = S(90);
	ImGui::SetCursorPosX(ImGui::GetWindowWidth() - w * 2 - S(24));
	if (PrimaryButton("적용", ImVec2(w, 0)))
	{
		std::vector<std::string> list(g_picker.picked.begin(), g_picker.picked.end());
		if (lens)
		{
			WriteTargetList(BpTrackPath(app), "Blueprints followed by dwookit (bp add / bp remove). One package path or folder (ending with /) per line.", list);
			PushToast("추적 목록 저장", Fmt("블루프린트 %d개", (int)list.size()), col::Pass);
			if (g_picker.exportAfter && !list.empty()) app.Run("bp export");
		}
		else
		{
			SetSmWatch(app, list);
			PushToast("관찰 목록 저장", Fmt("클래스 %d개 · 연결된 게임에 바로 적용", (int)list.size()), col::Pass);
		}
		ImGui::CloseCurrentPopup();
	}
	ImGui::SameLine();
	if (ImGui::Button("취소", ImVec2(w, 0))) ImGui::CloseCurrentPopup();
	ImGui::EndPopup();
}
