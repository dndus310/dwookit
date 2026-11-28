#include "DwoQACommandlet.h"
#include "DwoQABuildBlueprint.h"

DEFINE_LOG_CATEGORY_STATIC(LogDwoQA, Log, All);

UDwoQACommandlet::UDwoQACommandlet()
{
	IsClient = false;
	IsServer = false;
	IsEditor = true;
	LogToConsole = true;
	ShowErrorCount = true;
}

int32 UDwoQACommandlet::Main(const FString& Params)
{
	FString Spec;
	if (FParse::Value(*Params, TEXT("QaBuildBlueprint="), Spec) && !Spec.IsEmpty())
	{
		return DwoQA::BuildBlueprint(Spec);
	}
	UE_LOG(LogDwoQA, Error, TEXT("Usage: -run=DwoQA -QaBuildBlueprint=<spec.json>"));
	return 1;
}
