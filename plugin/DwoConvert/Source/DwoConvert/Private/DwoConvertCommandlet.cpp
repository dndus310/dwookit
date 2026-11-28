#include "DwoConvertCommandlet.h"
#include "DwoConvertBehaviour.h"
#include "DwoConvertRebase.h"

#include "Misc/Paths.h"

DEFINE_LOG_CATEGORY_STATIC(LogDwoConvertCommandlet, Log, All);

UDwoConvertCommandlet::UDwoConvertCommandlet()
{
	IsClient = false;
	IsServer = false;
	IsEditor = true;
	LogToConsole = true;
	ShowErrorCount = true;
}

int32 UDwoConvertCommandlet::Main(const FString& Params)
{
	// Behaviour check: -ConvertBehave=<scenario.json> -ConvertBehaveOut=<trace.json>
	FString Scenario;
	if (FParse::Value(*Params, TEXT("ConvertBehave="), Scenario) && !Scenario.IsEmpty())
	{
		FString Out;
		if (!FParse::Value(*Params, TEXT("ConvertBehaveOut="), Out) || Out.IsEmpty())
		{
			Out = FPaths::ChangeExtension(Scenario, TEXT("trace.json"));
		}
		return DwoConvert::RunBehaviour(Scenario, Out);
	}

	// Rebase: -ConvertRebase=<Name.native.json> -ConvertRebaseReport=<file> [-ConvertDryRun] [-ConvertForce]
	FString Map;
	if (FParse::Value(*Params, TEXT("ConvertRebase="), Map) && !Map.IsEmpty())
	{
		FString Report;
		if (!FParse::Value(*Params, TEXT("ConvertRebaseReport="), Report) || Report.IsEmpty())
		{
			Report = FPaths::ChangeExtension(Map, TEXT("rebase.json"));
		}
		return DwoConvert::RebaseBlueprint(Map, Report, FParse::Param(*Params, TEXT("ConvertDryRun")), FParse::Param(*Params, TEXT("ConvertForce")));
	}

	UE_LOG(LogDwoConvertCommandlet, Error, TEXT("Nothing to do: pass -ConvertRebase=<Name.native.json> or -ConvertBehave=<scenario.json>"));
	return 1;
}
