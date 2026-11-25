// Native migration, behaviour check: runs one actor Blueprint through a fixed scenario in a headless game world and
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
#include "DwoLensBehaviour.h"

#include "Components/ActorComponent.h"
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
	const bool bProbe = Scenario->GetStringField(TEXT("probe")) == TEXT("character");
	FVector ProbeOffset = FVector::ZeroVector;
	const TArray<TSharedPtr<FJsonValue>>* Offset = nullptr;
	if (Scenario->TryGetArrayField(TEXT("probeOffset"), Offset) && Offset->Num() == 3)
	{
		ProbeOffset = FVector((*Offset)[0]->AsNumber(), (*Offset)[1]->AsNumber(), (*Offset)[2]->AsNumber());
	}
	Trace->SetStringField(TEXT("blueprint"), BlueprintPath);
	// calls: functions or events to run on the actor at a frame (an implementable event the C++ parent would fire)
	TArray<TPair<int32, FString>> Calls;
	const TArray<TSharedPtr<FJsonValue>>* CallList = nullptr;
	if (Scenario->TryGetArrayField(TEXT("calls"), CallList))
	{
		for (const TSharedPtr<FJsonValue>& V : *CallList)
		{
			const TSharedPtr<FJsonObject> C = V->AsObject();
			if (C.IsValid()) { Calls.Emplace(static_cast<int32>(C->GetNumberField(TEXT("frame"))), C->GetStringField(TEXT("function"))); }
		}
	}

	UBlueprint* Blueprint = LoadObject<UBlueprint>(nullptr, *BlueprintPath);
	UClass* Class = Blueprint ? Blueprint->GeneratedClass.Get() : nullptr;
	if (!Class) { return Write(false, TEXT("Blueprint not found: ") + BlueprintPath); }
	if (!Class->IsChildOf(AActor::StaticClass())) { return Write(false, TEXT("the behaviour check runs actor Blueprints only (this is a ") + Class->GetSuperClass()->GetName() + TEXT(")")); }
	Trace->SetStringField(TEXT("parent"), GetNameSafe(Class->GetSuperClass()));

	// The engine's own test world: a game instance, the project's game mode (so BeginPlay is dispatched the normal
	// way) and a frame counter advanced after every tick (the tick manager skips functions it thinks already ran this
	// frame, and nothing else advances the counter in a commandlet).
	FTestWorldWrapper Wrapper;
	if (!Wrapper.CreateTestWorld(EWorldType::Game) || !Wrapper.GetTestWorld()) { return Write(false, TEXT("could not create a test world")); }
	UWorld* World = Wrapper.GetTestWorld();
	World->PersistentLevel->bIsVisible = true;           // the tick manager only ticks visible levels
	if (!Wrapper.BeginPlayInTestWorld()) { Wrapper.DestroyTestWorld(false); return Write(false, TEXT("could not begin play in the test world")); }

	FActorSpawnParameters SubjectParams;
	SubjectParams.Name = TEXT("Subject");
	SubjectParams.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	AActor* Subject = World->SpawnActor<AActor>(Class, FTransform::Identity, SubjectParams);
	ACharacter* Probe = nullptr;

	auto Record = [&](const FString& Name, int32 Frame)
	{
		FJson Step = MakeShared<FJsonObject>();
		Step->SetStringField(TEXT("name"), Name);
		Step->SetNumberField(TEXT("frame"), Frame);
		Step->SetNumberField(TEXT("@worldTime"), World->GetTimeSeconds());
		Step->SetBoolField(TEXT("@begunPlay"), World->HasBegunPlay());
		Step->SetBoolField(TEXT("subjectAlive"), IsValid(Subject));
		if (IsValid(Subject)) { Step->SetObjectField(TEXT("subject"), DescribeActor(Subject)); }
		if (Probe) { Step->SetObjectField(TEXT("probe"), DescribeProbe(Probe)); }
		Steps.Add(MakeShared<FJsonValueObject>(Step));
	};

	if (!Subject) { Wrapper.DestroyTestWorld(false); return Write(false, TEXT("could not spawn ") + BlueprintPath); }
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
		for (const TPair<int32, FString>& Call : Calls)
		{
			if (Call.Key != Frame || !IsValid(Subject)) { continue; }
			UFunction* Function = Subject->FindFunction(FName(*Call.Value));
			if (!Function) { Wrapper.DestroyTestWorld(false); return Write(false, TEXT("no function ") + Call.Value + TEXT(" on the actor")); }
			// default arguments: every parameter value-initialized
			uint8* Params = static_cast<uint8*>(FMemory::Malloc(FMath::Max<int32>(1, Function->ParmsSize), 16));
			FMemory::Memzero(Params, FMath::Max<int32>(1, Function->ParmsSize));
			for (TFieldIterator<FProperty> It(Function); It && It->HasAnyPropertyFlags(CPF_Parm); ++It) { It->InitializeValue_InContainer(Params); }
			Subject->ProcessEvent(Function, Params);
			for (TFieldIterator<FProperty> It(Function); It && It->HasAnyPropertyFlags(CPF_Parm); ++It) { It->DestroyValue_InContainer(Params); }
			FMemory::Free(Params);
			Record(TEXT("call ") + Call.Value, Frame);
		}
		Wrapper.TickTestWorld(Dt);
		if (Frame % Every == 0 || Frame == Frames) { Record(FString::Printf(TEXT("frame %d"), Frame), Frame); }
	}
	Trace->SetStringField(TEXT("gameMode"), GetNameSafe(World->GetAuthGameMode() ? World->GetAuthGameMode()->GetClass() : nullptr));
	Wrapper.DestroyTestWorld(false);
	return Write(true, FString());
}
