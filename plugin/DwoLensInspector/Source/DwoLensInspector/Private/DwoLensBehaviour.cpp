// Native migration, behaviour check: runs one actor, widget or animation Blueprint through a fixed scenario in a headless game world and
// records what it does, so the same scenario can be run on the Blueprint before and after the migration and the two
// traces compared (tools/lens_native.py compare).
//
//   -LensBehave=<scenario.json> -LensBehaveOut=<trace.json>
//
// scenario: {"blueprint": "/Game/A/BP_X.BP_X", "frames": 90, "dt": 0.0333, "probe": "character" | "none",
//            "probeOffset": [x, y, z], "probeFrame": 30, "recordEvery": 10}
// The world has no game mode; begin play is dispatched through the world settings. Every frame uses the same fixed
// step, so two runs see the same inputs. Recorded at spawn, at every recordEvery frames and at the end:
//   subject    each editable or Blueprint-visible property of the actor (text), and per component the same plus its
//              world transform
//   probe      a Character dropped onto the actor (overlaps, casts to Character, launches): location, velocity,
//              movement mode, pending launch velocity
// A widget Blueprint is created for the game instance, constructed (TakeWidget) and ticked through its Slate widget;
// recorded: its properties and those of every widget in its tree. An animation Blueprint runs on a Character's mesh
// (a skeletal mesh of its target skeleton), launched at the probe frame; recorded: the anim instance's properties,
// the character's movement and a sample of the pose.
#include "DwoLensBehaviour.h"

#include "Animation/AnimBlueprint.h"
#include "Animation/AnimInstance.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "Blueprint/UserWidget.h"
#include "Blueprint/WidgetTree.h"
#include "Components/ActorComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Components/Widget.h"
#include "Engine/SkeletalMesh.h"
#include "Framework/Application/SlateApplication.h"
#include "Layout/Geometry.h"
#include "Widgets/SWidget.h"
#include "Components/SceneComponent.h"
#include "Dom/JsonObject.h"
#include "Engine/Blueprint.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "Engine/Level.h"
#include "GameFramework/Character.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "GameFramework/WorldSettings.h"
#include "GameFramework/GameModeBase.h"
#include "GameFramework/RotatingMovementComponent.h"
#include "Misc/FileHelper.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Tests/AutomationCommon.h"
#include "UObject/UnrealType.h"

DEFINE_LOG_CATEGORY_STATIC(LogDwoBehaviour, Log, All);

namespace DwoLensInspector::Behaviour
{
	using FJson = TSharedPtr<FJsonObject>;

	static bool Observable(const FProperty* Property)
	{
		return Property->HasAnyPropertyFlags(CPF_Edit | CPF_BlueprintVisible) && !Property->HasAnyPropertyFlags(CPF_Deprecated);
	}

	static FJson Properties(const UObject* Object)
	{
		FJson Out = MakeShared<FJsonObject>();
		for (TFieldIterator<FProperty> It(Object->GetClass()); It; ++It)
		{
			const FProperty* Property = *It;
			if (!Observable(Property)) { continue; }
			// pointers to the object's own subobjects (components) differ by instance, not by behaviour
			if (const FObjectPropertyBase* ObjectProperty = CastField<FObjectPropertyBase>(Property))
			{
				const UObject* Target = ObjectProperty->GetObjectPropertyValue_InContainer(Object);
				if (Target && Target->IsIn(Object)) { continue; }
			}
			FString Text;
			Property->ExportTextItem_InContainer(Text, Object, nullptr, const_cast<UObject*>(Object), PPF_None);
			Out->SetStringField(Property->GetName(), Text.Left(2000));
		}
		return Out;
	}

