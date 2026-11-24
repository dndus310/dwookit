#include "DwoLensLive.h"

#include "DwoLensInspectorCommandlet.h"

#include "AssetRegistry/AssetData.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "Engine/Blueprint.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "GameFramework/Actor.h"
#include "HAL/FileManager.h"
#include "HAL/PlatformOutputDevices.h"
#include "HAL/PlatformProcess.h"
#include "Misc/App.h"
#include "Misc/CoreDelegates.h"
#include "Misc/DateTime.h"
#include "Misc/FileHelper.h"
#include "Misc/OutputDevice.h"
#include "Misc/OutputDeviceRedirector.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Subsystems/AssetEditorSubsystem.h"
#include "UObject/ObjectSaveContext.h"
#include "UObject/Package.h"
#include "UObject/UObjectGlobals.h"

DEFINE_LOG_CATEGORY_STATIC(LogDwoLensLive, Log, All);

namespace DwoLensLive
{
	FString ForgeDir() { return FPaths::ConvertRelativePathToFull(FPaths::Combine(FPaths::ProjectDir(), TEXT("__dwookit"))); }

	TSharedPtr<FJsonObject> ReadJson(const FString& Path)
	{
		FString Text;
		if (!FFileHelper::LoadFileToString(Text, *Path))
		{
			return nullptr;
		}
		TSharedPtr<FJsonObject> Object;
		TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Text);
		return FJsonSerializer::Deserialize(Reader, Object) ? Object : nullptr;
	}

	FString ToLine(const TSharedRef<FJsonObject>& Object)
	{
		FString Line;
		TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer = TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Line);
		FJsonSerializer::Serialize(Object, Writer);
		return Line;
	}

	void WriteJson(const FString& Path, const TSharedRef<FJsonObject>& Object)
	{
		FString Text;
		TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Text);
		FJsonSerializer::Serialize(Object, Writer);
		FFileHelper::SaveStringToFile(Text, *Path, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
	}

	bool IsContentPackage(const FString& Package)
	{
		return Package.StartsWith(TEXT("/")) && !Package.StartsWith(TEXT("/Script/")) && !Package.StartsWith(TEXT("/Engine/")) &&
		       !Package.StartsWith(TEXT("/Temp/")) && !Package.Contains(TEXT("/__ExternalActors__/")) &&
		       !Package.Contains(TEXT("/__ExternalObjects__/"));
	}
} // namespace DwoLensLive

/** Warnings and errors from the editor log, kept for the session (thread safe, bounded). */
class FDwoLensLogCapture : public FOutputDevice
{
public:
	virtual bool CanBeUsedOnAnyThread() const override { return true; }
	virtual bool CanBeUsedOnMultipleThreads() const override { return true; }

	virtual void Serialize(const TCHAR* Message, ELogVerbosity::Type Verbosity, const FName& Category) override
	{
		const ELogVerbosity::Type Level = (ELogVerbosity::Type)(Verbosity & ELogVerbosity::VerbosityMask);
		if (Level > ELogVerbosity::Warning || Category == TEXT("LogDwoLensLive"))
		{
			return;
		}
		FScopeLock Lock(&Mutex);
		(Level <= ELogVerbosity::Error ? Errors : Warnings)++;
		if (Lines.Num() < 400)
		{
			FString Text(Message);
			Lines.Add(FString::Printf(TEXT("%s|%s|%s"), Level <= ELogVerbosity::Error ? TEXT("error") : TEXT("warning"),
			                          *Category.ToString(), *Text.Left(400)));
		}
	}

	void Take(TArray<FString>& Out, int32& OutErrors, int32& OutWarnings)
	{
		FScopeLock Lock(&Mutex);
		Out = MoveTemp(Lines);
		Lines.Reset();
		OutErrors = Errors;
		OutWarnings = Warnings;
	}

private:
	FCriticalSection Mutex;
	TArray<FString> Lines;
	int32 Errors = 0;
	int32 Warnings = 0;
};

