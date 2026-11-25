// Native migration, engine side: rebases a Blueprint onto the C++ class dwookit generated for it (tools/lens_native.py)
// and proves nothing changed that should not have.
//
//   -LensRebase=<Name.native.json> -LensRebaseReport=<report.json> [-LensDryRun] [-LensForce]
//
// 1. snapshot   every property of the Blueprint's class default object and of each component template (text form)
// 2. move       migrated variables, components, functions, events and dispatchers leave the Blueprint; nodes that
//               used them are retargeted to the inherited C++ members by name (the engine's RemoveMemberVariable
//               would delete those nodes, so declarations are removed directly)
// 3. reparent   ParentClass = the generated class; children of moved components attach to the native ones
// 4. restore    the snapshot values are written back onto the new class default object and native components
// 5. verify     compile must succeed; the snapshot is compared again and any difference fails the rebase
// 6. save       only when everything above passed (and not -LensDryRun); the caller keeps a backup of the file
#include "DwoLensNativeRebase.h"

#include "Components/ActorComponent.h"
#include "Components/SceneComponent.h"
#include "Dom/JsonObject.h"
#include "EdGraph/EdGraph.h"
#include "EdGraphSchema_K2.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/SCS_Node.h"
#include "Engine/SimpleConstructionScript.h"
#include "K2Node_CallFunction.h"
#include "K2Node_CustomEvent.h"
#include "K2Node_Event.h"
#include "K2Node.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "K2Node_Variable.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Misc/FileHelper.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UObject/Package.h"
#include "UObject/SavePackage.h"
#include "UObject/UnrealType.h"

DEFINE_LOG_CATEGORY_STATIC(LogDwoNative, Log, All);

namespace DwoLensInspector::NativeRebase
{
	using FJson = TSharedPtr<FJsonObject>;
	using FProps = TMap<FString, FString>;

	struct FMapping
	{
		FString Blueprint;
		FString ClassPath;
		TMap<FString, FString> Variables;      // Blueprint name -> C++ name (migrated only)
		TMap<FString, FString> Components;
		TMap<FString, FString> Dispatchers;
		TMap<FString, FString> Functions;
		TMap<FString, TMap<FString, FString>> FunctionPins;   // function -> Blueprint pin -> C++ param
		TSet<FString> EventNodes;              // node ids of migrated events and every node only they used
		TSet<FString> EventRoots;              // the event nodes themselves
		TSet<FString> ConstructionNodes;
		bool bConstruction = false;
		TMap<FString, FString> EventThunks;    // implementable event node id -> C++ function it now calls
		TMap<FString, FString> FunctionThunks; // implementable event function graph -> C++ function it now calls
		TMap<FString, TSet<FString>> ThunkNodes;   // function graph -> its nodes that moved
		TArray<TPair<FString, FString>> Literals;   // C++ property -> asset path
	};

