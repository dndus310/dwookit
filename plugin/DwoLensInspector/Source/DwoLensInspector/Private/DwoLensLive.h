#pragma once

#include "CoreMinimal.h"
#include "Containers/Ticker.h"
#include "EditorSubsystem.h"
#include "DwoLensLive.generated.h"

class UBlueprint;
class UPackage;
class FObjectPostSaveContext;
class FJsonObject;
struct FAssetData;
class FDwoLensLogCapture;

/**
 * Live Lens: collects DwoLens raw data while the Unreal Editor is used, so analysis results accumulate without
 * closing the editor to run a commandlet. Off unless the project's __dwo-forge/live-lens.json enables it
 * (__dwo-forge.bat live on). Every editor run is a session under __dwo-forge/live/sessions/<id>/:
 *
 *   events.jsonl   what happened, appended as it happens (survives a crash): assets saved, opened, removed,
 *                  renamed, Blueprints compiled, C++ reloaded, maps opened, PIE runs (map, length, project
 *                  classes that existed), warnings and errors from the log
 *   raw/           DwoLens raw data collected at the chosen time (timing):
 *                    changes  record changed packages during the session, export only those at exit (default)
 *                    exit     full collection at exit
 *                    save     export each saved Blueprint right away (Blueprints only), the rest at exit
 *                  The first session (no cache yet) always collects in full.
 *   session.json   summary written at exit
 *
 * At exit it starts '__dwo-forge.bat live-ingest' in the background, which waits for the editor to close,
 * merges the raw data into the cache and runs the analysis (tools/lens_live.py, forge.ps1). A session that
 * ended in a crash is closed at the next editor start and its unsaved changes are carried over.
 * Heavy work never runs while the user works: only light recording, except in 'save' timing.
 */
UCLASS()
class UDwoLensLiveSubsystem : public UEditorSubsystem
{
	GENERATED_BODY()

public:
	virtual bool ShouldCreateSubsystem(UObject* Outer) const override;
	virtual void Initialize(FSubsystemCollectionBase& Collection) override;
	virtual void Deinitialize() override;

	/** __dwo-forge/live-lens.json says Live Lens is on. */
	static bool IsEnabledForProject();

private:
	struct FSettings
	{
		bool bEnabled = false;
		FString Timing = TEXT("changes");
		bool bStructure = true;
		bool bLogs = true;
		bool bPie = true;
		bool bWork = true;
	};

	void LoadSettings();
	void CloseCrashedSessions();
	void AddEvent(const FString& Type, const TSharedRef<FJsonObject>& Data);
	bool Tick(float DeltaTime);
	void Flush();

	void OnPackageSaved(const FString& Filename, UPackage* Package, FObjectPostSaveContext Context);
	void OnAssetRemoved(const FAssetData& Asset);
	void OnAssetRenamed(const FAssetData& Asset, const FString& OldObjectPath);
	void OnAssetOpened(UObject* Asset, class IAssetEditorInstance* Instance);
	void OnBlueprintPreCompile(UBlueprint* Blueprint);
	void OnPieStarted(bool bSimulating);
	void OnPieEnded(bool bSimulating);
	void OnMapOpened(const FString& Filename, bool bAsTemplate);
	void OnShutdown();

	/** Runs the DwoLens collector in this editor. OnlyPackages null = everything. */
	bool Collect(const TSet<FString>* OnlyPackages, const FString& OutDir, bool bSkipAssets);
	void StartIngest();
	bool IsProjectClass(const UClass* Class) const;

	FSettings Settings;
	FString ForgeDir;
	FString SessionDir;
	FString SessionId;
	FDateTime StartedAt;
	bool bHasCache = false;
	bool bFinished = false;
	int32 SaveExports = 0;
	TArray<FString> PendingLines;
	TSet<FString> ChangedPackages;
	TSet<FString> RemovedPackages;
	TArray<TWeakObjectPtr<UBlueprint>> Compiling;
	TSharedPtr<FDwoLensLogCapture> LogCapture;
	FTSTicker::FDelegateHandle TickHandle;
	double PieStartSeconds = 0.0;
	int32 PieRuns = 0;
	mutable TMap<FString, bool> ProjectModuleCache;
};