bool UDwoLensLiveSubsystem::IsEnabledForProject()
{
	const TSharedPtr<FJsonObject> Config = DwoLensLive::ReadJson(FPaths::Combine(DwoLensLive::ForgeDir(), TEXT("live-lens.json")));
	bool bEnabled = false;
	return Config.IsValid() && Config->TryGetBoolField(TEXT("enabled"), bEnabled) && bEnabled;
}

bool UDwoLensLiveSubsystem::ShouldCreateSubsystem(UObject* Outer) const
{
	return GIsEditor && !IsRunningCommandlet() && !FApp::IsUnattended() && IsEnabledForProject();
}

void UDwoLensLiveSubsystem::LoadSettings()
{
	const TSharedPtr<FJsonObject> Config = DwoLensLive::ReadJson(FPaths::Combine(ForgeDir, TEXT("live-lens.json")));
	if (!Config.IsValid())
	{
		return;
	}
	Config->TryGetBoolField(TEXT("enabled"), Settings.bEnabled);
	Config->TryGetStringField(TEXT("timing"), Settings.Timing);
	const TSharedPtr<FJsonObject>* Collect = nullptr;
	if (Config->TryGetObjectField(TEXT("collect"), Collect) && Collect)
	{
		(*Collect)->TryGetBoolField(TEXT("structure"), Settings.bStructure);
		(*Collect)->TryGetBoolField(TEXT("logs"), Settings.bLogs);
		(*Collect)->TryGetBoolField(TEXT("pie"), Settings.bPie);
		(*Collect)->TryGetBoolField(TEXT("work"), Settings.bWork);
	}
}

void UDwoLensLiveSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
	Super::Initialize(Collection);
	Collection.InitializeDependency<UAssetEditorSubsystem>();
	ForgeDir = DwoLensLive::ForgeDir();
	LoadSettings();
	StartedAt = FDateTime::UtcNow();
	SessionId = StartedAt.ToString(TEXT("%Y%m%d-%H%M%S"));
	SessionDir = FPaths::Combine(ForgeDir, TEXT("live"), TEXT("sessions"), SessionId);
	IFileManager::Get().MakeDirectory(*SessionDir, true);
	bHasCache = IFileManager::Get().FileExists(*FPaths::Combine(ForgeDir, TEXT("live"), TEXT("base"), TEXT("meta.json")));

	CloseCrashedSessions();

	TSharedRef<FJsonObject> Start = MakeShared<FJsonObject>();
	Start->SetStringField(TEXT("timing"), Settings.Timing);
	Start->SetStringField(TEXT("engine"), FEngineVersion::Current().ToString());
	Start->SetNumberField(TEXT("pid"), FPlatformProcess::GetCurrentProcessId());
	Start->SetStringField(TEXT("log"), FPaths::ConvertRelativePathToFull(FPlatformOutputDevices::GetAbsoluteLogFilename()));
	AddEvent(TEXT("session_start"), Start);

	if (Settings.bStructure || Settings.bWork)
	{
		UPackage::PackageSavedWithContextEvent.AddUObject(this, &UDwoLensLiveSubsystem::OnPackageSaved);
		IAssetRegistry& Registry = FModuleManager::LoadModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry")).Get();
		Registry.OnAssetRemoved().AddUObject(this, &UDwoLensLiveSubsystem::OnAssetRemoved);
		Registry.OnAssetRenamed().AddUObject(this, &UDwoLensLiveSubsystem::OnAssetRenamed);
		if (GEditor)
		{
			GEditor->OnBlueprintPreCompile().AddUObject(this, &UDwoLensLiveSubsystem::OnBlueprintPreCompile);
		}
	}
	if (Settings.bWork && GEditor)
	{
		if (UAssetEditorSubsystem* Editors = GEditor->GetEditorSubsystem<UAssetEditorSubsystem>())
		{
			Editors->OnAssetOpenedInEditor().AddUObject(this, &UDwoLensLiveSubsystem::OnAssetOpened);
		}
		FEditorDelegates::OnMapOpened.AddUObject(this, &UDwoLensLiveSubsystem::OnMapOpened);
	}
	if (Settings.bPie)
	{
		FEditorDelegates::PostPIEStarted.AddUObject(this, &UDwoLensLiveSubsystem::OnPieStarted);
		FEditorDelegates::EndPIE.AddUObject(this, &UDwoLensLiveSubsystem::OnPieEnded);
	}
	if (Settings.bLogs)
	{
		LogCapture = MakeShared<FDwoLensLogCapture>();
		GLog->AddOutputDevice(LogCapture.Get());
	}
	FEditorDelegates::OnShutdownPostPackagesSaved.AddUObject(this, &UDwoLensLiveSubsystem::OnShutdown);
	TickHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateUObject(this, &UDwoLensLiveSubsystem::Tick), 5.0f);
	UE_LOG(LogDwoLensLive, Display, TEXT("Live Lens: session %s (%s)%s"), *SessionId, *Settings.Timing,
	       bHasCache ? TEXT("") : TEXT(", first session: full collection at exit"));
}