	static bool ReadMapping(const FString& File, FMapping& Out, FString& Error)
	{
		FString Text;
		if (!FFileHelper::LoadFileToString(Text, *File)) { Error = TEXT("cannot read ") + File; return false; }
		FJson Root;
		if (!FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Text), Root) || !Root.IsValid()) { Error = TEXT("not JSON: ") + File; return false; }
		Out.Blueprint = Root->GetStringField(TEXT("blueprint"));
		Out.ClassPath = Root->GetStringField(TEXT("classPath"));
		auto Pairs = [&Root](const TCHAR* Field, TMap<FString, FString>& Map)
		{
			const TArray<TSharedPtr<FJsonValue>>* Items = nullptr;
			if (!Root->TryGetArrayField(Field, Items)) { return; }
			for (const TSharedPtr<FJsonValue>& V : *Items)
			{
				const FJson Item = V->AsObject();
				if (Item.IsValid() && Item->GetBoolField(TEXT("migrated"))) { Map.Add(Item->GetStringField(TEXT("bp")), Item->GetStringField(TEXT("cpp"))); }
			}
		};
		Pairs(TEXT("variables"), Out.Variables);
		Pairs(TEXT("components"), Out.Components);
		Pairs(TEXT("dispatchers"), Out.Dispatchers);
		const TArray<TSharedPtr<FJsonValue>>* Functions = nullptr;
		if (Root->TryGetArrayField(TEXT("functions"), Functions))
		{
			for (const TSharedPtr<FJsonValue>& V : *Functions)
			{
				const FJson F = V->AsObject();
				if (!F.IsValid() || !F->GetBoolField(TEXT("migrated"))) { continue; }
				const FString Bp = F->GetStringField(TEXT("bp"));
				bool bThunk = false;
				if (F->TryGetBoolField(TEXT("thunk"), bThunk) && bThunk)
				{
					Out.FunctionThunks.Add(Bp, F->GetStringField(TEXT("cpp")));
					TSet<FString>& Nodes = Out.ThunkNodes.Add(Bp);
					for (const TSharedPtr<FJsonValue>& N : F->GetArrayField(TEXT("nodes"))) { Nodes.Add(N->AsString()); }
					continue;
				}
				if (Bp == TEXT("UserConstructionScript"))
				{
					Out.bConstruction = true;
					for (const TSharedPtr<FJsonValue>& N : F->GetArrayField(TEXT("nodes"))) { Out.ConstructionNodes.Add(N->AsString()); }
					continue;
				}
				Out.Functions.Add(Bp, F->GetStringField(TEXT("cpp")));
				TMap<FString, FString>& Pins = Out.FunctionPins.Add(Bp);
				for (const TSharedPtr<FJsonValue>& P : F->GetArrayField(TEXT("params")))
				{
					Pins.Add(P->AsObject()->GetStringField(TEXT("bp")), P->AsObject()->GetStringField(TEXT("cpp")));
				}
			}
		}
		const TArray<TSharedPtr<FJsonValue>>* Events = nullptr;
		if (Root->TryGetArrayField(TEXT("events"), Events))
		{
			for (const TSharedPtr<FJsonValue>& V : *Events)
			{
				const FJson E = V->AsObject();
				if (!E.IsValid() || !E->GetBoolField(TEXT("migrated"))) { continue; }
				const FString Node = E->GetStringField(TEXT("node"));
				FString Kind;
				E->TryGetStringField(TEXT("kind"), Kind);
				if (Kind == TEXT("thunk"))
				{
					// the event stays and calls the C++ function; only its body moves
					Out.EventThunks.Add(Node, E->GetStringField(TEXT("cpp")));
					for (const TSharedPtr<FJsonValue>& N : E->GetArrayField(TEXT("nodes"))) { if (N->AsString() != Node) { Out.EventNodes.Add(N->AsString()); } }
					continue;
				}
				Out.EventNodes.Add(Node);
				Out.EventRoots.Add(Node);
				for (const TSharedPtr<FJsonValue>& N : E->GetArrayField(TEXT("nodes"))) { Out.EventNodes.Add(N->AsString()); }
				// a migrated custom event is called like a function: same name in C++
				const FString Cpp = E->GetStringField(TEXT("cpp"));
				if (!Cpp.IsEmpty() && (Kind.IsEmpty() || Kind == TEXT("custom"))) { Out.Functions.FindOrAdd(E->GetStringField(TEXT("bp")), Cpp); }
			}
		}
		// timelines: the timeline node and its flows move (removing the node removes the template); the timeline
		// variable and the properties its tracks drove are C++ members now (renamed where the names differ)
		const TArray<TSharedPtr<FJsonValue>>* Timelines = nullptr;
		if (Root->TryGetArrayField(TEXT("timelines"), Timelines))
		{
			for (const TSharedPtr<FJsonValue>& V : *Timelines)
			{
				const FJson T = V->AsObject();
				if (!T.IsValid() || !T->GetBoolField(TEXT("migrated"))) { continue; }
				for (const TSharedPtr<FJsonValue>& N : T->GetArrayField(TEXT("nodes"))) { Out.EventNodes.Add(N->AsString()); }
				Out.Variables.Add(T->GetStringField(TEXT("bp")), T->GetStringField(TEXT("cpp")));
				const TSharedPtr<FJsonObject>* Props = nullptr;
				if (T->TryGetObjectField(TEXT("properties"), Props))
				{
					for (const TPair<FString, TSharedPtr<FJsonValue>>& P : (*Props)->Values) { Out.Variables.Add(P.Key, P.Value->AsString()); }
				}
			}
		}
		const TArray<TSharedPtr<FJsonValue>>* Literals = nullptr;
		if (Root->TryGetArrayField(TEXT("literals"), Literals))
		{
			for (const TSharedPtr<FJsonValue>& V : *Literals)
			{
				Out.Literals.Emplace(V->AsObject()->GetStringField(TEXT("cpp")), V->AsObject()->GetStringField(TEXT("path")));
			}
		}
		return true;
	}

	/** Every property of Object that is data (not transient, not a pointer to its own subobjects), as text. */
	static FProps Snapshot(const UObject* Object)
	{
		FProps Out;
		if (!Object) { return Out; }
		for (TFieldIterator<FProperty> It(Object->GetClass()); It; ++It)
		{
			const FProperty* Property = *It;
			if (Property->HasAnyPropertyFlags(CPF_Transient | CPF_DuplicateTransient | CPF_Deprecated)) { continue; }
			if (const FObjectPropertyBase* ObjectProperty = CastField<FObjectPropertyBase>(Property))
			{
				const UObject* Target = ObjectProperty->GetObjectPropertyValue_InContainer(Object);
				if (Target && Target->IsIn(Object)) { continue; }
			}
			FString Text;
			Property->ExportTextItem_InContainer(Text, Object, nullptr, const_cast<UObject*>(Object), PPF_None);
			Out.Add(Property->GetName(), Text);
		}
		return Out;
	}

	/** Writes Values onto Object where they differ; returns the names it could not write. */
	static TArray<FString> Restore(UObject* Object, const FProps& Values, const TMap<FString, FString>& Rename)
	{
		TArray<FString> Failed;
		if (!Object) { return Failed; }
		for (const TPair<FString, FString>& Pair : Values)
		{
			const FString* Renamed = Rename.Find(Pair.Key);
			const FString Name = Renamed ? *Renamed : Pair.Key;
			FProperty* Property = FindFProperty<FProperty>(Object->GetClass(), *Name);
			if (!Property) { continue; }
			if (const FObjectPropertyBase* ObjectProperty = CastField<FObjectPropertyBase>(Property))
			{
				const UObject* Current = ObjectProperty->GetObjectPropertyValue_InContainer(Object);
				if (Current && Current->IsIn(Object)) { continue; }      // native component: set by the C++ constructor
			}
			FString Now;
			Property->ExportTextItem_InContainer(Now, Object, nullptr, Object, PPF_None);
			if (Now == Pair.Value) { continue; }
			if (!Property->ImportText_InContainer(*Pair.Value, Object, Object, PPF_None)) { Failed.Add(Name); continue; }
			// The snapshot text leaves out struct fields that are zero (false, 0, None); importing it onto a value the C++
			// constructor set keeps that value instead (BodyInstance.bEnableGravity: false in the Blueprint, true in C++).
			// Fields the text does not name are zero in the original: set them so.
			if (const FStructProperty* StructProperty = CastField<FStructProperty>(Property))
			{
				void* Value = StructProperty->ContainerPtrToValuePtr<void>(Object);
				for (TFieldIterator<FProperty> Inner(StructProperty->Struct); Inner; ++Inner)
				{
					if (Inner->ArrayDim != 1 || !Inner->ShouldPort(PPF_None)) { continue; }
					if (Inner->Identical_InContainer(Value, nullptr, 0, PPF_None)) { continue; }
					const FString Key = Inner->GetName() + TEXT("=");
					if (Pair.Value.Contains(TEXT("(") + Key) || Pair.Value.Contains(TEXT(",") + Key)) { continue; }
					Inner->ClearValue_InContainer(Value);
				}
			}
		}
		return Failed;
	}

	static void Retarget(UBlueprint* Blueprint, const FMapping& Map)
	{
		TArray<UEdGraph*> Graphs;
		Blueprint->GetAllGraphs(Graphs);
		for (UEdGraph* Graph : Graphs)
		{
			for (UEdGraphNode* Node : Graph->Nodes)
			{
				if (UK2Node_Variable* Var = Cast<UK2Node_Variable>(Node))
				{
					if (!Var->VariableReference.IsSelfContext()) { continue; }
					const FString Old = Var->VariableReference.GetMemberName().ToString();
					const FString* New = Map.Variables.Find(Old);
					if (!New) { New = Map.Components.Find(Old); }
					if (!New) { New = Map.Dispatchers.Find(Old); }
					if (!New) { continue; }
					for (UEdGraphPin* Pin : Var->Pins)
					{
						// the value pin, and the pins of a split struct value ('Door Size_X')
						const FString PinName = Pin->PinName.ToString();
						if (PinName == Old) { Pin->PinName = FName(**New); }
						else if (PinName.StartsWith(Old + TEXT("_"))) { Pin->PinName = FName(*(*New + PinName.RightChop(Old.Len()))); }
					}
					Var->VariableReference.SetSelfMember(FName(**New));
				}
				else if (UK2Node_CallFunction* Call = Cast<UK2Node_CallFunction>(Node))
				{
					if (!Call->FunctionReference.IsSelfContext()) { continue; }
					const FString Old = Call->FunctionReference.GetMemberName().ToString();
					const FString* New = Map.Functions.Find(Old);
					if (!New) { continue; }
					if (const TMap<FString, FString>* Pins = Map.FunctionPins.Find(Old))
					{
						for (UEdGraphPin* Pin : Call->Pins)
						{
							if (const FString* NewPin = Pins->Find(Pin->PinName.ToString())) { Pin->PinName = FName(**NewPin); }
						}
					}
					Call->FunctionReference.SetSelfMember(FName(**New));
				}
			}
		}
	}

	static bool IsPure(const UEdGraphNode* Node)
	{
		const UK2Node* K2 = Cast<UK2Node>(Node);
		if (!K2 || !K2->IsNodePure()) { return false; }
		for (const UEdGraphPin* Pin : Node->Pins) { if (Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec) { return false; } }
		return true;
	}

	/**
	 * Removes the migrated code: every node that runs (events, calls, sets, flow control) always goes, so nothing runs
	 * twice. A pure data node another kept node also reads stays (it is harmless). If kept code reads the output of a
	 * node that is removed, the Blueprint would change behaviour: reported, and the rebase is not saved.
	 */
	static FString Id(const UEdGraphNode* Node) { return Node->NodeGuid.ToString(EGuidFormats::Digits); }

	static int32 RemoveNodes(UBlueprint* Blueprint, const TSet<FString>& Ids, const TArray<UEdGraph*>& Graphs, TArray<FString>& Problems,
		const TSet<FString>& KeptRoots = TSet<FString>(), bool bKeepResults = false)
	{
		TArray<UEdGraphNode*> Candidates;
		for (UEdGraph* Graph : Graphs)
		{
			for (UEdGraphNode* Node : Graph->Nodes)
			{
				if (!Node || !Ids.Contains(Id(Node)) || KeptRoots.Contains(Id(Node))) { continue; }
				if (bKeepResults && Cast<UK2Node_FunctionResult>(Node)) { continue; }
				Candidates.Add(Node);
			}
		}
		bool bChanged = true;
		while (bChanged)            // shared pure data nodes stay, and so do pure nodes only they read
		{
			bChanged = false;
			for (int32 Index = Candidates.Num() - 1; Index >= 0; --Index)
			{
				const UEdGraphNode* Node = Candidates[Index];
				if (!IsPure(Node)) { continue; }
				for (const UEdGraphPin* Pin : Node->Pins)
				{
					if (Pin->Direction != EGPD_Output) { continue; }
					for (const UEdGraphPin* Linked : Pin->LinkedTo)
					{
						if (Linked && !Candidates.Contains(Linked->GetOwningNode()) && !bChanged)
						{
							Candidates.RemoveAt(Index);
							bChanged = true;
						}
					}
					if (bChanged) { break; }
				}
				if (bChanged) { break; }
			}
		}
		for (const UEdGraphNode* Node : Candidates)
		{
			if (IsPure(Node)) { continue; }
			for (const UEdGraphPin* Pin : Node->Pins)
			{
				if (Pin->Direction != EGPD_Output || Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec) { continue; }
				for (const UEdGraphPin* Linked : Pin->LinkedTo)
				{
					if (Linked && !Candidates.Contains(Linked->GetOwningNode()))
					{
						Problems.Add(FString::Printf(TEXT("kept Blueprint code reads '%s' from '%s', which moved to C++"),
							*Pin->PinName.ToString(), *Node->GetNodeTitle(ENodeTitleType::ListView).ToString()));
					}
				}
			}
		}
		// code that stays must not run into code that moved: it would silently stop there
		for (UEdGraph* Graph : Graphs)
		{
			for (const UEdGraphNode* Node : Graph->Nodes)
			{
				if (!Node || Candidates.Contains(Node) || KeptRoots.Contains(Id(Node))) { continue; }
				if (Cast<UK2Node_FunctionEntry>(Node) && bKeepResults) { continue; }
				for (const UEdGraphPin* Pin : Node->Pins)
				{
					if (Pin->Direction != EGPD_Output || Pin->PinType.PinCategory != UEdGraphSchema_K2::PC_Exec) { continue; }
					for (const UEdGraphPin* Linked : Pin->LinkedTo)
					{
						if (Linked && Candidates.Contains(Linked->GetOwningNode()) && !Cast<UK2Node_FunctionEntry>(Linked->GetOwningNode()))
						{
							Problems.Add(FString::Printf(TEXT("kept Blueprint code '%s' runs into '%s', which moved to C++"),
								*Node->GetNodeTitle(ENodeTitleType::ListView).ToString(), *Linked->GetOwningNode()->GetNodeTitle(ENodeTitleType::ListView).ToString()));
						}
					}
				}
			}
		}
		for (UEdGraphNode* Node : Candidates)
		{
			if (!Cast<UK2Node_FunctionEntry>(Node)) { FBlueprintEditorUtils::RemoveNode(Blueprint, Node, true); }
		}
		return Candidates.Num();
	}

	/** A call to Function placed next to From, its exec input wired to From's ExecOut and its inputs to From's outputs of the same name. */
	static UK2Node_CallFunction* AddThunkCall(UEdGraph* Graph, UEdGraphNode* From, UEdGraphPin* ExecOut, UFunction* Function, TArray<FString>& Problems)
	{
		const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();
		FGraphNodeCreator<UK2Node_CallFunction> Creator(*Graph);
		UK2Node_CallFunction* Call = Creator.CreateNode(false);
		Call->SetFromFunction(Function);
		Call->NodePosX = From->NodePosX + 320;
		Call->NodePosY = From->NodePosY;
		Creator.Finalize();
		if (ExecOut && !Schema->TryCreateConnection(ExecOut, Call->GetExecPin()))
		{
			Problems.Add(TEXT("could not wire the call to ") + Function->GetName());
		}
		for (UEdGraphPin* Pin : From->Pins)
		{
			if (Pin->Direction != EGPD_Output || Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec || Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Delegate) { continue; }
			UEdGraphPin* In = Call->FindPin(Pin->PinName, EGPD_Input);
			if (!In || !Schema->TryCreateConnection(Pin, In))
			{
				Problems.Add(FString::Printf(TEXT("could not pass %s to %s"), *Pin->PinName.ToString(), *Function->GetName()));
			}
		}
		return Call;
	}

	static FJson Diff(const TCHAR* Scope, const FString& Name, const FString& Before, const FString& After)
	{
		FJson D = MakeShared<FJsonObject>();
		D->SetStringField(TEXT("scope"), Scope);
		D->SetStringField(TEXT("property"), Name);
		// long values (a BodyInstance is thousands of characters): the part around the first difference
		int32 First = 0;
		while (First < Before.Len() && First < After.Len() && Before[First] == After[First]) { ++First; }
		const int32 From = (Before.Len() > 500 || After.Len() > 500) ? FMath::Max(0, First - 200) : 0;
		D->SetStringField(TEXT("before"), (From > 0 ? TEXT("...") : TEXT("")) + Before.Mid(From, 500));
		D->SetStringField(TEXT("after"), (From > 0 ? TEXT("...") : TEXT("")) + After.Mid(From, 500));
		return D;
	}
}

