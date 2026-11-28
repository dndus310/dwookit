using UnrealBuildTool;

public class DwoConvert : ModuleRules
{
	public DwoConvert(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"UnrealEd",
			"BlueprintGraph",
			"Kismet",
			"AssetRegistry",
			"Json",
			"UMG",
			"UMGEditor",
			"Slate",
			"SlateCore",
			"AIModule",
			"GameplayTasks"
		});
	}
}