void UDwoLensLiveSubsystem::Deinitialize()
{
	OnShutdown();          // no-op when the shutdown event already finished the session
	FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
	UPackage::PackageSavedWithContextEvent.RemoveAll(this);
	FEditorDelegates::OnMapOpened.RemoveAll(this);
	FEditorDelegates::PostPIEStarted.RemoveAll(this);
	FEditorDelegates::EndPIE.RemoveAll(this);
	FEditorDelegates::OnShutdownPostPackagesSaved.RemoveAll(this);
	if (FModuleManager::Get().IsModuleLoaded(TEXT("AssetRegistry")))
	{
		IAssetRegistry& Registry = FModuleManager::GetModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry")).Get();
		Registry.OnAssetRemoved().RemoveAll(this);
		Registry.OnAssetRenamed().RemoveAll(this);
	}
	if (GEditor)
	{
		GEditor->OnBlueprintPreCompile().RemoveAll(this);
	}
	if (LogCapture.IsValid() && GLog)
	{
		GLog->RemoveOutputDevice(LogCapture.Get());
	}
	LogCapture.Reset();
	Super::Deinitialize();
}

void UDwoLensLiveSubsystem::AddEvent(const FString& Type, const TSharedRef<FJsonObject>& Data)
{
	Data->SetStringField(TEXT("type"), Type);
	Data->SetStringField(TEXT("t"), FDateTime::UtcNow().ToIso8601());
	PendingLines.Add(DwoLensLive::ToLine(Data));
}

bool UDwoLensLiveSubsystem::Tick(float DeltaTime)
{
	Flush();
	return true;
}

void UDwoLensLiveSubsystem::Flush()
{
	// Blueprints compiled since the last flush: their result is known now.
	for (const TWeakObjectPtr<UBlueprint>& Weak : Compiling)
	{
		if (const UBlueprint* Blueprint = Weak.Get())
		{
			TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
			Event->SetStringField(TEXT("package"), Blueprint->GetOutermost()->GetName());
			Event->SetStringField(TEXT("status"), Blueprint->Status == BS_Error ? TEXT("error")
			                                      : Blueprint->Status == BS_UpToDateWithWarnings ? TEXT("warning") : TEXT("ok"));
			AddEvent(TEXT("bp_compiled"), Event);
		}
	}
	Compiling.Reset();
	if (LogCapture.IsValid())
	{
		TArray<FString> Lines;
		int32 Errors = 0;
		int32 Warnings = 0;
		LogCapture->Take(Lines, Errors, Warnings);
		for (const FString& Line : Lines)
		{
			FString Level, Rest, Category, Message;
			Line.Split(TEXT("|"), &Level, &Rest);
			Rest.Split(TEXT("|"), &Category, &Message);
			TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
			Event->SetStringField(TEXT("level"), Level);
			Event->SetStringField(TEXT("category"), Category);
			Event->SetStringField(TEXT("message"), Message);
			AddEvent(TEXT("log"), Event);
		}
	}
	if (PendingLines.Num() == 0)
	{
		return;
	}
	const FString Text = FString::Join(PendingLines, TEXT("\n")) + TEXT("\n");
	PendingLines.Reset();
	FFileHelper::SaveStringToFile(Text, *FPaths::Combine(SessionDir, TEXT("events.jsonl")),
	                              FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM, &IFileManager::Get(), FILEWRITE_Append);
}

