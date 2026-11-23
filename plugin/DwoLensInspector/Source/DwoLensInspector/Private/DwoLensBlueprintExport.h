#pragma once

#include "CoreMinimal.h"

namespace DwoLensInspector
{
	/**
	 * Full export of the Blueprints a user chose to track, one folder per Blueprint:
	 *   <OutDir>/<package path without the leading slash>/<AssetName>.json   (schema lens.bp/1)
	 * plus <OutDir>/export.json, the manifest of this export (requested entries, exported, missing, failed).
	 *
	 * ListFile has one entry per line: a package path (/Game/A/BP_X), an object path (/Game/A/BP_X.BP_X) or a
	 * folder ending with '/' (/Game/A/, every Blueprint under it). Blank lines and lines starting with '#' are skipped.
	 * Returns the number of Blueprints that could not be exported (0 = all fine).
	 */
	int32 ExportTrackedBlueprints(const FString& ListFile, const FString& OutDir);
}
