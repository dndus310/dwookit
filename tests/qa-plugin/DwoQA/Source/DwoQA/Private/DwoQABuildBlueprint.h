#pragma once

#include "CoreMinimal.h"

namespace DwoQA
{
	/**
	 * Test fixtures for the native migration: creates a Blueprint from a JSON description of its variables and
	 * event graph (nodes, pin defaults, links), compiles and saves it. Used by dwookit's own QA to get real Blueprints
	 * with node kinds a sample project does not have. Fails if the asset exists. Returns 0 when it compiled cleanly.
	 */
	int32 BuildBlueprint(const FString& SpecFile);
}