void UDwoLensLiveSubsystem::CloseCrashedSessions()
{
	// A session folder with events but no session.json ended without the shutdown event (crash, killed).
	TArray<FString> Dirs;
	IFileManager::Get().FindFiles(Dirs, *FPaths::Combine(ForgeDir, TEXT("live"), TEXT("sessions"), TEXT("*")), false, true);
	for (const FString& Name : Dirs)
	{
		const FString Dir = FPaths::Combine(ForgeDir, TEXT("live"), TEXT("sessions"), Name);
		if (Name == SessionId || IFileManager::Get().FileExists(*FPaths::Combine(Dir, TEXT("session.json"))) ||
		    !IFileManager::Get().FileExists(*FPaths::Combine(Dir, TEXT("events.jsonl"))))
		{
			continue;
		}
		TArray<FString> Lines;
		FFileHelper::LoadFileToStringArray(Lines, *FPaths::Combine(Dir, TEXT("events.jsonl")));
		for (const FString& Line : Lines)
		{
			TSharedPtr<FJsonObject> Event;
			TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Line);
			FString Type, Package;
			if (FJsonSerializer::Deserialize(Reader, Event) && Event.IsValid() && Event->TryGetStringField(TEXT("type"), Type) &&
			    Type == TEXT("asset_saved") && Event->TryGetStringField(TEXT("package"), Package))
			{
				ChangedPackages.Add(Package);     // carried over: collected at this session's exit
			}
		}
		TSharedRef<FJsonObject> Summary = MakeShared<FJsonObject>();
		Summary->SetStringField(TEXT("id"), Name);
		Summary->SetStringField(TEXT("ended"), TEXT("crashed"));
		Summary->SetStringField(TEXT("closedBy"), SessionId);
		DwoLensLive::WriteJson(FPaths::Combine(Dir, TEXT("session.json")), Summary);
		TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
		Event->SetStringField(TEXT("session"), Name);
		Event->SetNumberField(TEXT("carriedOver"), ChangedPackages.Num());
		AddEvent(TEXT("previous_session_crashed"), Event);
	}
}

void UDwoLensLiveSubsystem::OnPackageSaved(const FString& Filename, UPackage* Package, FObjectPostSaveContext Context)
{
	if (!Package || Context.IsProceduralSave() || bFinished)
	{
		return;
	}
	const FString Name = Package->GetName();
	if (!DwoLensLive::IsContentPackage(Name))
	{
		return;
	}
	ChangedPackages.Add(Name);
	RemovedPackages.Remove(Name);
	const UBlueprint* Blueprint = nullptr;
	ForEachObjectWithPackage(Package, [&Blueprint](UObject* Object)
	{
		Blueprint = Cast<UBlueprint>(Object);
		return Blueprint == nullptr;
	}, false);
	TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
	Event->SetStringField(TEXT("package"), Name);
	Event->SetBoolField(TEXT("blueprint"), Blueprint != nullptr);
	AddEvent(TEXT("asset_saved"), Event);

	// 'save' timing: the saved Blueprint right away (only it is loaded; the asset list waits for exit)
	if (Settings.bStructure && Settings.Timing == TEXT("save") && Blueprint && bHasCache)
	{
		TSet<FString> One = { Name };
		Collect(&One, FPaths::Combine(SessionDir, TEXT("saves"), FString::Printf(TEXT("%04d"), ++SaveExports)), true);
	}
}

