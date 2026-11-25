// Test fixtures for the native migration: a Blueprint built from a JSON description, so dwookit's QA can migrate and
// behaviour-check node kinds the sample project does not use. Spec:
//   path       /Game/Folder/BP_Name (must not exist)
//   type       "interface": a Blueprint interface; functions: [{name, inputs: [{name, type}], outputs: [...]}]
//   parent     native parent class path (/Script/Engine.Actor)
//   interfaces Blueprint interface classes it implements (/Game/X/BPI_Y.BPI_Y_C)
//   variables  [{name, type: {category, subCategory?, object?, container?}, default?, expose?}]
//   nodes      [{id, and one of:
//                  event: "<class path>:<function>"   an override (ReceiveBeginPlay)
//                  custom: "<name>", params?: [{name, type}], net?: ["Server" | "Client" | "Multicast", "Reliable"]
//                  function: "<class path>:<function>"
//                  macro: "<standard macro name>"     (Gate, DoN, ForLoopWithBreak ...)
//                  get / set: "<variable>"
//                  class: "<K2Node class name>", props?: {property: text}  (set before the pins exist)
//                  entry / result: "<graph>"              that graph's existing entry or result node
//                and graph?: "<function or interface graph>" (default: the event graph), late?: true (made after a
//                first compile: calls of this Blueprint's own functions and events)}
//              ]
//   defaults   {"node.pin": "value"}   (object and class pins: an object path)
//   links      [["node.output", "node.input"], ...]
#include "DwoLensBuildBlueprint.h"

#include "AssetRegistry/AssetRegistryModule.h"
#include "EdGraph/EdGraph.h"
#include "EdGraphSchema_K2.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "K2Node_CallFunction.h"
#include "K2Node_CustomEvent.h"
#include "K2Node_Event.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "UObject/Interface.h"
#include "K2Node_MacroInstance.h"
#include "K2Node_VariableGet.h"
#include "K2Node_VariableSet.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Misc/FileHelper.h"
#include "Misc/PackageName.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/Package.h"
#include "UObject/SavePackage.h"
#include "UObject/UObjectIterator.h"

DEFINE_LOG_CATEGORY_STATIC(LogDwoBuild, Log, All);

namespace
{
	using FJson = TSharedPtr<FJsonObject>;

	FEdGraphPinType PinType(const FJson& T)
	{
		FEdGraphPinType Type;
		Type.PinCategory = FName(*T->GetStringField(TEXT("category")));
		FString Text;
		if (T->TryGetStringField(TEXT("subCategory"), Text)) { Type.PinSubCategory = FName(*Text); }
		if (T->TryGetStringField(TEXT("object"), Text)) { Type.PinSubCategoryObject = LoadObject<UObject>(nullptr, *Text); }
		if (Type.PinCategory == UEdGraphSchema_K2::PC_Real && Type.PinSubCategory.IsNone()) { Type.PinSubCategory = UEdGraphSchema_K2::PC_Double; }
		if (T->TryGetStringField(TEXT("container"), Text))
		{
			Type.ContainerType = Text == TEXT("array") ? EPinContainerType::Array : Text == TEXT("set") ? EPinContainerType::Set
				: Text == TEXT("map") ? EPinContainerType::Map : EPinContainerType::None;
		}
		const FJson* Value = nullptr;
		if (Type.ContainerType == EPinContainerType::Map && T->TryGetObjectField(TEXT("value"), Value))
		{
			Type.PinValueType.TerminalCategory = FName(*(*Value)->GetStringField(TEXT("category")));
			if ((*Value)->TryGetStringField(TEXT("object"), Text)) { Type.PinValueType.TerminalSubCategoryObject = LoadObject<UObject>(nullptr, *Text); }
			if (Type.PinValueType.TerminalCategory == UEdGraphSchema_K2::PC_Real) { Type.PinValueType.TerminalSubCategory = UEdGraphSchema_K2::PC_Double; }
		}
		return Type;
	}

