#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "DwoConvertCommandlet.generated.h"

/**
 * DwoConvert (Blueprint to C++ migration), the parts that need the engine:
 *   -ConvertRebase=<Name.native.json> [-ConvertRebaseReport=<file>] [-ConvertDryRun] [-ConvertForce]
 *       rebase the Blueprint onto its generated C++ class (DwoConvertRebase.cpp); saves the asset when it verifies
 *   -ConvertBehave=<scenario.json> [-ConvertBehaveOut=<trace.json>]
 *       run the Blueprint through a fixed scenario and record what it does (DwoConvertBehaviour.cpp)
 * The network check runs in the editor as the automation test DwoConvert.NetCheck (a commandlet has no PIE).
 */
UCLASS()
class UDwoConvertCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	UDwoConvertCommandlet();

	virtual int32 Main(const FString& Params) override;
};