void UDwoLensLiveSubsystem::OnAssetRemoved(const FAssetData& Asset)
{
	const FString Name = Asset.PackageName.ToString();
	if (bFinished || !DwoLensLive::IsContentPackage(Name))
	{
		return;
	}
	RemovedPackages.Add(Name);
	ChangedPackages.Remove(Name);
	TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
	Event->SetStringField(TEXT("package"), Name);
	AddEvent(TEXT("asset_removed"), Event);
}

void UDwoLensLiveSubsystem::OnAssetRenamed(const FAssetData& Asset, const FString& OldObjectPath)
{
	const FString Name = Asset.PackageName.ToString();
	if (bFinished || !DwoLensLive::IsContentPackage(Name))
	{
		return;
	}
	FString OldPackage = OldObjectPath;
	OldPackage.Split(TEXT("."), &OldPackage, nullptr);
	RemovedPackages.Add(OldPackage);
	ChangedPackages.Add(Name);
	TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
	Event->SetStringField(TEXT("package"), Name);
	Event->SetStringField(TEXT("from"), OldPackage);
	AddEvent(TEXT("asset_renamed"), Event);
}

void UDwoLensLiveSubsystem::OnAssetOpened(UObject* Asset, IAssetEditorInstance* Instance)
{
	if (!Asset || bFinished)
	{
		return;
	}
	TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
	Event->SetStringField(TEXT("package"), Asset->GetOutermost()->GetName());
	Event->SetStringField(TEXT("class"), Asset->GetClass()->GetName());
	AddEvent(TEXT("asset_opened"), Event);
}

void UDwoLensLiveSubsystem::OnBlueprintPreCompile(UBlueprint* Blueprint)
{
	if (Blueprint && !bFinished && DwoLensLive::IsContentPackage(Blueprint->GetOutermost()->GetName()))
	{
		Compiling.AddUnique(Blueprint);
	}
}

void UDwoLensLiveSubsystem::OnMapOpened(const FString& Filename, bool bAsTemplate)
{
	TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
	FString Package;
	Event->SetStringField(TEXT("map"), FPackageName::TryConvertFilenameToLongPackageName(Filename, Package) ? Package : Filename);
	AddEvent(TEXT("map_opened"), Event);
}

bool UDwoLensLiveSubsystem::IsProjectClass(const UClass* Class) const
{
	if (!Class)
	{
		return false;
	}
	const FString Package = Class->GetOutermost()->GetName();
	if (!Package.StartsWith(TEXT("/Script/")))
	{
		return DwoLensLive::IsContentPackage(Package);           // Blueprint classes of the project and its plugins
	}
	const FString Module = Package.RightChop(8);
	if (const bool* Known = ProjectModuleCache.Find(Module))
	{
		return *Known;
	}
	// a C++ module is the project's when its source lives under the project folder
	const FString ProjectDir = FPaths::ConvertRelativePathToFull(FPaths::ProjectDir());
	bool bProject = IFileManager::Get().DirectoryExists(*FPaths::Combine(ProjectDir, TEXT("Source"), Module));
	if (!bProject)
	{
		TArray<FString> Found;
		IFileManager::Get().FindFilesRecursive(Found, *FPaths::Combine(ProjectDir, TEXT("Plugins")), *Module, false, true, false);
		bProject = Found.ContainsByPredicate([](const FString& Dir) { return Dir.Contains(TEXT("/Source/")) || Dir.Contains(TEXT("\\Source\\")); });
	}
	ProjectModuleCache.Add(Module, bProject);
	return bProject;
}