	static FJson DescribeActor(const AActor* Actor)
	{
		FJson Out = MakeShared<FJsonObject>();
		Out->SetObjectField(TEXT("props"), Properties(Actor));
		Out->SetStringField(TEXT("transform"), Actor->GetActorTransform().ToString());
		FJson Components = MakeShared<FJsonObject>();
		TInlineComponentArray<UActorComponent*> List(Actor);
		for (const UActorComponent* Component : List)
		{
			FJson C = Properties(Component);
			if (const USceneComponent* Scene = Cast<USceneComponent>(Component))
			{
				C->SetStringField(TEXT("@world"), Scene->GetComponentTransform().ToString());
				C->SetBoolField(TEXT("@visible"), Scene->IsVisible());
			}
			C->SetBoolField(TEXT("@active"), Component->IsActive());
			Components->SetObjectField(Component->GetName(), C);
		}
		Out->SetObjectField(TEXT("components"), Components);
		return Out;
	}

	static FJson DescribeWidget(const UUserWidget* Widget)
	{
		FJson Out = MakeShared<FJsonObject>();
		Out->SetObjectField(TEXT("props"), Properties(Widget));
		FJson Children = MakeShared<FJsonObject>();
		if (Widget->WidgetTree)
		{
			Widget->WidgetTree->ForEachWidget([&Children](UWidget* Child)
			{
				if (!Child) { return; }
				FJson C = Properties(Child);
				C->SetStringField(TEXT("@visibility"), StaticEnum<ESlateVisibility>()->GetNameStringByValue(static_cast<int64>(Child->GetVisibility())));
				Children->SetObjectField(Child->GetName(), C);
			});
		}
		Out->SetObjectField(TEXT("components"), Children);
		return Out;
	}

	static FJson DescribeAnim(const ACharacter* Owner)
	{
		FJson Out = MakeShared<FJsonObject>();
		const USkeletalMeshComponent* Mesh = Owner->GetMesh();
		Out->SetObjectField(TEXT("props"), Properties(Mesh->GetAnimInstance()));
		Out->SetStringField(TEXT("transform"), Owner->GetActorTransform().ToString());
		FJson Parts = MakeShared<FJsonObject>();
		FJson Move = MakeShared<FJsonObject>();
		Move->SetStringField(TEXT("velocity"), Owner->GetVelocity().ToString());
		if (const UCharacterMovementComponent* M = Owner->GetCharacterMovement())
		{
			Move->SetStringField(TEXT("movementMode"), StaticEnum<EMovementMode>()->GetNameStringByValue(M->MovementMode));
		}
		Parts->SetObjectField(TEXT("Owner"), Move);
		// the pose the AnimGraph produced from those variables: every 8th bone, component space
		FJson Pose = MakeShared<FJsonObject>();
		const TArray<FTransform>& Bones = Mesh->GetComponentSpaceTransforms();
		for (int32 Index = 0; Index < Bones.Num(); Index += 8)
		{
			Pose->SetStringField(Mesh->GetBoneName(Index).ToString(), Bones[Index].ToString());
		}
		Parts->SetObjectField(TEXT("Pose"), Pose);
		Out->SetObjectField(TEXT("components"), Parts);
		return Out;
	}

	/** A skeletal mesh made for the anim Blueprint's target skeleton (the first by path, so every run picks the same). */
	static USkeletalMesh* FindMeshFor(const UClass* Class, const UBlueprint* Blueprint)
	{
		const UAnimBlueprint* AnimBlueprint = Cast<UAnimBlueprint>(Blueprint);
		const USkeleton* Skeleton = AnimBlueprint ? AnimBlueprint->TargetSkeleton.Get() : nullptr;
		if (!Skeleton) { return nullptr; }
		IAssetRegistry& Registry = FModuleManager::LoadModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry")).Get();
		Registry.SearchAllAssets(true);
		TArray<FAssetData> Meshes;
		Registry.GetAssetsByClass(USkeletalMesh::StaticClass()->GetClassPathName(), Meshes, true);
		Meshes.Sort([](const FAssetData& A, const FAssetData& B) { return A.GetObjectPathString() < B.GetObjectPathString(); });
		const FString SkeletonPath = Skeleton->GetPathName();
		for (const FAssetData& Asset : Meshes)
		{
			FString Tag;
			if (Asset.GetTagValue(TEXT("Skeleton"), Tag) && Tag.Contains(SkeletonPath))
			{
				if (USkeletalMesh* Mesh = Cast<USkeletalMesh>(Asset.GetAsset())) { return Mesh; }
			}
		}
		return nullptr;
	}

