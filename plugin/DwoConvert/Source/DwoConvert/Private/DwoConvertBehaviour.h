#pragma once

#include "CoreMinimal.h"

namespace DwoConvert
{
	/**
	 * Runs the actor, widget or animation Blueprint named in ScenarioFile through a fixed scenario in a headless game world (fixed time
	 * step, an optional probe Character dropped onto it) and writes what it observed to OutFile (dwookit.behaviour/1).
	 * Used to compare a Blueprint before and after its native migration. Returns 0 when the run completed.
	 */
	int32 RunBehaviour(const FString& ScenarioFile, const FString& OutFile);
}
