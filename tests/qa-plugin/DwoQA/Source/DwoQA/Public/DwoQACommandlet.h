#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "DwoQACommandlet.generated.h"

/** dwookit QA only: -run=DwoQA -QaBuildBlueprint=<spec.json> creates a test Blueprint (DwoQABuildBlueprint.cpp). */
UCLASS()
class UDwoQACommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	UDwoQACommandlet();

	virtual int32 Main(const FString& Params) override;
};