	UClass* NodeClass(const FString& Name)
	{
		for (TObjectIterator<UClass> It; It; ++It)
		{
			if (It->GetName() == Name && It->IsChildOf(UEdGraphNode::StaticClass())) { return *It; }
		}
		return nullptr;
	}

	UFunction* FunctionAt(const FString& Ref)
	{
		FString ClassPath, Name;
		if (!Ref.Split(TEXT(":"), &ClassPath, &Name)) { return nullptr; }
		UClass* Class = LoadObject<UClass>(nullptr, *ClassPath);
		return Class ? Class->FindFunctionByName(FName(*Name)) : nullptr;
	}
}

int32 DwoLensInspector::BuildBlueprint(const FString& SpecFile)
{
	FString Text;
	FJson Spec;
	if (!FFileHelper::LoadFileToString(Text, *SpecFile) || !FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Text), Spec) || !Spec.IsValid())
	{
		UE_LOG(LogDwoBuild, Error, TEXT("cannot read %s"), *SpecFile);
		return 1;
	}
	const FString Path = Spec->GetStringField(TEXT("path"));
	if (FPackageName::DoesPackageExist(Path)) { UE_LOG(LogDwoBuild, Error, TEXT("%s exists; delete it first"), *Path); return 1; }
	FString Type;
	Spec->TryGetStringField(TEXT("type"), Type);
	const bool bInterface = Type == TEXT("interface");
	UClass* Parent = bInterface ? UInterface::StaticClass() : LoadObject<UClass>(nullptr, *Spec->GetStringField(TEXT("parent")));
	if (!Parent) { UE_LOG(LogDwoBuild, Error, TEXT("parent class not found")); return 1; }
	UPackage* Package = CreatePackage(*Path);
	const FName AssetName(*FPackageName::GetLongPackageAssetName(Path));
	UBlueprint* Blueprint = FKismetEditorUtilities::CreateBlueprint(Parent, Package, AssetName, bInterface ? BPTYPE_Interface : BPTYPE_Normal,
		UBlueprint::StaticClass(), UBlueprintGeneratedClass::StaticClass());
	if (!Blueprint) { UE_LOG(LogDwoBuild, Error, TEXT("could not create %s"), *Path); return 1; }
	FAssetRegistryModule::AssetCreated(Blueprint);

	int32 Problems = 0;
	auto Problem = [&Problems](const FString& Message) { UE_LOG(LogDwoBuild, Error, TEXT("%s"), *Message); ++Problems; };

	const TArray<TSharedPtr<FJsonValue>>* Functions = nullptr;
	if (bInterface && Spec->TryGetArrayField(TEXT("functions"), Functions))
	{
		for (const TSharedPtr<FJsonValue>& V : *Functions)
		{
			const FJson F = V->AsObject();
			UEdGraph* Graph = FBlueprintEditorUtils::CreateNewGraph(Blueprint, FName(*F->GetStringField(TEXT("name"))), UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
			FBlueprintEditorUtils::AddFunctionGraph<UClass>(Blueprint, Graph, true, nullptr);
			UK2Node_FunctionEntry* Entry = nullptr;
			for (UEdGraphNode* Node : Graph->Nodes) { if (!Entry) { Entry = Cast<UK2Node_FunctionEntry>(Node); } }
			const TArray<TSharedPtr<FJsonValue>>* Params = nullptr;
			if (Entry && F->TryGetArrayField(TEXT("inputs"), Params))
			{
				for (const TSharedPtr<FJsonValue>& P : *Params)
				{
					Entry->CreateUserDefinedPin(FName(*P->AsObject()->GetStringField(TEXT("name"))), PinType(P->AsObject()->GetObjectField(TEXT("type"))), EGPD_Output);
				}
			}
			if (F->TryGetArrayField(TEXT("outputs"), Params) && Params->Num() > 0)
			{
				FGraphNodeCreator<UK2Node_FunctionResult> Creator(*Graph);
				UK2Node_FunctionResult* Result = Creator.CreateNode();
				Result->NodePosX = 400;
				Creator.Finalize();
				for (const TSharedPtr<FJsonValue>& P : *Params)
				{
					Result->CreateUserDefinedPin(FName(*P->AsObject()->GetStringField(TEXT("name"))), PinType(P->AsObject()->GetObjectField(TEXT("type"))), EGPD_Input);
				}
			}
		}
	}
	const TArray<TSharedPtr<FJsonValue>>* Interfaces = nullptr;
	if (Spec->TryGetArrayField(TEXT("interfaces"), Interfaces))
	{
		for (const TSharedPtr<FJsonValue>& V : *Interfaces)
		{
			UClass* Interface = LoadObject<UClass>(nullptr, *V->AsString());
			if (!Interface || !FBlueprintEditorUtils::ImplementNewInterface(Blueprint, Interface->GetClassPathName()))
			{
				Problem(TEXT("cannot implement ") + V->AsString());
			}
		}
	}
	auto GraphNamed = [Blueprint](const FString& Name) -> UEdGraph*
	{
		for (UEdGraph* G : Blueprint->FunctionGraphs) { if (G && G->GetName() == Name) { return G; } }
		for (const FBPInterfaceDescription& I : Blueprint->ImplementedInterfaces) { for (UEdGraph* G : I.Graphs) { if (G && G->GetName() == Name) { return G; } } }
		return nullptr;
	};

	const TArray<TSharedPtr<FJsonValue>>* List = nullptr;
	if (Spec->TryGetArrayField(TEXT("variables"), List))
	{
		for (const TSharedPtr<FJsonValue>& V : *List)
		{
			const FJson Var = V->AsObject();
			FString Default;
			Var->TryGetStringField(TEXT("default"), Default);
			const FName VarName(*Var->GetStringField(TEXT("name")));
			if (!FBlueprintEditorUtils::AddMemberVariable(Blueprint, VarName, PinType(Var->GetObjectField(TEXT("type"))), Default))
			{
				Problem(TEXT("could not add variable ") + VarName.ToString());
			}
			bool bExpose = false;
			if (Var->TryGetBoolField(TEXT("expose"), bExpose) && bExpose)      // Instance Editable + Expose on Spawn
			{
				FBlueprintEditorUtils::SetBlueprintOnlyEditableFlag(Blueprint, VarName, false);
				FBlueprintEditorUtils::SetBlueprintVariableMetaData(Blueprint, VarName, nullptr, FBlueprintMetadata::MD_ExposeOnSpawn, TEXT("true"));
			}
		}
	}

	UEdGraph* const EventGraph = bInterface ? nullptr : FBlueprintEditorUtils::FindEventGraph(Blueprint);
	UBlueprint* Macros = LoadObject<UBlueprint>(nullptr, TEXT("/Engine/EditorBlueprintResources/StandardMacros.StandardMacros"));
	const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();
	TMap<FString, UEdGraphNode*> Nodes;
	int32 Column = 0;
	// late nodes call this Blueprint's own functions and events: made after a first compile creates them
	for (int32 Pass = 0; Pass < 2 && Spec->TryGetArrayField(TEXT("nodes"), List); ++Pass)
	{
		if (Pass == 1)
		{
			bool bAnyLate = false;
			for (const TSharedPtr<FJsonValue>& V : *List) { bool bLate = false; bAnyLate |= V->AsObject()->TryGetBoolField(TEXT("late"), bLate) && bLate; }
			if (!bAnyLate) { break; }
			FKismetEditorUtilities::CompileBlueprint(Blueprint, EBlueprintCompileOptions::SkipGarbageCollection);
		}
		for (const TSharedPtr<FJsonValue>& V : *List)
		{
			const FJson N = V->AsObject();
			bool bLateNode = false;
			N->TryGetBoolField(TEXT("late"), bLateNode);
			if (bLateNode != (Pass == 1)) { continue; }
			const FString Id = N->GetStringField(TEXT("id"));
			FString Ref;
			UEdGraphNode* Node = nullptr;
			UEdGraph* Graph = EventGraph;
			if (N->TryGetStringField(TEXT("graph"), Ref) && !(Graph = GraphNamed(Ref))) { Problem(TEXT("graph not found: ") + Ref); continue; }
			if (N->TryGetStringField(TEXT("entry"), Ref) || N->TryGetStringField(TEXT("result"), Ref))
			{
				UEdGraph* Owner = GraphNamed(Ref);
				const bool bEntry = N->HasField(TEXT("entry"));
				for (UEdGraphNode* Existing : Owner ? Owner->Nodes : TArray<TObjectPtr<UEdGraphNode>>())
				{
					if (bEntry ? (bool)Cast<UK2Node_FunctionEntry>(Existing) : (bool)Cast<UK2Node_FunctionResult>(Existing)) { Nodes.Add(Id, Existing); }
				}
				if (!Nodes.Contains(Id)) { Problem(FString::Printf(TEXT("%s: no %s node in %s"), *Id, bEntry ? TEXT("entry") : TEXT("result"), *Ref)); }
				continue;
			}
			if (N->TryGetStringField(TEXT("event"), Ref))
			{
				UK2Node_Event* Event = NewObject<UK2Node_Event>(Graph);
				FString ClassPath, Name;
				Ref.Split(TEXT(":"), &ClassPath, &Name);
				Event->EventReference.SetExternalMember(FName(*Name), LoadObject<UClass>(nullptr, *ClassPath));
				Event->bOverrideFunction = true;
				Node = Event;
			}
			else if (N->TryGetStringField(TEXT("custom"), Ref))
			{
				UK2Node_CustomEvent* Event = NewObject<UK2Node_CustomEvent>(Graph);
				Event->CustomFunctionName = FName(*Ref);
				// replication, as the event's details panel sets it: Server / Client / Multicast, Reliable
				const TArray<TSharedPtr<FJsonValue>>* Net = nullptr;
				if (N->TryGetArrayField(TEXT("net"), Net))
				{
					for (const TSharedPtr<FJsonValue>& F : *Net)
					{
						const FString Flag = F->AsString();
						Event->FunctionFlags |= FUNC_Net;
						if (Flag == TEXT("Server")) { Event->FunctionFlags |= FUNC_NetServer; }
						else if (Flag == TEXT("Client")) { Event->FunctionFlags |= FUNC_NetClient; }
						else if (Flag == TEXT("Multicast")) { Event->FunctionFlags |= FUNC_NetMulticast; }
						else if (Flag == TEXT("Reliable")) { Event->FunctionFlags |= FUNC_NetReliable; }
						else { Problem(TEXT("unknown net flag ") + Flag); }
					}
				}
				Node = Event;
			}
			else if (N->TryGetStringField(TEXT("function"), Ref))
			{
				UFunction* Function = FunctionAt(Ref);
				if (!Function) { Problem(TEXT("function not found: ") + Ref); continue; }
				UK2Node_CallFunction* Call = NewObject<UK2Node_CallFunction>(Graph);
				Call->SetFromFunction(Function);
				Node = Call;
			}
			else if (N->TryGetStringField(TEXT("macro"), Ref))
			{
				UEdGraph* Macro = nullptr;
				for (UEdGraph* G : Macros ? Macros->MacroGraphs : TArray<TObjectPtr<UEdGraph>>()) { if (G && G->GetName() == Ref) { Macro = G; } }
				if (!Macro)
				{
					FString Names;
					for (UEdGraph* G : Macros ? Macros->MacroGraphs : TArray<TObjectPtr<UEdGraph>>()) { if (G) { Names += TEXT(" '") + G->GetName() + TEXT("'"); } }
					Problem(TEXT("standard macro not found: ") + Ref + TEXT(" (macros:") + Names + TEXT(")"));
					continue;
				}
				UK2Node_MacroInstance* Instance = NewObject<UK2Node_MacroInstance>(Graph);
				Instance->SetMacroGraph(Macro);
				Node = Instance;
			}
			else if (N->TryGetStringField(TEXT("get"), Ref))
			{
				UK2Node_VariableGet* Get = NewObject<UK2Node_VariableGet>(Graph);
				Get->VariableReference.SetSelfMember(FName(*Ref));
				Node = Get;
			}
			else if (N->TryGetStringField(TEXT("set"), Ref))
			{
				UK2Node_VariableSet* Set = NewObject<UK2Node_VariableSet>(Graph);
				Set->VariableReference.SetSelfMember(FName(*Ref));
				Node = Set;
			}
			else if (N->TryGetStringField(TEXT("class"), Ref))
			{
				UClass* Class = NodeClass(Ref);
				if (!Class) { Problem(TEXT("node class not found: ") + Ref); continue; }
				Node = NewObject<UEdGraphNode>(Graph, Class);
				const FJson* Props = nullptr;
				if (N->TryGetObjectField(TEXT("props"), Props))
				{
					for (const TPair<FString, TSharedPtr<FJsonValue>>& P : (*Props)->Values)
					{
						FProperty* Property = FindFProperty<FProperty>(Class, FName(*P.Key));
						if (!Property || !Property->ImportText_InContainer(*P.Value->AsString(), Node, Node, PPF_None))
						{
							Problem(FString::Printf(TEXT("%s: cannot set %s"), *Id, *P.Key));
						}
					}
				}
			}
			if (!Node) { Problem(TEXT("node without a kind: ") + Id); continue; }
			Node->NodePosX = (Column % 6) * 360;
			Node->NodePosY = (Column / 6) * 260;
			++Column;
			Graph->AddNode(Node, false, false);
			Node->CreateNewGuid();
			Node->AllocateDefaultPins();          // before PostPlacedNewNode: some nodes look their pins up there
			Node->PostPlacedNewNode();
			if (UK2Node_CustomEvent* Event = Cast<UK2Node_CustomEvent>(Node))
			{
				const TArray<TSharedPtr<FJsonValue>>* Params = nullptr;
				if (N->TryGetArrayField(TEXT("params"), Params))
				{
					for (const TSharedPtr<FJsonValue>& P : *Params)
					{
						const FJson Param = P->AsObject();
						Event->CreateUserDefinedPin(FName(*Param->GetStringField(TEXT("name"))), PinType(Param->GetObjectField(TEXT("type"))), EGPD_Output);
					}
				}
			}
			Nodes.Add(Id, Node);
		}
	}

	auto FindPin = [&](const FString& Ref, EEdGraphPinDirection Dir) -> UEdGraphPin*
	{
		FString Id, Name;
		if (!Ref.Split(TEXT("."), &Id, &Name)) { return nullptr; }
		UEdGraphNode** Node = Nodes.Find(Id);
		if (!Node) { return nullptr; }
		UEdGraphPin* Pin = (*Node)->FindPin(FName(*Name), Dir);
		return Pin ? Pin : (*Node)->FindPin(FName(*Name));
	};

	const FJson* Defaults = nullptr;
	Spec->TryGetObjectField(TEXT("defaults"), Defaults);
	auto ApplyDefaults = [&](bool bReport)
	{
		if (!Defaults) { return; }
		for (const TPair<FString, TSharedPtr<FJsonValue>>& D : (*Defaults)->Values)
		{
			UEdGraphPin* Pin = FindPin(D.Key, EGPD_Input);
			if (!Pin)
			{
				if (bReport)
				{
					FString Id, Unused, Names;
					D.Key.Split(TEXT("."), &Id, &Unused);
					if (UEdGraphNode** Node = Nodes.Find(Id)) { for (UEdGraphPin* P : (*Node)->Pins) { Names += FString::Printf(TEXT(" '%s'"), *P->PinName.ToString()); } }
					Problem(TEXT("no pin for default ") + D.Key + TEXT(" (pins:") + Names + TEXT(")"));
				}
				continue;
			}
			if (Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Wildcard) { continue; }
			const FString Value = D.Value->AsString();
			const FName Category = Pin->PinType.PinCategory;
			if (Category == UEdGraphSchema_K2::PC_Object || Category == UEdGraphSchema_K2::PC_Class || Category == UEdGraphSchema_K2::PC_SoftClass
				|| Category == UEdGraphSchema_K2::PC_SoftObject || Category == UEdGraphSchema_K2::PC_Interface)
			{
				UObject* Object = LoadObject<UObject>(nullptr, *Value);
				if (!Object) { Problem(TEXT("default object not found: ") + Value); continue; }
				Schema->TrySetDefaultObject(*Pin, Object);
			}
			else
			{
				Schema->TrySetDefaultValue(*Pin, Value);
			}
		}
	};
	ApplyDefaults(false);           // class pins first: they create the pins links and other defaults name
	if (Spec->TryGetArrayField(TEXT("links"), List))
	{
		for (const TSharedPtr<FJsonValue>& V : *List)
		{
			const TArray<TSharedPtr<FJsonValue>>& L = V->AsArray();
			if (L.Num() != 2) { Problem(TEXT("a link needs two pins")); continue; }
			UEdGraphPin* From = FindPin(L[0]->AsString(), EGPD_Output);
			UEdGraphPin* To = FindPin(L[1]->AsString(), EGPD_Input);
			if (!From || !To)
			{
				FString Id, Unused;
				(From ? L[1] : L[0])->AsString().Split(TEXT("."), &Id, &Unused);
				FString Names;
				if (UEdGraphNode** Node = Nodes.Find(Id)) { for (UEdGraphPin* P : (*Node)->Pins) { Names += FString::Printf(TEXT(" '%s'"), *P->PinName.ToString()); } }
				Problem(FString::Printf(TEXT("link %s -> %s: pin not found (pins:%s)"), *L[0]->AsString(), *L[1]->AsString(), *Names));
				continue;
			}
			if (!Schema->TryCreateConnection(From, To))
			{
				Problem(FString::Printf(TEXT("link %s -> %s refused: %s"), *L[0]->AsString(), *L[1]->AsString(), *Schema->CanCreateConnection(From, To).Message.ToString()));
			}
		}
	}

	ApplyDefaults(true);            // again: wildcard pins have their types now
	FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
	FCompilerResultsLog Results;
	FKismetEditorUtilities::CompileBlueprint(Blueprint, EBlueprintCompileOptions::SkipGarbageCollection, &Results);
	for (const TSharedRef<FTokenizedMessage>& Message : Results.Messages)
	{
		if (Message->GetSeverity() == EMessageSeverity::Error) { Problem(TEXT("compile: ") + Message->ToText().ToString()); }
		else if (Message->GetSeverity() == EMessageSeverity::Warning) { UE_LOG(LogDwoBuild, Warning, TEXT("compile: %s"), *Message->ToText().ToString()); }
	}
	if (Problems > 0)
	{
		UE_LOG(LogDwoBuild, Error, TEXT("%s: %d problem(s), not saved"), *Path, Problems);
		return 1;
	}
	const FString File = FPackageName::LongPackageNameToFilename(Path, FPackageName::GetAssetPackageExtension());
	FSavePackageArgs Args;
	Args.TopLevelFlags = RF_Public | RF_Standalone;
	if (!UPackage::SavePackage(Package, Blueprint, *File, Args)) { UE_LOG(LogDwoBuild, Error, TEXT("save failed: %s"), *File); return 1; }
	UE_LOG(LogDwoBuild, Display, TEXT("built %s (%d nodes)"), *File, Nodes.Num());
	return 0;
}
