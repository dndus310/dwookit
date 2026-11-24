// AI instructions and skills: the dwookit library (<dwookit>/library, tools/lens_library.py) and the selection a
// project gets for Claude Code and GitHub Copilot (the only agents dwookit targets). Shown as the launcher's
// "AI 지침·스킬" tab.
//   - Upload: instruction files (.md, .txt) and skills (a folder with SKILL.md, that SKILL.md, or a .zip) are copied
//     into the library by 'dwookit.ps1 library add-instruction|add-skill' (names and front matter are checked there).
//   - Not connected yet: the ticks are the library defaults (library.json), applied when the project is connected.
//   - Connected: the ticks are the project's selection (__dwookit/agent.json); 'agent use' applies a change.
//   - Uninstall removes everything dwookit put into the project for this (lens_agent.py remove).
#pragma once

#include "app.h"

namespace agentlib
{
struct Item
{
	std::string name, title, description, applyTo;
	int files = 0;
	uintmax_t bytes = 0;
};

struct State
{
	fs::path root;                       // library folder that was scanned
	std::vector<Item> instructions, skills;
	std::set<std::string> defI, defS;    // library defaults
	bool defClaude = true, defCopilot = true;
	double scannedAt = -100;
	bool wasRunning = false;
	// connected project: what it has (agent.json) and the edit in progress
	fs::path project;
	std::set<std::string> curI, curS, selI, selS;
	bool curClaude = true, curCopilot = true, selClaude = true, selCopilot = true;
	bool hasSelection = false;           // agent.json already holds a selection
	std::string confirmKind, confirmName;
	bool openConfirm = false;
};

static State& St()
{
	static State s;
	return s;
}

static fs::path LibraryRoot(const App& app)
{
	wchar_t buf[1024];
	DWORD n = GetEnvironmentVariableW(L"DWOOKIT_LIBRARY", buf, 1024);
	if (n > 0 && n < 1024) return fs::path(buf);
	return app.home / "library";
}

// Front matter value (key: value, quotes stripped) and the body after it.
static std::string FrontValue(const std::string& text, const char* key, std::string* body = nullptr)
{
	std::string t = text;
	if (t.size() >= 3 && (unsigned char)t[0] == 0xEF) t.erase(0, 3);
	std::string value;
	size_t bodyAt = 0;
	if (t.rfind("---", 0) == 0)
	{
		size_t end = t.find("\n---", 3);
		if (end != std::string::npos)
		{
			std::istringstream in(t.substr(t.find('\n') + 1, end - t.find('\n') - 1));
			std::string line, k = std::string(key) + ":";
			while (std::getline(in, line))
			{
				if (!line.empty() && line.back() == '\r') line.pop_back();
				if (line.rfind(k, 0) != 0) continue;
				value = line.substr(k.size());
				value.erase(0, value.find_first_not_of(" \t"));
				if (value.size() >= 2 && (value.front() == '"' || value.front() == '\'') && value.back() == value.front())
					value = value.substr(1, value.size() - 2);
			}
			bodyAt = t.find('\n', end + 4);
			bodyAt = bodyAt == std::string::npos ? t.size() : bodyAt + 1;
		}
	}
	if (body) *body = t.substr(bodyAt);
	return value;
}

static std::string FirstLine(const std::string& body, bool headingOnly)
{
	std::istringstream in(body);
	std::string line;
	while (std::getline(in, line))
	{
		if (!line.empty() && line.back() == '\r') line.pop_back();
		size_t a = line.find_first_not_of(" \t");
		if (a == std::string::npos) continue;
		line = line.substr(a);
		if (headingOnly)
		{
			if (line.rfind("# ", 0) == 0) return line.substr(2);
			continue;
		}
		if (line.rfind("<!--", 0) == 0 || line.rfind("```", 0) == 0) continue;
		size_t h = line.find_first_not_of('#');
		if (h == std::string::npos) continue;
		line = line.substr(h);
		line.erase(0, line.find_first_not_of(' '));
		if (!line.empty()) return line.substr(0, 200);
	}
	return "";
}

static std::set<std::string> NameSet(const json& j)
{
	std::set<std::string> out;
	if (j.is_array())
		for (auto& v : j)
			if (v.is_string()) out.insert(v.get<std::string>());
	return out;
}

static void Scan(App& app, bool force)
{
	State& s = St();
	fs::path root = LibraryRoot(app);
	bool running = app.runner.running;
	bool finished = s.wasRunning && !running;
	s.wasRunning = running;
	if (!force && !finished && root == s.root && s.project == app.project && ImGui::GetTime() - s.scannedAt < 3.0) return;
	s.scannedAt = ImGui::GetTime();
	s.root = root;
	s.instructions.clear();
	s.skills.clear();
	std::error_code ec;
	if (fs::is_directory(root / "instructions", ec))
		for (auto& e : fs::directory_iterator(root / "instructions", ec))
		{
			if (!e.is_regular_file(ec) || e.path().extension() != ".md") continue;
			std::string body, text = ReadText(e.path());
			Item it;
			it.name = PathUtf8(e.path().stem());
			it.description = FrontValue(text, "description", &body);
			it.applyTo = FrontValue(text, "applyTo");
			it.title = FrontValue(text, "title");
			if (it.title.empty()) it.title = FirstLine(body, true);
			if (it.description.empty()) it.description = FirstLine(body, false);
			it.files = 1;
			it.bytes = e.file_size(ec);
			s.instructions.push_back(it);
		}
	if (fs::is_directory(root / "skills", ec))
		for (auto& e : fs::directory_iterator(root / "skills", ec))
		{
			if (!e.is_directory(ec) || !fs::exists(e.path() / "SKILL.md", ec)) continue;
			std::string body, text = ReadText(e.path() / "SKILL.md");
			Item it;
			it.name = PathUtf8(e.path().filename());
			it.description = FrontValue(text, "description", &body);
			if (it.description.empty()) it.description = FirstLine(body, false);
			for (auto& f : fs::recursive_directory_iterator(e.path(), ec))
				if (f.is_regular_file(ec)) { it.files++; it.bytes += f.file_size(ec); }
			s.skills.push_back(it);
		}
	auto byName = [](const Item& a, const Item& b) { return a.name < b.name; };
	std::sort(s.instructions.begin(), s.instructions.end(), byName);
	std::sort(s.skills.begin(), s.skills.end(), byName);

	json lib = ReadJson(root / "library.json").value_or(json::object());
	const json& d = Obj(lib, "defaults");
	s.defI = NameSet(d.is_object() && d.contains("instructions") ? d["instructions"] : json());
	s.defS = NameSet(d.is_object() && d.contains("skills") ? d["skills"] : json());
	std::set<std::string> targets = lib.contains("targets") ? NameSet(lib["targets"]) : std::set<std::string>{ "claude", "copilot" };
	s.defClaude = targets.count("claude") > 0;
	s.defCopilot = targets.count("copilot") > 0;

	// the connected project's selection; an edit in progress survives a rescan of the same project
	bool sameProject = s.project == app.project;
	bool editing = sameProject && (s.selI != s.curI || s.selS != s.curS || s.selClaude != s.curClaude || s.selCopilot != s.curCopilot);
	s.project = app.project;
	json agent = ReadJson(app.project / "__dwookit" / "agent.json").value_or(json::object());
	s.hasSelection = agent.contains("instructions");
	if (s.hasSelection)
	{
		s.curI = NameSet(agent["instructions"]);
		s.curS = NameSet(agent.contains("skills") ? agent["skills"] : json());
		std::set<std::string> t = agent.contains("targets") ? NameSet(agent["targets"]) : std::set<std::string>{ "claude", "copilot" };
		s.curClaude = t.count("claude") > 0;
		s.curCopilot = t.count("copilot") > 0;
	}
	else
	{
		s.curI = s.defI;
		s.curS = s.defS;
		s.curClaude = s.defClaude;
		s.curCopilot = s.defCopilot;
	}
	if (!editing) { s.selI = s.curI; s.selS = s.curS; s.selClaude = s.curClaude; s.selCopilot = s.curCopilot; }
}

static std::string Join(const std::set<std::string>& names)
{
	std::string out;
	for (auto& n : names) out += (out.empty() ? "" : ",") + n;
	return out.empty() ? "none" : out;
}

static std::string Targets(bool claude, bool copilot)
{
	std::string t = std::string(claude ? "claude" : "") + (claude && copilot ? "," : "") + (copilot ? "copilot" : "");
	return t.empty() ? "none" : t;
}

static void SaveDefaults(App& app)
{
	State& s = St();
	json lib = ReadJson(s.root / "library.json").value_or(json::object());
	lib["defaults"] = { { "instructions", std::vector<std::string>(s.selI.begin(), s.selI.end()) },
	                    { "skills", std::vector<std::string>(s.selS.begin(), s.selS.end()) } };
	std::vector<std::string> t;
	if (s.selClaude) t.push_back("claude");
	if (s.selCopilot) t.push_back("copilot");
	lib["targets"] = t;
	std::error_code ec;
	fs::create_directories(s.root, ec);
	std::ofstream out(s.root / "library.json", std::ios::binary | std::ios::trunc);
	out << lib.dump(2) << "\n";
	s.defI = s.selI; s.defS = s.selS; s.defClaude = s.selClaude; s.defCopilot = s.selCopilot;
	s.curI = s.selI; s.curS = s.selS; s.curClaude = s.selClaude; s.curCopilot = s.selCopilot;
}

static std::string SizeText(uintmax_t b)
{
	return b >= 1024 * 1024 ? Fmt("%.1f MB", b / 1048576.0) : b >= 1024 ? Fmt("%.0f KB", b / 1024.0) : Fmt("%d B", (int)b);
}

static void DrawRows(App& app, const char* kind, const std::vector<Item>& items, std::set<std::string>& sel, bool idle)
{
	State& s = St();
	bool skill = std::string(kind) == "skill";
	for (auto& it : items)
	{
		ImGui::PushID((std::string(kind) + it.name).c_str());
		bool on = sel.count(it.name) > 0;
		if (ImGui::Checkbox("##on", &on))
		{
			if (on) sel.insert(it.name); else sel.erase(it.name);
			if (!app.attached) SaveDefaults(app);
		}
		ImGui::SameLine();
		ImGui::AlignTextToFramePadding();
		TextBold(it.name);
		std::string meta = skill ? Fmt("파일 %d개 · %s", it.files, SizeText(it.bytes).c_str())
		                         : (it.applyTo.empty() || it.applyTo == "**" ? std::string("모든 파일") : "적용 범위 " + it.applyTo) + " · " + SizeText(it.bytes);
		ImGui::SameLine(0, S(10));
		TextDim(meta);
		float right = ImGui::GetWindowContentRegionMax().x;
		ImGui::SameLine(right - ImGui::GetFrameHeight());
		ImGui::BeginDisabled(!idle);
		if (ToolButton(ICON_DELETE, "라이브러리에서 삭제")) { s.confirmKind = kind; s.confirmName = it.name; s.openConfirm = true; }
		ImGui::EndDisabled();
		std::string desc = !it.title.empty() && it.description.rfind(it.title, 0) != 0 ? it.title + " · " + it.description : it.description;
		if (!desc.empty())
		{
			ImGui::Indent(ImGui::GetFrameHeight() + S(8));
			ImGui::PushTextWrapPos(right - S(40));
			TextDim(desc);
			ImGui::PopTextWrapPos();
			ImGui::Unindent(ImGui::GetFrameHeight() + S(8));
		}
		ImGui::Dummy(ImVec2(0, S(2)));
		ImGui::PopID();
	}
}

static void Upload(App& app, const char* sub, const fs::path& p)
{
	app.Run(std::string("library ") + sub + " \"" + PathUtf8(p) + "\"");
	St().scannedAt = -100;
}

static void Draw(App& app)
{
	State& s = St();
	Scan(app, false);
	bool idle = !app.runner.running;
	bool connected = app.hasUProject && app.attached;

	ImGui::BeginChild("agentlib", ImVec2(0, 0), ImGuiChildFlags_None);
	ImGui::PushTextWrapPos(ImGui::GetContentRegionAvail().x);
	TextDim("Claude Code와 GitHub Copilot이 이 프로젝트에서 따를 지침서와 쓸 스킬을 고릅니다. 지침서는 에이전트가 항상 읽는 규칙, "
	        "스킬은 필요할 때 불러 쓰는 작업 절차입니다. 파일은 dwookit 폴더의 library에 올려 두고 프로젝트마다 골라 적용하며, "
	        "연결을 해제하면 프로젝트에서 모두 지워집니다(library는 남음).");
	ImGui::PopTextWrapPos();
	ImGui::Dummy(ImVec2(0, S(6)));

	// agents
	SectionHeader("적용할 에이전트");
	if (ImGui::Checkbox("Claude Code", &s.selClaude) && !connected) SaveDefaults(app);
	Tooltip("CLAUDE.local.md에 지침서를 연결하고, 스킬을 .claude/skills에 넣습니다.");
	ImGui::SameLine(0, S(24));
	if (ImGui::Checkbox("GitHub Copilot", &s.selCopilot) && !connected) SaveDefaults(app);
	Tooltip(".github/instructions에 지침서를, .github/skills에 스킬을 넣습니다 (VS Code Copilot).");
	ImGui::Dummy(ImVec2(0, S(8)));

	// instructions
	SectionHeader(Fmt("지침서 (%d)", (int)s.instructions.size()).c_str());
	ImGui::BeginDisabled(!idle);
	if (ImGui::Button(ICON_ADD "  지침서 올리기..."))
	{
		fs::path p;
		if (PickOpen(g_hwnd, false, L"올릴 지침서 (Markdown)", p, L"지침서 (*.md, *.txt)", L"*.md;*.txt;*.markdown")) Upload(app, "add-instruction", p);
	}
	ImGui::EndDisabled();
	ImGui::SameLine(0, S(10));
	ImGui::AlignTextToFramePadding();
	TextDim("Markdown 파일. 앞머리(front matter)의 applyTo가 있으면 Copilot 적용 범위로 씁니다.");
	ImGui::Dummy(ImVec2(0, S(4)));
	bool fixedOn = true;
	ImGui::BeginDisabled();
	ImGui::Checkbox("##agentmd", &fixedOn);
	ImGui::EndDisabled();
	ImGui::SameLine();
	ImGui::AlignTextToFramePadding();
	TextBold("dwookit 기본 지침 (AGENT.md)");
	ImGui::SameLine(0, S(10));
	TextDim("항상 적용 · 블루프린트 의사코드 읽는 법, 산출물 형식, 검사 명령");
	DrawRows(app, "instruction", s.instructions, s.selI, idle);
	if (s.instructions.empty()) TextDim("올린 지침서가 없습니다. 팀 코딩 규칙, 폴더·이름 규칙 같은 문서를 올리세요.");
	ImGui::Dummy(ImVec2(0, S(8)));

	// skills
	SectionHeader(Fmt("스킬 (%d)", (int)s.skills.size()).c_str());
	ImGui::BeginDisabled(!idle);
	if (ImGui::Button(ICON_ADD "  스킬 폴더 올리기..."))
	{
		fs::path p;
		if (PickOpen(g_hwnd, true, L"SKILL.md가 있는 스킬 폴더 선택", p)) Upload(app, "add-skill", p);
	}
	ImGui::SameLine();
	if (ImGui::Button("ZIP·SKILL.md 올리기..."))
	{
		fs::path p;
		if (PickOpen(g_hwnd, false, L"스킬 ZIP 또는 SKILL.md", p, L"스킬 (*.zip, SKILL.md)", L"*.zip;SKILL.md;*.md")) Upload(app, "add-skill", p);
	}
	ImGui::EndDisabled();
	ImGui::SameLine(0, S(10));
	ImGui::AlignTextToFramePadding();
	TextDim("SKILL.md와 딸린 파일. 이름·설명이 없으면 올릴 때 채웁니다.");
	ImGui::Dummy(ImVec2(0, S(4)));
	DrawRows(app, "skill", s.skills, s.selS, idle);
	if (s.skills.empty()) TextDim("올린 스킬이 없습니다. 블루프린트 리뷰, 패키지 점검처럼 반복하는 작업 절차를 스킬로 올리세요.");
	ImGui::Dummy(ImVec2(0, S(10)));
	ImGui::Separator();
	ImGui::Dummy(ImVec2(0, S(6)));

	// apply
	if (!app.hasUProject)
	{
		TextDim("고른 항목은 이 PC의 기본값으로 저장되어, 프로젝트를 연결할 때 적용됩니다.");
	}
	else if (!connected)
	{
		TextDim("이 프로젝트는 연결되지 않았습니다. 고른 항목은 기본값으로 저장되어 연결할 때 적용됩니다.");
		ImGui::BeginDisabled(!idle || app.legacyAttached);
		if (PrimaryButton("연결하고 적용"))
			app.Run("connect --instructions " + Join(s.selI) + " --skills " + Join(s.selS) + " --targets " + Targets(s.selClaude, s.selCopilot));
		ImGui::EndDisabled();
	}
	else
	{
		bool dirty = s.selI != s.curI || s.selS != s.curS || s.selClaude != s.curClaude || s.selCopilot != s.curCopilot || !s.hasSelection;
		std::string now = std::string("이 프로젝트에 적용됨: ") + (s.curClaude ? "Claude Code" : "") + (s.curClaude && s.curCopilot ? ", " : "") +
		                  (s.curCopilot ? "GitHub Copilot" : "") + (!s.curClaude && !s.curCopilot ? "에이전트 없음" : "") +
		                  Fmt(" · 지침서 %d개 · 스킬 %d개", (int)s.curI.size(), (int)s.curS.size()) + (s.hasSelection ? "" : " (기본값, 아직 기록 전)");
		TextDim(now);
		ImGui::BeginDisabled(!idle || !dirty);
		if (PrimaryButton("이 프로젝트에 적용"))
			app.Run("agent use --instructions " + Join(s.selI) + " --skills " + Join(s.selS) + " --targets " + Targets(s.selClaude, s.selCopilot));
		ImGui::SameLine();
		if (ImGui::Button("되돌리기")) { s.selI = s.curI; s.selS = s.curS; s.selClaude = s.curClaude; s.selCopilot = s.curCopilot; }
		ImGui::EndDisabled();
	}
	ImGui::EndChild();

	// delete confirmation
	if (s.openConfirm) { ImGui::OpenPopup("agentlib-delete"); s.openConfirm = false; }
	if (ImGui::BeginPopup("agentlib-delete"))
	{
		std::string what = s.confirmKind == "skill" ? "스킬" : "지침서";
		TextBold(what + " '" + s.confirmName + "' 삭제");
		ImGui::PushTextWrapPos(S(380));
		TextDim("dwookit library에서 지웁니다. 이미 적용한 프로젝트에는 선택을 바꾸거나 연결을 해제할 때까지 남습니다. 원본 파일은 그대로입니다.");
		ImGui::PopTextWrapPos();
		ImGui::Spacing();
		if (DangerButton("삭제"))
		{
			app.Run("library remove " + s.confirmKind + " " + s.confirmName);
			s.selI.erase(s.confirmName);
			s.selS.erase(s.confirmName);
			s.confirmName.clear();
			ImGui::CloseCurrentPopup();
		}
		ImGui::SameLine();
		if (ImGui::Button("취소")) { s.confirmName.clear(); ImGui::CloseCurrentPopup(); }
		ImGui::EndPopup();
	}
}
} // namespace agentlib