	static FJson DescribeProbe(const ACharacter* Probe)
	{
		FJson Out = MakeShared<FJsonObject>();
		Out->SetStringField(TEXT("location"), Probe->GetActorLocation().ToString());
		Out->SetStringField(TEXT("@rotation"), Probe->GetActorRotation().ToString());
		Out->SetStringField(TEXT("velocity"), Probe->GetVelocity().ToString());
		if (const UCharacterMovementComponent* Move = Probe->GetCharacterMovement())
		{
			Out->SetStringField(TEXT("movementMode"), StaticEnum<EMovementMode>()->GetNameStringByValue(Move->MovementMode));
			Out->SetStringField(TEXT("pendingLaunch"), Move->PendingLaunchVelocity.ToString());
			Out->SetBoolField(TEXT("@tickRegistered"), Move->PrimaryComponentTick.IsTickFunctionRegistered());
			Out->SetBoolField(TEXT("@tickEnabled"), Move->PrimaryComponentTick.IsTickFunctionEnabled());
			Out->SetBoolField(TEXT("@hasValidData"), Move->UpdatedComponent != nullptr);
		}
		Out->SetBoolField(TEXT("alive"), IsValid(Probe));
		TArray<AActor*> Overlapping;
		Probe->GetOverlappingActors(Overlapping);
		FString Names;
		for (const AActor* A : Overlapping) { Names += (Names.IsEmpty() ? TEXT("") : TEXT(",")) + A->GetName(); }
		Out->SetStringField(TEXT("@overlapping"), Names);
		Out->SetStringField(TEXT("@gravityZ"), FString::SanitizeFloat(Probe->GetWorld() ? Probe->GetWorld()->GetGravityZ() : 0.f));
		return Out;
	}
}