void UDwoLensLiveSubsystem::OnPieStarted(bool bSimulating)
{
	PieStartSeconds = FPlatformTime::Seconds();
	TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
	Event->SetBoolField(TEXT("simulate"), bSimulating);
	if (GEditor && GEditor->PlayWorld)
	{
		Event->SetStringField(TEXT("map"), GEditor->PlayWorld->GetOutermost()->GetName());
	}
	AddEvent(TEXT("pie_start"), Event);
}

void UDwoLensLiveSubsystem::OnPieEnded(bool bSimulating)
{
	++PieRuns;
	TSharedRef<FJsonObject> Event = MakeShared<FJsonObject>();
	Event->SetNumberField(TEXT("seconds"), PieStartSeconds > 0 ? FPlatformTime::Seconds() - PieStartSeconds : 0.0);
	// Which project classes were really in play (actors and their components), for the game flow.
	TMap<FString, int32> Counts;
	if (GEditor && GEditor->PlayWorld)
	{
		Event->SetStringField(TEXT("map"), GEditor->PlayWorld->GetOutermost()->GetName());
		for (TActorIterator<AActor> It(GEditor->PlayWorld); It; ++It)
		{
			const AActor* Actor = *It;
			if (!IsValid(Actor))
			{
				continue;
			}
			if (IsProjectClass(Actor->GetClass()))
			{
				Counts.FindOrAdd(Actor->GetClass()->GetPathName())++;
			}
			for (const UActorComponent* Component : Actor->GetComponents())
			{
				if (Component && IsProjectClass(Component->GetClass()))
				{
					Counts.FindOrAdd(Component->GetClass()->GetPathName())++;
				}
			}
		}
	}
	TSharedRef<FJsonObject> Classes = MakeShared<FJsonObject>();
	for (const TPair<FString, int32>& Pair : Counts)
	{
		Classes->SetNumberField(Pair.Key, Pair.Value);
	}
	Event->SetObjectField(TEXT("classes"), Classes);
	AddEvent(TEXT("pie_end"), Event);
	Flush();
}

