#pragma once

#include "CoreMinimal.h"

namespace DwoConvert
{
	/**
	 * Rebases the Blueprint named in MappingFile (<Name>.native.json from tools/convert_native.py) onto its generated C++
	 * class: moves the migrated members out of the Blueprint, retargets the nodes that used them, reparents, restores
	 * every class default and component value, compiles and compares against the snapshot taken first. Saves only
	 * when nothing differs (or bForce) and not bDryRun. Writes ReportFile (dwookit.rebase/1). Returns 0 on success.
	 */
	int32 RebaseBlueprint(const FString& MappingFile, const FString& ReportFile, bool bDryRun, bool bForce);
}