int32 DwoLensInspector::RunBehaviour(const FString& ScenarioFile, const FString& OutFile)
{
	using namespace DwoLensInspector::Behaviour;
	FJson Trace = MakeShared<FJsonObject>();
	TArray<TSharedPtr<FJsonValue>> Steps;
	auto Write = [&](bool bOk, const FString& Error) -> int32
	{
		Trace->SetStringField(TEXT("schema"), TEXT("dwookit.behaviour/1"));
		Trace->SetBoolField(TEXT("ok"), bOk);
		if (!Error.IsEmpty()) { Trace->SetStringField(TEXT("error"), Error); UE_LOG(LogDwoBehaviour, Error, TEXT("%s"), *Error); }
		Trace->SetArrayField(TEXT("steps"), Steps);
		FString Text;
		FJsonSerializer::Serialize(Trace.ToSharedRef(), TJsonWriterFactory<>::Create(&Text));
		FFileHelper::SaveStringToFile(Text, *OutFile, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
		return bOk ? 0 : 1;
	};

	FString Text;
	FJson Scenario;
	if (!FFileHelper::LoadFileToString(Text, *ScenarioFile) || !FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Text), Scenario) || !Scenario.IsValid())
	{
		return Write(false, TEXT("cannot read the scenario ") + ScenarioFile);
	}
	const FString BlueprintPath = Scenario->GetStringField(TEXT("blueprint"));
	const int32 Frames = FMath::Max(1, static_cast<int32>(Scenario->GetNumberField(TEXT("frames"))));
	const float Dt = static_cast<float>(Scenario->GetNumberField(TEXT("dt")));
	const int32 Every = FMath::Max(1, static_cast<int32>(Scenario->GetNumberField(TEXT("recordEvery"))));
	const int32 ProbeFrame = static_cast<int32>(Scenario->GetNumberField(TEXT("probeFrame")));
	bool bProbe = Scenario->GetStringField(TEXT("probe")) == TEXT("character");
	FVector ProbeOffset = FVector::ZeroVector;
	const TArray<TSharedPtr<FJsonValue>>* Offset = nullptr;
	if (Scenario->TryGetArrayField(TEXT("probeOffset"), Offset) && Offset->Num() == 3)
	{
		ProbeOffset = FVector((*Offset)[0]->AsNumber(), (*Offset)[1]->AsNumber(), (*Offset)[2]->AsNumber());
	}
	Trace->SetStringField(TEXT("blueprint"), BlueprintPath);
	// calls: functions or events to run on the subject at a frame (an implementable event the C++ parent would
	// fire), with arguments as text in parameter order; parameters without one are value-initialized
	struct FCall { int32 Frame; FString Function; TArray<FString> Args; };
	TArray<FCall> Calls;
	const TArray<TSharedPtr<FJsonValue>>* CallList = nullptr;
	if (Scenario->TryGetArrayField(TEXT("calls"), CallList))
	{
		for (const TSharedPtr<FJsonValue>& V : *CallList)
		{
			const TSharedPtr<FJsonObject> C = V->AsObject();
			if (!C.IsValid()) { continue; }
			FCall Call{ static_cast<int32>(C->GetNumberField(TEXT("frame"))), C->GetStringField(TEXT("function")), {} };
			const TArray<TSharedPtr<FJsonValue>>* Args = nullptr;
			if (C->TryGetArrayField(TEXT("args"), Args)) { for (const TSharedPtr<FJsonValue>& A : *Args) { Call.Args.Add(A->AsString()); } }
			Calls.Add(Call);
		}
	}

	UBlueprint* Blueprint = LoadObject<UBlueprint>(nullptr, *BlueprintPath);
	UClass* Class = Blueprint ? Blueprint->GeneratedClass.Get() : nullptr;
	if (!Class) { return Write(false, TEXT("Blueprint not found: ") + BlueprintPath); }
	enum class EKind { Actor, Widget, Anim } Kind = EKind::Actor;
	if (Class->IsChildOf(UUserWidget::StaticClass())) { Kind = EKind::Widget; bProbe = false; }
	else if (Class->IsChildOf(UAnimInstance::StaticClass())) { Kind = EKind::Anim; bProbe = false; }
	else if (!Class->IsChildOf(AActor::StaticClass()))
	{
		return Write(false, TEXT("the behaviour check runs actor, widget and animation Blueprints (this is a ") + Class->GetSuperClass()->GetName() + TEXT(")"));
	}
	Trace->SetStringField(TEXT("parent"), GetNameSafe(Class->GetSuperClass()));
	Trace->SetStringField(TEXT("kind"), Kind == EKind::Widget ? TEXT("widget") : Kind == EKind::Anim ? TEXT("anim") : TEXT("actor"));

	// The engine's own test world: a game instance, the project's game mode (so BeginPlay is dispatched the normal
	// way) and a frame counter advanced after every tick (the tick manager skips functions it thinks already ran this
	// frame, and nothing else advances the counter in a commandlet).
	FTestWorldWrapper Wrapper;
	if (!Wrapper.CreateTestWorld(EWorldType::Game) || !Wrapper.GetTestWorld()) { return Write(false, TEXT("could not create a test world")); }
	UWorld* World = Wrapper.GetTestWorld();
	World->PersistentLevel->bIsVisible = true;           // the tick manager only ticks visible levels
	if (!Wrapper.BeginPlayInTestWorld()) { Wrapper.DestroyTestWorld(false); return Write(false, TEXT("could not begin play in the test world")); }

	// subjects: the actor; or the widget (created and constructed as the viewport would, ticked through its Slate
	// widget with a fixed geometry); or the anim instance on a Character's mesh with a skeletal mesh of its skeleton
	AActor* Subject = nullptr;
	UUserWidget* Widget = nullptr;
	TSharedPtr<SWidget> SlateWidget;
	ACharacter* Owner = nullptr;
	ACharacter* Probe = nullptr;
	UObject* Target = nullptr;                // what calls run on
	const FGeometry Geometry = FGeometry::MakeRoot(FVector2D(1920, 1080), FSlateLayoutTransform());
	double SlateTime = 0;

	auto Alive = [&]() -> bool
	{
		return Kind == EKind::Actor ? IsValid(Subject) : Kind == EKind::Widget ? IsValid(Widget) : (IsValid(Owner) && Owner->GetMesh() && Owner->GetMesh()->GetAnimInstance());
	};
	auto Record = [&](const FString& Name, int32 Frame)
	{
		FJson Step = MakeShared<FJsonObject>();
		Step->SetStringField(TEXT("name"), Name);
		Step->SetNumberField(TEXT("frame"), Frame);
		Step->SetNumberField(TEXT("@worldTime"), World->GetTimeSeconds());
		Step->SetBoolField(TEXT("@begunPlay"), World->HasBegunPlay());
		Step->SetBoolField(TEXT("subjectAlive"), Alive());
		if (Alive())
		{
			if (Kind == EKind::Actor) { Step->SetObjectField(TEXT("subject"), DescribeActor(Subject)); }
			else if (Kind == EKind::Widget) { Step->SetObjectField(TEXT("subject"), DescribeWidget(Widget)); }
			else { Step->SetObjectField(TEXT("subject"), DescribeAnim(Owner)); }
		}
		if (Probe) { Step->SetObjectField(TEXT("probe"), DescribeProbe(Probe)); }
		Steps.Add(MakeShared<FJsonValueObject>(Step));
	};

	if (Kind == EKind::Actor)
	{
		FActorSpawnParameters SubjectParams;
		SubjectParams.Name = TEXT("Subject");
		SubjectParams.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
		Subject = World->SpawnActor<AActor>(Class, FTransform::Identity, SubjectParams);
		Target = Subject;
	}
	else if (Kind == EKind::Widget)
	{
		// a commandlet has no Slate application; widgets need one to build (nothing is painted, so no renderer)
		if (!FSlateApplication::IsInitialized()) { FSlateApplication::Create(); }
		Widget = CreateWidget<UUserWidget>(World->GetGameInstance(), Class, TEXT("Subject"));
		if (Widget) { SlateWidget = Widget->TakeWidget(); }     // builds the tree: PreConstruct, Construct
		Target = Widget;
	}
	else
	{
		USkeletalMesh* Mesh = FindMeshFor(Class, Blueprint);
		if (!Mesh) { Wrapper.DestroyTestWorld(false); return Write(false, TEXT("no skeletal mesh uses the skeleton of ") + BlueprintPath); }
		Trace->SetStringField(TEXT("mesh"), Mesh->GetPathName());
		const FTransform At(FVector(0, 0, 200));
		Owner = World->SpawnActorDeferred<ACharacter>(ACharacter::StaticClass(), At, nullptr, nullptr, ESpawnActorCollisionHandlingMethod::AlwaysSpawn);
		if (Owner)
		{
			Owner->Rename(TEXT("Subject"));
			if (UCharacterMovementComponent* Move = Owner->GetCharacterMovement()) { Move->bRunPhysicsWithNoController = true; }
			USkeletalMeshComponent* MeshComponent = Owner->GetMesh();
			// no renderer here: tick the pose regardless of visibility, as a visible character would
			MeshComponent->VisibilityBasedAnimTickOption = EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
			MeshComponent->SetSkeletalMesh(Mesh);
			MeshComponent->SetAnimInstanceClass(Class);
			Owner->FinishSpawning(At);
			if (UCharacterMovementComponent* Move = Owner->GetCharacterMovement()) { Move->SetMovementMode(MOVE_Falling); }
		}
		Target = Owner && Owner->GetMesh() ? Owner->GetMesh()->GetAnimInstance() : nullptr;
	}
	if (!Target) { Wrapper.DestroyTestWorld(false); return Write(false, TEXT("could not create ") + BlueprintPath); }
	Record(TEXT("spawn"), 0);

	for (int32 Frame = 1; Frame <= Frames; ++Frame)
	{
		if (bProbe && Frame == ProbeFrame && IsValid(Subject))
		{
			const FTransform Away(Subject->GetActorLocation() + FVector(0, 0, 5000));
			Probe = World->SpawnActorDeferred<ACharacter>(ACharacter::StaticClass(), Away, nullptr, nullptr, ESpawnActorCollisionHandlingMethod::AlwaysSpawn);
			if (Probe)
			{
				Probe->Rename(TEXT("Probe"));
				if (UCharacterMovementComponent* Move = Probe->GetCharacterMovement()) { Move->bRunPhysicsWithNoController = true; }
				Probe->FinishSpawning(Away);
				// tick canary: if component ticks run in this world, the probe turns 90 degrees a second
				URotatingMovementComponent* Canary = NewObject<URotatingMovementComponent>(Probe, TEXT("TickCanary"));
				Canary->RotationRate = FRotator(0, 90, 0);
				Canary->SetUpdatedComponent(Probe->GetRootComponent());
				Canary->RegisterComponent();
				if (UCharacterMovementComponent* Move = Probe->GetCharacterMovement()) { Move->SetMovementMode(MOVE_Falling); }
			}
		}
		if (bProbe && Frame == ProbeFrame + 1 && Probe && IsValid(Subject))
		{
			// swept down onto the actor: it stops at the first blocking surface (no start inside geometry, whose
			// push-out direction would depend on physics body order rather than on the Blueprint) and overlaps what
			// it passes through on the way, like something landing on the actor
			Probe->SetActorLocation(Subject->GetActorLocation() + ProbeOffset, true, nullptr, ETeleportType::None);
			Record(TEXT("probe"), Frame);
		}
		// the animated character: pushed sideways and up at the probe frame, so speed, direction and falling change
		if (Kind == EKind::Anim && Frame == ProbeFrame && IsValid(Owner))
		{
			Owner->LaunchCharacter(FVector(300, 150, 450), true, true);
			Record(TEXT("launch"), Frame);
		}
		for (const FCall& Call : Calls)
		{
			if (Call.Frame != Frame || !IsValid(Target)) { continue; }
			UFunction* Function = Target->FindFunction(FName(*Call.Function));
			if (!Function) { Wrapper.DestroyTestWorld(false); return Write(false, TEXT("no function ") + Call.Function + TEXT(" on the subject")); }
			uint8* Params = static_cast<uint8*>(FMemory::Malloc(FMath::Max<int32>(1, Function->ParmsSize), 16));
			FMemory::Memzero(Params, FMath::Max<int32>(1, Function->ParmsSize));
			int32 ArgIndex = 0;
			for (TFieldIterator<FProperty> It(Function); It && It->HasAnyPropertyFlags(CPF_Parm); ++It)
			{
				It->InitializeValue_InContainer(Params);
				if (!It->HasAnyPropertyFlags(CPF_ReturnParm | CPF_OutParm) || It->HasAnyPropertyFlags(CPF_ReferenceParm))
				{
					if (Call.Args.IsValidIndex(ArgIndex) && !It->ImportText_InContainer(*Call.Args[ArgIndex], Params, nullptr, PPF_None))
					{
						Wrapper.DestroyTestWorld(false);
						return Write(false, FString::Printf(TEXT("argument %d of %s is not a %s: %s"), ArgIndex + 1, *Call.Function, *It->GetCPPType(), *Call.Args[ArgIndex]));
					}
					++ArgIndex;
				}
			}
			Target->ProcessEvent(Function, Params);
			for (TFieldIterator<FProperty> It(Function); It && It->HasAnyPropertyFlags(CPF_Parm); ++It) { It->DestroyValue_InContainer(Params); }
			FMemory::Free(Params);
			Record(TEXT("call ") + Call.Function, Frame);
		}
		Wrapper.TickTestWorld(Dt);
		if (SlateWidget.IsValid() && IsValid(Widget))
		{
			SlateTime += Dt;
			SlateWidget->Tick(Geometry, SlateTime, Dt);              // NativeTick / Event Tick, animations, latent actions
		}
		if (Frame % Every == 0 || Frame == Frames) { Record(FString::Printf(TEXT("frame %d"), Frame), Frame); }
	}
	Trace->SetStringField(TEXT("gameMode"), GetNameSafe(World->GetAuthGameMode() ? World->GetAuthGameMode()->GetClass() : nullptr));
	SlateWidget.Reset();
	Wrapper.DestroyTestWorld(false);
	return Write(true, FString());
}