bool UDwoLensLiveSubsystem::Collect(const TSet<FString>* OnlyPackages, const FString& OutDir, bool bSkipAssets)
{
	IFileManager::Get().MakeDirectory(*OutDir, true);
	FString Args = FString::Printf(TEXT("-LensOut=\"%s\""), *OutDir);
	if (OnlyPackages)
	{
		const FString ListFile = FPaths::Combine(OutDir, TEXT("packages.txt"));
		FFileHelper::SaveStringToFile(FString::Join(OnlyPackages->Array(), TEXT("\n")), *ListFile, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
		Args += FString::Printf(TEXT(" -LensPackages=\"%s\""), *ListFile);
	}
	if (bSkipAssets)
	{
		Args += TEXT(" -LensSkipAssets");
	}
	// Tracked Blueprints (bp add) are refreshed with every collection that is not a single-save export.
	const FString Track = FPaths::Combine(ForgeDir, TEXT("bp-track.txt"));
	if (!bSkipAssets && IFileManager::Get().FileExists(*Track))
	{
		Args += FString::Printf(TEXT(" -LensBpList=\"%s\" -LensBpOut=\"%s\""), *Track, *FPaths::Combine(ForgeDir, TEXT("blueprints")));
	}
	const double Started = FPlatformTime::Seconds();
	UDwoLensInspectorCommandlet* Collector = NewObject<UDwoLensInspectorCommandlet>();
	const int32 Code = Collector->Main(Args);
	UE_LOG(LogDwoLensLive, Display, TEXT("Live Lens: collected %s in %.1fs (exit %d)"),
	       OnlyPackages ? *FString::Printf(TEXT("%d package(s)"), OnlyPackages->Num()) : TEXT("everything"), FPlatformTime::Seconds() - Started, Code);
	return Code == 0;
}

void UDwoLensLiveSubsystem::OnShutdown()
{
	if (bFinished)
	{
		return;
	}
	bFinished = true;
	Flush();
	const double Started = FPlatformTime::Seconds();
	FString Collected = TEXT("none");
	if (Settings.bStructure)
	{
		const FString Raw = FPaths::Combine(SessionDir, TEXT("raw"));
		if (!bHasCache || Settings.Timing == TEXT("exit"))
		{
			Collected = Collect(nullptr, Raw, false) ? TEXT("full") : TEXT("failed");
		}
		else if (ChangedPackages.Num() > 0 || RemovedPackages.Num() > 0 || SaveExports > 0)
		{
			// 'save' timing already exported the saved Blueprints; this refreshes the asset list and reflection.
			TSet<FString> Packages = Settings.Timing == TEXT("save") ? TSet<FString>() : ChangedPackages;
			Collected = Collect(&Packages, Raw, false) ? TEXT("partial") : TEXT("failed");
		}
	}
	int32 Errors = 0;
	int32 Warnings = 0;
	if (LogCapture.IsValid())
	{
		TArray<FString> Ignored;
		LogCapture->Take(Ignored, Errors, Warnings);
	}
	TSharedRef<FJsonObject> Summary = MakeShared<FJsonObject>();
	Summary->SetStringField(TEXT("id"), SessionId);
	Summary->SetStringField(TEXT("ended"), TEXT("normal"));
	Summary->SetStringField(TEXT("startedUtc"), StartedAt.ToIso8601());
	Summary->SetStringField(TEXT("endedUtc"), FDateTime::UtcNow().ToIso8601());
	Summary->SetStringField(TEXT("timing"), Settings.Timing);
	Summary->SetStringField(TEXT("collected"), Collected);
	Summary->SetNumberField(TEXT("exitSeconds"), FPlatformTime::Seconds() - Started);
	Summary->SetNumberField(TEXT("pieRuns"), PieRuns);
	Summary->SetNumberField(TEXT("logErrors"), Errors);
	Summary->SetNumberField(TEXT("logWarnings"), Warnings);
	Summary->SetNumberField(TEXT("pid"), FPlatformProcess::GetCurrentProcessId());
	TArray<TSharedPtr<FJsonValue>> Changed, Removed;
	for (const FString& Package : ChangedPackages) { Changed.Add(MakeShared<FJsonValueString>(Package)); }
	for (const FString& Package : RemovedPackages) { Removed.Add(MakeShared<FJsonValueString>(Package)); }
	Summary->SetArrayField(TEXT("changed"), Changed);
	Summary->SetArrayField(TEXT("removed"), Removed);
	Summary->SetStringField(TEXT("log"), FPaths::ConvertRelativePathToFull(FPlatformOutputDevices::GetAbsoluteLogFilename()));
	DwoLensLive::WriteJson(FPaths::Combine(SessionDir, TEXT("session.json")), Summary);
	StartIngest();
}

void UDwoLensLiveSubsystem::StartIngest()
{
	// Processing happens out of process after the editor closed (it waits for this process id).
	const FString Launcher = FPaths::ConvertRelativePathToFull(FPaths::Combine(FPaths::ProjectDir(), TEXT("__dwookit.bat")));
	if (!IFileManager::Get().FileExists(*Launcher))
	{
		UE_LOG(LogDwoLensLive, Warning, TEXT("Live Lens: %s not found; run __dwookit.bat live-ingest later"), *Launcher);
		return;
	}
	const FString Args = FString::Printf(TEXT("/c \"\"%s\" live-ingest --wait-pid %u\""), *Launcher, FPlatformProcess::GetCurrentProcessId());
	FProcHandle Handle = FPlatformProcess::CreateProc(TEXT("cmd.exe"), *Args, true, true, true, nullptr, 0, nullptr, nullptr);
	UE_LOG(LogDwoLensLive, Display, TEXT("Live Lens: background processing %s"), Handle.IsValid() ? TEXT("started") : TEXT("could not start"));
}