int32 DwoLensInspector::RebaseBlueprint(const FString& MappingFile, const FString& ReportFile, bool bDryRun, bool bForce)
{
	using namespace DwoLensInspector::NativeRebase;
	FJson Report = MakeShared<FJsonObject>();
	TArray<TSharedPtr<FJsonValue>> Steps, Diffs, Problems;
	auto Step = [&Steps](const FString& Text) { UE_LOG(LogDwoNative, Display, TEXT("%s"), *Text); Steps.Add(MakeShared<FJsonValueString>(Text)); };
	auto Problem = [&Problems](const FString& Text) { UE_LOG(LogDwoNative, Warning, TEXT("%s"), *Text); Problems.Add(MakeShared<FJsonValueString>(Text)); };
	auto Finish = [&](bool bOk, bool bSaved) -> int32
	{
		Report->SetStringField(TEXT("schema"), TEXT("dwookit.rebase/1"));
		Report->SetBoolField(TEXT("ok"), bOk);
		Report->SetBoolField(TEXT("saved"), bSaved);
		Report->SetBoolField(TEXT("dryRun"), bDryRun);
		Report->SetArrayField(TEXT("steps"), Steps);
		Report->SetArrayField(TEXT("diffs"), Diffs);
		Report->SetArrayField(TEXT("problems"), Problems);
		FString Text;
		FJsonSerializer::Serialize(Report.ToSharedRef(), TJsonWriterFactory<>::Create(&Text));
		FFileHelper::SaveStringToFile(Text, *ReportFile, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
		return bOk ? 0 : 1;
	};

	FMapping Map;
	FString Error;
	if (!ReadMapping(MappingFile, Map, Error)) { Problem(Error); return Finish(false, false); }
	Report->SetStringField(TEXT("blueprint"), Map.Blueprint);
	Report->SetStringField(TEXT("class"), Map.ClassPath);

	UBlueprint* Blueprint = LoadObject<UBlueprint>(nullptr, *Map.Blueprint);
	UClass* NewParent = LoadObject<UClass>(nullptr, *Map.ClassPath);
	if (!Blueprint) { Problem(TEXT("Blueprint not found: ") + Map.Blueprint); return Finish(false, false); }
	if (!NewParent) { Problem(TEXT("generated class not found (is the project compiled?): ") + Map.ClassPath); return Finish(false, false); }
	if (Blueprint->ParentClass == NewParent) { Problem(TEXT("already rebased onto ") + Map.ClassPath); return Finish(false, false); }
	if (NewParent->GetSuperClass() != Blueprint->ParentClass)
	{
		Problem(FString::Printf(TEXT("the generated class derives from %s but the Blueprint's parent is %s (export and generate again)"),
			*GetNameSafe(NewParent->GetSuperClass()), *GetNameSafe(Blueprint->ParentClass)));
		return Finish(false, false);
	}

	// 1. snapshot
	UClass* OldClass = Blueprint->GeneratedClass;
	const FProps BeforeDefaults = Snapshot(OldClass ? OldClass->GetDefaultObject() : nullptr);
	TMap<FString, FProps> BeforeComponents;
	USimpleConstructionScript* Scs = Blueprint->SimpleConstructionScript;
	if (Scs)
	{
		for (USCS_Node* Node : Scs->GetAllNodes())
		{
			if (Node && Map.Components.Contains(Node->GetVariableName().ToString()))
			{
				BeforeComponents.Add(Node->GetVariableName().ToString(), Snapshot(Node->ComponentTemplate));
			}
		}
	}
	Step(FString::Printf(TEXT("snapshot: %d class default values, %d component templates"), BeforeDefaults.Num(), BeforeComponents.Num()));

	// 2. move: declarations leave the Blueprint (their nodes stay and are retargeted below)
	int32 Moved = 0;
	for (int32 Index = Blueprint->NewVariables.Num() - 1; Index >= 0; --Index)
	{
		const FString Name = Blueprint->NewVariables[Index].VarName.ToString();
		if (Map.Variables.Contains(Name) || Map.Dispatchers.Contains(Name)) { Blueprint->NewVariables.RemoveAt(Index); ++Moved; }
	}
	// Components: the SCS nodes are renamed out of the way first and removed only after the class derives from the
	// generated one. Removing them earlier makes the SCS validate its root against the old class, which has no native
	// root, and put DefaultSceneRoot back (a name the generated class now owns).
	TMap<USCS_Node*, FString> Reattach;   // kept child -> native parent component name
	TArray<USCS_Node*> RemoveScs;
	if (Scs)
	{
		for (USCS_Node* Node : Scs->GetAllNodes())
		{
			if (Node && Map.Components.Contains(Node->GetVariableName().ToString())) { RemoveScs.Add(Node); }
		}
		for (USCS_Node* Node : RemoveScs)
		{
			for (USCS_Node* Child : Node->GetChildNodes())
			{
				if (!RemoveScs.Contains(Child)) { Reattach.Add(Child, Map.Components[Node->GetVariableName().ToString()]); }
			}
		}
		for (USCS_Node* Node : RemoveScs) { Node->SetVariableName(FName(*(Node->GetVariableName().ToString() + TEXT("_DwoMoved"))), true); }
	}
	TArray<UEdGraph*> RemoveGraphs;
	for (UEdGraph* Graph : Blueprint->FunctionGraphs)
	{
		if (Graph && Map.Functions.Contains(Graph->GetName())) { RemoveGraphs.Add(Graph); }
	}
	for (UEdGraph* Graph : Blueprint->DelegateSignatureGraphs)
	{
		if (Graph && Map.Dispatchers.Contains(Graph->GetName())) { RemoveGraphs.Add(Graph); }
	}
	for (UEdGraph* Graph : RemoveGraphs) { FBlueprintEditorUtils::RemoveGraph(Blueprint, Graph, EGraphRemoveFlags::None); ++Moved; }
	TArray<FString> RemoveProblems;
	TSet<FString> ThunkRoots;
	for (const TPair<FString, FString>& Pair : Map.EventThunks) { ThunkRoots.Add(Pair.Key); }
	const int32 EventNodes = RemoveNodes(Blueprint, Map.EventNodes, Blueprint->UbergraphPages, RemoveProblems, ThunkRoots);
	for (UEdGraph* Graph : Blueprint->FunctionGraphs)
	{
		if (const TSet<FString>* Nodes = Graph ? Map.ThunkNodes.Find(Graph->GetName()) : nullptr)
		{
			TArray<UEdGraph*> One = { Graph };
			RemoveNodes(Blueprint, *Nodes, One, RemoveProblems, TSet<FString>(), true);
		}
	}
	int32 ConstructionNodes = 0;
	if (Map.bConstruction)
	{
		TArray<UEdGraph*> Ucs;
		for (UEdGraph* Graph : Blueprint->FunctionGraphs) { if (Graph && Graph->GetFName() == UEdGraphSchema_K2::FN_UserConstructionScript) { Ucs.Add(Graph); } }
		ConstructionNodes = RemoveNodes(Blueprint, Map.ConstructionNodes, Ucs, RemoveProblems);
	}
	for (const FString& Text : RemoveProblems) { Problem(Text); }
	// no migrated event may stay: it would run in Blueprint and again in C++
	for (UEdGraph* Graph : Blueprint->UbergraphPages)
	{
		for (const UEdGraphNode* Node : Graph->Nodes)
		{
			if (Node && Cast<UK2Node_Event>(Node) && Map.EventRoots.Contains(Node->NodeGuid.ToString(EGuidFormats::Digits)))
			{
				Problem(TEXT("event still in the Blueprint after the move (it would run twice): ") + Node->GetNodeTitle(ENodeTitleType::ListView).ToString());
			}
		}
	}
	Step(FString::Printf(TEXT("moved to C++: %d declarations and graphs, %d event nodes, %d construction script nodes"), Moved, EventNodes, ConstructionNodes));

	// 3. reparent, compile once so the class has the native components, then drop the old component nodes
	Blueprint->ParentClass = NewParent;
	Retarget(Blueprint, Map);
	FBlueprintEditorUtils::RefreshAllNodes(Blueprint);
	FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
	{
		FCompilerResultsLog First;
		FKismetEditorUtilities::CompileBlueprint(Blueprint, EBlueprintCompileOptions::SkipGarbageCollection, &First);
	}
	for (USCS_Node* Node : RemoveScs) { Scs->RemoveNodeAndPromoteChildren(Node); ++Moved; }
	TArray<FString> ThunkProblems;
	for (UEdGraph* Graph : Blueprint->UbergraphPages)
	{
		for (UEdGraphNode* Node : TArray<UEdGraphNode*>(Graph->Nodes))
		{
			const FString* Cpp = Node ? Map.EventThunks.Find(Id(Node)) : nullptr;
			if (!Cpp) { continue; }
			UFunction* Function = NewParent->FindFunctionByName(FName(**Cpp));
			if (!Function) { ThunkProblems.Add(TEXT("generated function not found: ") + *Cpp); continue; }
			AddThunkCall(Graph, Node, Node->FindPin(UEdGraphSchema_K2::PN_Then, EGPD_Output), Function, ThunkProblems);
		}
	}
	for (UEdGraph* Graph : Blueprint->FunctionGraphs)
	{
		const FString* Cpp = Graph ? Map.FunctionThunks.Find(Graph->GetName()) : nullptr;
		if (!Cpp) { continue; }
		UFunction* Function = NewParent->FindFunctionByName(FName(**Cpp));
		UK2Node_FunctionEntry* Entry = nullptr;
		UK2Node_FunctionResult* Result = nullptr;
		for (UEdGraphNode* Node : Graph->Nodes)
		{
			if (!Entry) { Entry = Cast<UK2Node_FunctionEntry>(Node); }
			if (!Result) { Result = Cast<UK2Node_FunctionResult>(Node); }
		}
		if (!Function || !Entry) { ThunkProblems.Add(TEXT("cannot write the call in ") + Graph->GetName()); continue; }
		UK2Node_CallFunction* Call = AddThunkCall(Graph, Entry, Entry->FindPin(UEdGraphSchema_K2::PN_Then, EGPD_Output), Function, ThunkProblems);
		if (Result)
		{
			const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();
			Schema->TryCreateConnection(Call->GetThenPin(), Result->GetExecPin());
			for (UEdGraphPin* Pin : Call->Pins)
			{
				if (Pin->Direction != EGPD_Output || Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec) { continue; }
				UEdGraphPin* In = Result->FindPin(Pin->PinName, EGPD_Input);
				if (!In || !Schema->TryCreateConnection(Pin, In)) { ThunkProblems.Add(FString::Printf(TEXT("could not return %s from %s"), *Pin->PinName.ToString(), *Graph->GetName())); }
			}
		}
	}
	for (const FString& Text : ThunkProblems) { Problem(Text); }
	if (Map.EventThunks.Num() + Map.FunctionThunks.Num() > 0)
	{
		Step(FString::Printf(TEXT("implementable events now call C++: %d event(s), %d function(s)"), Map.EventThunks.Num(), Map.FunctionThunks.Num()));
	}
	UObject* NewDefaults = NewParent->GetDefaultObject();
	for (const TPair<USCS_Node*, FString>& Pair : Reattach)
	{
		if (USceneComponent* Native = Cast<USceneComponent>(NewDefaults->GetDefaultSubobjectByName(FName(*Pair.Value))))
		{
			Pair.Key->SetParent(Native);
		}
		else { Problem(TEXT("native component not found for reattach: ") + Pair.Value); }
	}
	if (Scs) { Scs->ValidateSceneRootNodes(); }
	FBlueprintEditorUtils::RefreshAllNodes(Blueprint);
	FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
	FCompilerResultsLog Results;
	FKismetEditorUtilities::CompileBlueprint(Blueprint, EBlueprintCompileOptions::SkipGarbageCollection, &Results);
	Step(FString::Printf(TEXT("compiled: %d error(s), %d warning(s)"), Results.NumErrors, Results.NumWarnings));
	for (const TSharedRef<FTokenizedMessage>& Message : Results.Messages)
	{
		if (Message->GetSeverity() == EMessageSeverity::Error) { Problem(TEXT("compile: ") + Message->ToText().ToString()); }
	}
	if (Results.NumErrors > 0 || Blueprint->Status == BS_Error) { return Finish(false, false); }

	// 4. restore
	UObject* Defaults = Blueprint->GeneratedClass->GetDefaultObject();
	// Recompiling copies old class-default values onto properties with the same name. The Blueprint's component
	// variables were None there (it fills them at construction), so the C++ members that took their names lost their
	// components: point them back at the native subobjects, or every spawned actor gets null members.
	for (const TPair<FString, FString>& Pair : Map.Components)
	{
		FObjectPropertyBase* Pointer = FindFProperty<FObjectPropertyBase>(Blueprint->GeneratedClass, *Pair.Value);
		UObject* Native = Defaults->GetDefaultSubobjectByName(FName(*Pair.Value));
		if (Pointer && Native) { Pointer->SetObjectPropertyValue_InContainer(Defaults, Native); }
	}
	TMap<FString, FString> Renames;
	for (const TPair<FString, FString>& Pair : Map.Variables) { if (Pair.Key != Pair.Value) { Renames.Add(Pair.Key, Pair.Value); } }
	FProps DefaultsToRestore = BeforeDefaults;
	for (const TPair<FString, FString>& Pair : Map.Components) { DefaultsToRestore.Remove(Pair.Key); }
	for (const FString& Name : Restore(Defaults, DefaultsToRestore, Renames)) { Problem(TEXT("could not restore ") + Name); }
	for (const TPair<FString, FProps>& Pair : BeforeComponents)
	{
		UObject* Native = Defaults->GetDefaultSubobjectByName(FName(*Map.Components[Pair.Key]));
		if (!Native) { Problem(TEXT("native component missing after rebase: ") + Pair.Key); continue; }
		for (const FString& Name : Restore(Native, Pair.Value, {})) { Problem(FString::Printf(TEXT("could not restore %s.%s"), *Pair.Key, *Name)); }
	}
	for (const TPair<FString, FString>& Literal : Map.Literals)
	{
		FObjectPropertyBase* Property = FindFProperty<FObjectPropertyBase>(Blueprint->GeneratedClass, *Literal.Key);
		UObject* Asset = LoadObject<UObject>(nullptr, *Literal.Value);
		if (Property && Asset) { Property->SetObjectPropertyValue_InContainer(Defaults, Asset); }
		else { Problem(TEXT("asset reference not set: ") + Literal.Key + TEXT(" = ") + Literal.Value); }
	}
	Defaults->MarkPackageDirty();
	Step(TEXT("restored class default and component values"));

	// 5. verify against the snapshot
	const FProps AfterDefaults = Snapshot(Defaults);
	for (const TPair<FString, FString>& Pair : BeforeDefaults)
	{
		if (Map.Components.Contains(Pair.Key)) { continue; }   // component pointers: checked below, not as values
		const FString* Renamed = Renames.Find(Pair.Key);
		const FString* After = AfterDefaults.Find(Renamed ? *Renamed : Pair.Key);
		if (!After)
		{
			// None before, now a native component of this class (RootComponent, a moved component): the expected
			// result of moving components to C++, not a changed value
			const FObjectPropertyBase* Pointer = FindFProperty<FObjectPropertyBase>(Blueprint->GeneratedClass, *(Renamed ? *Renamed : Pair.Key));
			const UObject* Now = Pointer ? Pointer->GetObjectPropertyValue_InContainer(Defaults) : nullptr;
			if (Pair.Value == TEXT("None") && Now && Now->IsIn(Defaults)) { continue; }
			Diffs.Add(MakeShared<FJsonValueObject>(Diff(TEXT("defaults"), Pair.Key, Pair.Value, TEXT("(missing)"))));
		}
		else if (*After != Pair.Value) { Diffs.Add(MakeShared<FJsonValueObject>(Diff(TEXT("defaults"), Pair.Key, Pair.Value, *After))); }
	}
	for (const TPair<FString, FProps>& Pair : BeforeComponents)
	{
		const FProps After = Snapshot(Defaults->GetDefaultSubobjectByName(FName(*Map.Components[Pair.Key])));
		for (const TPair<FString, FString>& Value : Pair.Value)
		{
			const FString* Now = After.Find(Value.Key);
			// the attachment parent is expected to change from an SCS template to the native component
			if (Value.Key == TEXT("AttachParent")) { continue; }
			if (!Now || *Now != Value.Value)
			{
				Diffs.Add(MakeShared<FJsonValueObject>(Diff(*(TEXT("component ") + Pair.Key), Value.Key, Value.Value, Now ? *Now : FString(TEXT("(missing)")))));
			}
		}
	}
	// every moved component must still be reachable through its C++ member on the class default
	for (const TPair<FString, FString>& Pair : Map.Components)
	{
		const FObjectPropertyBase* Pointer = FindFProperty<FObjectPropertyBase>(Blueprint->GeneratedClass, *Pair.Value);
		const UObject* Target = Pointer ? Pointer->GetObjectPropertyValue_InContainer(Defaults) : nullptr;
		if (!Target || !Target->IsIn(Defaults))
		{
			Problem(FString::Printf(TEXT("the C++ member %s does not point to its component after the rebase"), *Pair.Value));
		}
	}
	Step(FString::Printf(TEXT("verified: %d value difference(s)"), Diffs.Num()));
	const bool bOk = Problems.Num() == 0 && (Diffs.Num() == 0 || bForce);
	if (!bOk || bDryRun) { return Finish(bOk, false); }

	// 6. save
	UPackage* Package = Blueprint->GetOutermost();
	const FString File = FPackageName::LongPackageNameToFilename(Package->GetName(), FPackageName::GetAssetPackageExtension());
	FSavePackageArgs Args;
	Args.TopLevelFlags = RF_Public | RF_Standalone;
	const bool bSaved = UPackage::SavePackage(Package, Blueprint, *File, Args);
	Step(bSaved ? TEXT("saved ") + File : TEXT("save failed: ") + File);
	return Finish(bSaved, bSaved);
}
