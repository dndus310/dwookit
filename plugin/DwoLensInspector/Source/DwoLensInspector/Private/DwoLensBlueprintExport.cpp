#include "DwoLensBlueprintExport.h"

#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "Blueprint/WidgetTree.h"
#include "Components/ActorComponent.h"
#include "Components/PanelWidget.h"
#include "Components/Widget.h"
#include "Dom/JsonObject.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphNode_Comment.h"
#include "EdGraphSchema_K2.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/SCS_Node.h"
#include "Engine/SimpleConstructionScript.h"
#include "Engine/TimelineTemplate.h"
#include "HAL/FileManager.h"
#include "K2Node.h"
#include "K2Node_BaseMCDelegate.h"
#include "K2Node_BreakStruct.h"
#include "K2Node_CallFunction.h"
#include "K2Node_ComponentBoundEvent.h"
#include "K2Node_CreateDelegate.h"
#include "K2Node_CustomEvent.h"
#include "K2Node_DynamicCast.h"
#include "K2Node_Event.h"
#include "K2Node_ExecutionSequence.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "K2Node_IfThenElse.h"
#include "K2Node_Knot.h"
#include "K2Node_MacroInstance.h"
#include "K2Node_MakeStruct.h"
#include "K2Node_Self.h"
#include "K2Node_Switch.h"
#include "K2Node_Timeline.h"
#include "K2Node_Tunnel.h"
#include "K2Node_Variable.h"
#include "K2Node_VariableSet.h"
#include "Misc/DateTime.h"
#include "Misc/EngineVersion.h"
#include "Misc/FileHelper.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "WidgetBlueprint.h"
#include "Animation/WidgetAnimation.h"

DEFINE_LOG_CATEGORY_STATIC(LogDwoLensBp, Log, All);

// Own namespace: the module may be built as one unity file together with the commandlet's helpers.
namespace DwoLensInspector::BpExport
{
	static const TCHAR* Schema = TEXT("lens.bp/1");
	static const int32 MaxValueLength = 2000;

	using FJsonRef = TSharedRef<FJsonObject>;
	using FJsonValues = TArray<TSharedPtr<FJsonValue>>;

	static void PushString(FJsonValues& Array, const FString& Value) { Array.Add(MakeShared<FJsonValueString>(Value)); }
	static void PushObject(FJsonValues& Array, const FJsonRef& Value) { Array.Add(MakeShared<FJsonValueObject>(Value)); }
	static FString PathOf(const UObject* Object) { return Object ? Object->GetPathName() : FString(); }

	/** Sets the field only when the value says something, so the file stays readable. */
	static void SetIf(const FJsonRef& Out, const TCHAR* Field, const FString& Value)
	{
		if (!Value.IsEmpty()) { Out->SetStringField(Field, Value); }
	}

	static FString Clip(FString Value)
	{
		if (Value.Len() > MaxValueLength) { Value = Value.Left(MaxValueLength) + TEXT("...(clipped)"); }
		return Value;
	}

	/** Display name of a type object: Actor, BP_Player (no _C), FVector -> Vector, EMyEnum. */
	static FString ShortName(const UObject* Object)
	{
		if (!Object) { return FString(); }
		FString Name = Object->GetName();
		if (Cast<UBlueprintGeneratedClass>(Object)) { Name.RemoveFromEnd(TEXT("_C")); }
		return Name;
	}

	static FString TerminalType(const FName Category, const FName SubCategory, const UObject* SubObject)
	{
		const FString Sub = ShortName(SubObject);
		if (Category == UEdGraphSchema_K2::PC_Boolean) { return TEXT("bool"); }
		if (Category == UEdGraphSchema_K2::PC_Byte) { return Sub.IsEmpty() ? TEXT("byte") : Sub; }
		if (Category == UEdGraphSchema_K2::PC_Int) { return TEXT("int"); }
		if (Category == UEdGraphSchema_K2::PC_Int64) { return TEXT("int64"); }
		if (Category == UEdGraphSchema_K2::PC_Real || Category == UEdGraphSchema_K2::PC_Float || Category == UEdGraphSchema_K2::PC_Double) { return TEXT("float"); }
		if (Category == UEdGraphSchema_K2::PC_Name) { return TEXT("name"); }
		if (Category == UEdGraphSchema_K2::PC_String) { return TEXT("string"); }
		if (Category == UEdGraphSchema_K2::PC_Text) { return TEXT("text"); }
		if (Category == UEdGraphSchema_K2::PC_Exec) { return TEXT("exec"); }
		if (Category == UEdGraphSchema_K2::PC_Wildcard) { return TEXT("any"); }
		if (Category == UEdGraphSchema_K2::PC_Enum) { return Sub.IsEmpty() ? TEXT("enum") : Sub; }
		if (Category == UEdGraphSchema_K2::PC_Struct) { return Sub.IsEmpty() ? TEXT("struct") : Sub; }
		if (Category == UEdGraphSchema_K2::PC_Delegate) { return TEXT("delegate"); }
		if (Category == UEdGraphSchema_K2::PC_MCDelegate) { return TEXT("dispatcher"); }
		if (Category == UEdGraphSchema_K2::PC_FieldPath) { return TEXT("field"); }
		const FString Target = SubCategory == UEdGraphSchema_K2::PSC_Self ? FString(TEXT("Self")) : (Sub.IsEmpty() ? FString(TEXT("Object")) : Sub);
		if (Category == UEdGraphSchema_K2::PC_Object || Category == UEdGraphSchema_K2::PC_Interface) { return Target; }
		if (Category == UEdGraphSchema_K2::PC_Class) { return FString::Printf(TEXT("Class<%s>"), *Target); }
		if (Category == UEdGraphSchema_K2::PC_SoftObject) { return FString::Printf(TEXT("Soft<%s>"), *Target); }
		if (Category == UEdGraphSchema_K2::PC_SoftClass) { return FString::Printf(TEXT("SoftClass<%s>"), *Target); }
		return Category.ToString();
	}

	/** Compact type text: float, Vector, Array<Actor>, Map<name,int>, Class<Pawn>, a trailing & for by-reference. */
	static FString TypeText(const FEdGraphPinType& Type)
	{
		FString Text = TerminalType(Type.PinCategory, Type.PinSubCategory, Type.PinSubCategoryObject.Get());
		switch (Type.ContainerType)
		{
		case EPinContainerType::Array: Text = FString::Printf(TEXT("Array<%s>"), *Text); break;
		case EPinContainerType::Set: Text = FString::Printf(TEXT("Set<%s>"), *Text); break;
		case EPinContainerType::Map:
			Text = FString::Printf(TEXT("Map<%s,%s>"), *Text, *TerminalType(Type.PinValueType.TerminalCategory,
				Type.PinValueType.TerminalSubCategory, Type.PinValueType.TerminalSubCategoryObject.Get()));
			break;
		default: break;
		}
		if (Type.bIsReference) { Text += TEXT("&"); }
		return Text;
	}

	static FJsonRef DescribeType(const FEdGraphPinType& Type)
	{
		FJsonRef Out = MakeShared<FJsonObject>();
		Out->SetStringField(TEXT("text"), TypeText(Type));
		Out->SetStringField(TEXT("category"), Type.PinCategory.ToString());
		if (!Type.PinSubCategory.IsNone()) { Out->SetStringField(TEXT("subCategory"), Type.PinSubCategory.ToString()); }
		SetIf(Out, TEXT("object"), PathOf(Type.PinSubCategoryObject.Get()));
		if (Type.ContainerType == EPinContainerType::Array) { Out->SetStringField(TEXT("container"), TEXT("array")); }
		if (Type.ContainerType == EPinContainerType::Set) { Out->SetStringField(TEXT("container"), TEXT("set")); }
		if (Type.ContainerType == EPinContainerType::Map)
		{
			Out->SetStringField(TEXT("container"), TEXT("map"));
			Out->SetStringField(TEXT("valueCategory"), Type.PinValueType.TerminalCategory.ToString());
			SetIf(Out, TEXT("valueObject"), PathOf(Type.PinValueType.TerminalSubCategoryObject.Get()));
		}
		if (Type.bIsReference) { Out->SetBoolField(TEXT("ref"), true); }
		if (Type.bIsConst) { Out->SetBoolField(TEXT("const"), true); }
		return Out;
	}

	static FString NodeId(const UEdGraphNode* Node) { return Node->NodeGuid.ToString(EGuidFormats::Digits); }

	static FJsonRef DescribePin(const UEdGraphPin* Pin)
	{
		FJsonRef Out = MakeShared<FJsonObject>();
		Out->SetStringField(TEXT("name"), Pin->PinName.ToString());
		const FString Friendly = Pin->PinFriendlyName.ToString();
		if (!Friendly.IsEmpty() && Friendly != Pin->PinName.ToString()) { Out->SetStringField(TEXT("label"), Friendly); }
		Out->SetStringField(TEXT("dir"), Pin->Direction == EGPD_Input ? TEXT("in") : TEXT("out"));
		Out->SetObjectField(TEXT("type"), DescribeType(Pin->PinType));
		SetIf(Out, TEXT("default"), Clip(Pin->DefaultValue));
		SetIf(Out, TEXT("defaultObject"), PathOf(Pin->DefaultObject));
		if (!Pin->DefaultTextValue.IsEmpty()) { Out->SetStringField(TEXT("defaultText"), Clip(Pin->DefaultTextValue.ToString())); }
		// The value the node had when placed: a default equal to it was not set by the author.
		SetIf(Out, TEXT("autoDefault"), Clip(Pin->AutogeneratedDefaultValue));
		if (Pin->bHidden) { Out->SetBoolField(TEXT("hidden"), true); }
		if (Pin->bAdvancedView) { Out->SetBoolField(TEXT("advanced"), true); }
		if (Pin->ParentPin) { Out->SetStringField(TEXT("splitFrom"), Pin->ParentPin->PinName.ToString()); }
		if (Pin->SubPins.Num() > 0) { Out->SetBoolField(TEXT("split"), true); }

		FJsonValues Links;
		for (const UEdGraphPin* Linked : Pin->LinkedTo)
		{
			const UEdGraphNode* Other = Linked ? Linked->GetOwningNodeUnchecked() : nullptr;
			if (!Other) { continue; }
			FJsonRef Link = MakeShared<FJsonObject>();
			Link->SetStringField(TEXT("node"), NodeId(Other));
			Link->SetStringField(TEXT("pin"), Linked->PinName.ToString());
			PushObject(Links, Link);
		}
		if (Links.Num() > 0) { Out->SetArrayField(TEXT("links"), Links); }
		return Out;
	}

	static FJsonValues FunctionFlagNames(uint32 Flags)
	{
		struct FFlagName { EFunctionFlags Flag; const TCHAR* Name; };
		static const FFlagName Names[] = {
			{ FUNC_Native, TEXT("Native") }, { FUNC_Event, TEXT("Event") }, { FUNC_BlueprintCallable, TEXT("BlueprintCallable") },
			{ FUNC_BlueprintEvent, TEXT("BlueprintEvent") }, { FUNC_BlueprintPure, TEXT("Pure") }, { FUNC_Const, TEXT("Const") },
			{ FUNC_Static, TEXT("Static") }, { FUNC_Net, TEXT("Net") }, { FUNC_NetReliable, TEXT("Reliable") },
			{ FUNC_NetServer, TEXT("Server") }, { FUNC_NetClient, TEXT("Client") }, { FUNC_NetMulticast, TEXT("Multicast") },
			{ FUNC_Protected, TEXT("Protected") }, { FUNC_Private, TEXT("Private") }, { FUNC_BlueprintAuthorityOnly, TEXT("AuthorityOnly") },
		};
		FJsonValues Out;
		for (const FFlagName& Entry : Names)
		{
			if (Flags & Entry.Flag) { PushString(Out, Entry.Name); }
		}
		return Out;
	}

	/**
	 * role: the coarse role the analyzer uses (call, event, delegate, variable, macro, cast, other).
	 * kind: the exact construct, for pseudocode (branch, sequence, knot, set, entry, result, comment, ...).
	 */
	static void DescribeNodeKind(const UEdGraphNode* Node, const FJsonRef& Out)
	{
		FString Role = TEXT("other");
		FString Kind = TEXT("node");
		if (const UEdGraphNode_Comment* Comment = Cast<UEdGraphNode_Comment>(Node))
		{
			Kind = TEXT("comment");
			FJsonValues Size;
			Size.Add(MakeShared<FJsonValueNumber>(Comment->NodeWidth));
			Size.Add(MakeShared<FJsonValueNumber>(Comment->NodeHeight));
			Out->SetArrayField(TEXT("size"), Size);
		}
		else if (const UK2Node_CallFunction* Call = Cast<UK2Node_CallFunction>(Node))
		{
			Role = TEXT("call");
			Kind = TEXT("call");
			if (const UFunction* Target = Call->GetTargetFunction())
			{
				Out->SetStringField(TEXT("member"), Target->GetName());
				Out->SetStringField(TEXT("memberOwner"), PathOf(Target->GetOwnerClass()));
				Out->SetArrayField(TEXT("memberFlags"), FunctionFlagNames(Target->FunctionFlags));
			}
			else
			{
				Out->SetStringField(TEXT("member"), Call->FunctionReference.GetMemberName().ToString());
			}
			if (Call->IsLatentFunction()) { Out->SetBoolField(TEXT("latent"), true); }
		}
		else if (const UK2Node_ComponentBoundEvent* Bound = Cast<UK2Node_ComponentBoundEvent>(Node))
		{
			Role = TEXT("event");
			Kind = TEXT("boundEvent");
			Out->SetStringField(TEXT("member"), Bound->DelegatePropertyName.ToString());
			Out->SetStringField(TEXT("component"), Bound->ComponentPropertyName.ToString());
		}
		else if (const UK2Node_CustomEvent* Custom = Cast<UK2Node_CustomEvent>(Node))
		{
			Role = TEXT("event");
			Kind = TEXT("customEvent");
			Out->SetStringField(TEXT("member"), Custom->CustomFunctionName.ToString());
			Out->SetArrayField(TEXT("memberFlags"), FunctionFlagNames(Custom->FunctionFlags));
		}
		else if (const UK2Node_Event* Event = Cast<UK2Node_Event>(Node))
		{
			Role = TEXT("event");
			Kind = TEXT("event");
			Out->SetStringField(TEXT("member"), Event->GetFunctionName().ToString());
			Out->SetStringField(TEXT("memberOwner"), PathOf(Event->EventReference.GetMemberParentClass()));
			if (Event->bOverrideFunction) { Out->SetBoolField(TEXT("override"), true); }
		}
		else if (const UK2Node_FunctionEntry* Entry = Cast<UK2Node_FunctionEntry>(Node))
		{
			Kind = TEXT("entry");
			Out->SetStringField(TEXT("member"), Entry->FunctionReference.GetMemberName().ToString());
		}
		else if (Cast<UK2Node_FunctionResult>(Node))
		{
			Kind = TEXT("result");
		}
		else if (const UK2Node_BaseMCDelegate* Delegate = Cast<UK2Node_BaseMCDelegate>(Node))
		{
			Role = TEXT("delegate");
			Kind = Node->GetClass()->GetName().Replace(TEXT("K2Node_"), TEXT(""));   // AddDelegate, CallDelegate, RemoveDelegate, ClearDelegate
			Kind[0] = FChar::ToLower(Kind[0]);
			Out->SetStringField(TEXT("member"), Delegate->GetPropertyName().ToString());
			Out->SetStringField(TEXT("memberOwner"), PathOf(Delegate->DelegateReference.GetMemberParentClass(Delegate->GetBlueprintClassFromNode())));
		}
		else if (const UK2Node_CreateDelegate* Create = Cast<UK2Node_CreateDelegate>(Node))
		{
			Kind = TEXT("createDelegate");
			Out->SetStringField(TEXT("member"), Create->GetFunctionName().ToString());
		}
		else if (const UK2Node_Variable* Variable = Cast<UK2Node_Variable>(Node))
		{
			Role = TEXT("variable");
			Kind = Cast<UK2Node_VariableSet>(Node) ? TEXT("set") : TEXT("get");
			Out->SetStringField(TEXT("member"), Variable->GetVarNameString());
			Out->SetStringField(TEXT("memberOwner"), PathOf(Variable->GetVariableSourceClass()));
		}
		else if (const UK2Node_MacroInstance* Macro = Cast<UK2Node_MacroInstance>(Node))
		{
			Role = TEXT("macro");
			Kind = TEXT("macro");
			const UEdGraph* MacroGraph = Macro->GetMacroGraph();
			Out->SetStringField(TEXT("member"), MacroGraph ? MacroGraph->GetName() : FString());
			Out->SetStringField(TEXT("memberOwner"), MacroGraph ? PathOf(MacroGraph->GetOutermostObject()) : FString());
		}
		else if (const UK2Node_DynamicCast* CastNode = Cast<UK2Node_DynamicCast>(Node))
		{
			Role = TEXT("cast");
			Kind = TEXT("cast");
			Out->SetStringField(TEXT("member"), PathOf(CastNode->TargetType.Get()));
		}
		else if (Cast<UK2Node_IfThenElse>(Node)) { Kind = TEXT("branch"); }
		else if (Cast<UK2Node_ExecutionSequence>(Node)) { Kind = TEXT("sequence"); }
		else if (Cast<UK2Node_Knot>(Node)) { Kind = TEXT("knot"); }
		else if (Cast<UK2Node_Self>(Node)) { Kind = TEXT("self"); }
		else if (Cast<UK2Node_Switch>(Node)) { Kind = TEXT("switch"); }
		else if (const UK2Node_Timeline* Timeline = Cast<UK2Node_Timeline>(Node))
		{
			Kind = TEXT("timeline");
			Out->SetStringField(TEXT("member"), Timeline->TimelineName.ToString());
		}
		else if (const UK2Node_MakeStruct* Make = Cast<UK2Node_MakeStruct>(Node))
		{
			Kind = TEXT("make");
			Out->SetStringField(TEXT("member"), PathOf(Make->StructType));
		}
		else if (const UK2Node_BreakStruct* Break = Cast<UK2Node_BreakStruct>(Node))
		{
			Kind = TEXT("break");
			Out->SetStringField(TEXT("member"), PathOf(Break->StructType));
		}
		else if (Cast<UK2Node_Tunnel>(Node)) { Kind = TEXT("tunnel"); }

		Out->SetStringField(TEXT("role"), Role);
		Out->SetStringField(TEXT("kind"), Kind);
		if (const UK2Node* K2 = Cast<UK2Node>(Node))
		{
			if (K2->IsNodePure()) { Out->SetBoolField(TEXT("pure"), true); }
		}
		// Property Access (anim graphs): the path it reads, through reflection so no editor plugin module is needed.
		// Titles are localized in a non-English editor, so the path is the only readable name of the node.
		if (Node->GetClass()->GetName() == TEXT("K2Node_PropertyAccess"))
		{
			if (const FArrayProperty* PathProperty = FindFProperty<FArrayProperty>(Node->GetClass(), TEXT("Path")))
			{
				if (CastField<FStrProperty>(PathProperty->Inner))
				{
					FScriptArrayHelper Array(PathProperty, PathProperty->ContainerPtrToValuePtr<void>(Node));
					FJsonValues Segments;
					for (int32 Index = 0; Index < Array.Num(); ++Index)
					{
						Segments.Add(MakeShared<FJsonValueString>(*reinterpret_cast<const FString*>(Array.GetRawPtr(Index))));
					}
					if (Segments.Num() > 0) { Out->SetArrayField(TEXT("path"), Segments); }
				}
			}
		}
	}

	static FJsonRef DescribeVariable(const FBPVariableDescription& Variable, const UClass* GeneratedClass)
	{
		FJsonRef Out = MakeShared<FJsonObject>();
		Out->SetStringField(TEXT("name"), Variable.VarName.ToString());
		Out->SetObjectField(TEXT("type"), DescribeType(Variable.VarType));
		SetIf(Out, TEXT("category"), Variable.Category.ToString() == TEXT("Default") ? FString() : Variable.Category.ToString());

		// The compiled class default is the real start value; the description's text is the fallback.
		FString Value = Variable.DefaultValue;
		if (GeneratedClass)
		{
			if (const FProperty* Property = FindFProperty<FProperty>(GeneratedClass, Variable.VarName))
			{
				if (const UObject* Defaults = GeneratedClass->GetDefaultObject(false))
				{
					Value.Reset();
					Property->ExportTextItem_Direct(Value, Property->ContainerPtrToValuePtr<void>(Defaults), nullptr, nullptr, PPF_None);
				}
			}
		}
		SetIf(Out, TEXT("default"), Clip(Value));

		FJsonValues Flags;
		const uint64 F = Variable.PropertyFlags;
		if ((F & CPF_Edit) && !(F & CPF_DisableEditOnInstance)) { PushString(Flags, TEXT("InstanceEditable")); }
		if (F & CPF_BlueprintReadOnly) { PushString(Flags, TEXT("ReadOnly")); }
		if (F & CPF_Net) { PushString(Flags, TEXT("Replicated")); }
		if (F & CPF_Transient) { PushString(Flags, TEXT("Transient")); }
		if (F & CPF_SaveGame) { PushString(Flags, TEXT("SaveGame")); }
		if (F & CPF_Config) { PushString(Flags, TEXT("Config")); }
		FString Tooltip;
		for (const FBPVariableMetaDataEntry& Meta : Variable.MetaDataArray)
		{
			if (Meta.DataKey == TEXT("ExposeOnSpawn") && Meta.DataValue == TEXT("true")) { PushString(Flags, TEXT("ExposeOnSpawn")); }
			if (Meta.DataKey == TEXT("BlueprintPrivate") && Meta.DataValue == TEXT("true")) { PushString(Flags, TEXT("Private")); }
			if (Meta.DataKey == TEXT("tooltip")) { Tooltip = Meta.DataValue; }
		}
		if (Flags.Num() > 0) { Out->SetArrayField(TEXT("flags"), Flags); }
		if (!Variable.RepNotifyFunc.IsNone()) { Out->SetStringField(TEXT("repNotify"), Variable.RepNotifyFunc.ToString()); }
		SetIf(Out, TEXT("tooltip"), Tooltip);
		return Out;
	}

	/** Editable properties of Object whose value differs from Base (same class or a parent class of it). */
	static FJsonValues DiffProperties(const UObject* Object, const UObject* Base, const UClass* SkipOwner)
	{
		FJsonValues Out;
		if (!Object || !Base)
		{
			return Out;
		}
		for (TFieldIterator<FProperty> It(Object->GetClass()); It; ++It)
		{
			const FProperty* Property = *It;
			if (!Property->HasAnyPropertyFlags(CPF_Edit) || Property->HasAnyPropertyFlags(CPF_Transient | CPF_Deprecated))
			{
				continue;
			}
			const UClass* Owner = Property->GetOwnerClass();
			if (!Owner || Owner == SkipOwner || !Base->IsA(Owner))
			{
				continue;
			}
			const void* Value = Property->ContainerPtrToValuePtr<void>(Object);
			const void* BaseValue = Property->ContainerPtrToValuePtr<void>(Base);
			if (Property->Identical(Value, BaseValue, PPF_None))
			{
				continue;
			}
			// Subobject pointers (components) always differ between two class defaults; they are described elsewhere.
			if (const FObjectPropertyBase* ObjectProperty = CastField<FObjectPropertyBase>(Property))
			{
				const UObject* Target = ObjectProperty->GetObjectPropertyValue(Value);
				if (Target && (Target->IsIn(Object) || Target->IsIn(Base)))
				{
					continue;
				}
			}
			FString Text;
			Property->ExportTextItem_Direct(Text, Value, BaseValue, const_cast<UObject*>(Object), PPF_None);
			FJsonRef Entry = MakeShared<FJsonObject>();
			Entry->SetStringField(TEXT("name"), Property->GetName());
			Entry->SetStringField(TEXT("value"), Clip(Text));
			Entry->SetStringField(TEXT("owner"), ShortName(Owner));
			PushObject(Out, Entry);
		}
		return Out;
	}

	static FJsonRef DescribeGraph(const UBlueprint* Blueprint, const UEdGraph* Graph)
	{
		FJsonRef Out = MakeShared<FJsonObject>();
		Out->SetStringField(TEXT("name"), Graph->GetName());
		FString Kind = TEXT("sub");
		if (Blueprint->UbergraphPages.Contains(Graph)) { Kind = TEXT("event"); }
		else if (Blueprint->FunctionGraphs.Contains(Graph)) { Kind = TEXT("function"); }
		else if (Blueprint->MacroGraphs.Contains(Graph)) { Kind = TEXT("macro"); }
		else if (Blueprint->DelegateSignatureGraphs.Contains(Graph)) { Kind = TEXT("dispatcher"); }
		else
		{
			for (const FBPInterfaceDescription& Interface : Blueprint->ImplementedInterfaces)
			{
				if (Interface.Graphs.Contains(Graph))
				{
					Kind = TEXT("interface");
					Out->SetStringField(TEXT("interface"), PathOf(Interface.Interface));
				}
			}
		}
		Out->SetStringField(TEXT("kind"), Kind);
		if (const UEdGraph* Parent = Cast<UEdGraph>(Graph->GetOuter())) { Out->SetStringField(TEXT("parentGraph"), Parent->GetName()); }
		// A graph owned by a node (anim state, state machine, transition 'Idle -> Start', collapsed node): the
		// node's title says what the graph is; transition graphs are all named 'Transition'.
		if (const UEdGraphNode* OwnerNode = Cast<UEdGraphNode>(Graph->GetOuter()))
		{
			Out->SetStringField(TEXT("owner"), OwnerNode->GetNodeTitle(ENodeTitleType::ListView).ToString());
			if (const UEdGraph* OwnerGraph = OwnerNode->GetGraph()) { Out->SetStringField(TEXT("parentGraph"), OwnerGraph->GetName()); }
		}

		FJsonValues Nodes;
		for (const UEdGraphNode* Node : Graph->Nodes)
		{
			if (!Node) { continue; }
			FJsonRef Entry = MakeShared<FJsonObject>();
			Entry->SetStringField(TEXT("id"), NodeId(Node));
			Entry->SetStringField(TEXT("class"), Node->GetClass()->GetName());
			Entry->SetStringField(TEXT("title"), Node->GetNodeTitle(ENodeTitleType::FullTitle).ToString());
			DescribeNodeKind(Node, Entry);
			FJsonValues Pos;
			Pos.Add(MakeShared<FJsonValueNumber>(Node->NodePosX));
			Pos.Add(MakeShared<FJsonValueNumber>(Node->NodePosY));
			Entry->SetArrayField(TEXT("pos"), Pos);
			if (!Cast<UEdGraphNode_Comment>(Node)) { SetIf(Entry, TEXT("comment"), Node->NodeComment); }
			if (!Node->IsNodeEnabled()) { Entry->SetBoolField(TEXT("disabled"), true); }

			// Function signature, flags and locals live on the entry node.
			if (const UK2Node_FunctionEntry* FunctionEntry = Cast<UK2Node_FunctionEntry>(Node))
			{
				Entry->SetArrayField(TEXT("functionFlags"), FunctionFlagNames(static_cast<uint32>(FunctionEntry->GetFunctionFlags())));
				SetIf(Entry, TEXT("tooltip"), FunctionEntry->MetaData.ToolTip.ToString());
				SetIf(Entry, TEXT("category"), FunctionEntry->MetaData.Category.ToString() == TEXT("Default") ? FString() : FunctionEntry->MetaData.Category.ToString());
				if (FunctionEntry->MetaData.bCallInEditor) { Entry->SetBoolField(TEXT("callInEditor"), true); }
				FJsonValues Locals;
				for (const FBPVariableDescription& Local : FunctionEntry->LocalVariables)
				{
					FJsonRef L = MakeShared<FJsonObject>();
					L->SetStringField(TEXT("name"), Local.VarName.ToString());
					L->SetObjectField(TEXT("type"), DescribeType(Local.VarType));
					SetIf(L, TEXT("default"), Clip(Local.DefaultValue));
					PushObject(Locals, L);
				}
				if (Locals.Num() > 0) { Entry->SetArrayField(TEXT("locals"), Locals); }
			}

			FJsonValues Pins;
			for (const UEdGraphPin* Pin : Node->Pins)
			{
				if (Pin) { PushObject(Pins, DescribePin(Pin)); }
			}
			Entry->SetArrayField(TEXT("pins"), Pins);
			PushObject(Nodes, Entry);
		}
		Out->SetArrayField(TEXT("nodes"), Nodes);
		return Out;
	}

	static FJsonValues DescribeComponents(const UBlueprint* Blueprint)
	{
		FJsonValues Out;
		const USimpleConstructionScript* Scs = Blueprint->SimpleConstructionScript;
		if (!Scs) { return Out; }
		for (USCS_Node* Node : Scs->GetAllNodes())
		{
			if (!Node) { continue; }
			FJsonRef Entry = MakeShared<FJsonObject>();
			Entry->SetStringField(TEXT("name"), Node->GetVariableName().ToString());
			Entry->SetStringField(TEXT("class"), ShortName(Node->ComponentClass));
			Entry->SetStringField(TEXT("classPath"), PathOf(Node->ComponentClass));
			if (const USCS_Node* Parent = Scs->FindParentNode(Node)) { Entry->SetStringField(TEXT("parent"), Parent->GetVariableName().ToString()); }
			else if (!Node->ParentComponentOrVariableName.IsNone()) { Entry->SetStringField(TEXT("parent"), Node->ParentComponentOrVariableName.ToString()); }
			if (!Node->AttachToName.IsNone()) { Entry->SetStringField(TEXT("socket"), Node->AttachToName.ToString()); }
			if (Scs->GetRootNodes().Contains(Node)) { Entry->SetBoolField(TEXT("root"), true); }
			if (Node->ComponentTemplate && Node->ComponentClass)
			{
				FJsonValues Changed = DiffProperties(Node->ComponentTemplate, Node->ComponentClass->GetDefaultObject(), nullptr);
				if (Changed.Num() > 0) { Entry->SetArrayField(TEXT("changed"), Changed); }
			}
			PushObject(Out, Entry);
		}
		return Out;
	}

	/** Components the Blueprint inherits from a native parent (CharacterMovement, Mesh ...) with values it changed. */
	static FJsonValues DescribeInheritedComponents(const UClass* GeneratedClass)
	{
		FJsonValues Out;
		UObject* Defaults = GeneratedClass ? GeneratedClass->GetDefaultObject(false) : nullptr;
		const UClass* Super = GeneratedClass ? GeneratedClass->GetSuperClass() : nullptr;
		UObject* SuperDefaults = Super ? Super->GetDefaultObject(false) : nullptr;
		if (!Defaults || !SuperDefaults) { return Out; }
		TArray<UObject*> Subobjects;
		Defaults->GetDefaultSubobjects(Subobjects);
		for (const UObject* Sub : Subobjects)
		{
			const UActorComponent* Component = Cast<UActorComponent>(Sub);
			if (!Component) { continue; }
			const UObject* Base = SuperDefaults->GetDefaultSubobjectByName(Component->GetFName());
			if (!Base || Base->GetClass() != Component->GetClass()) { continue; }
			FJsonValues Changed = DiffProperties(Component, Base, nullptr);
			if (Changed.Num() == 0) { continue; }
			FJsonRef Entry = MakeShared<FJsonObject>();
			Entry->SetStringField(TEXT("name"), Component->GetName());
			Entry->SetStringField(TEXT("class"), ShortName(Component->GetClass()));
			Entry->SetArrayField(TEXT("changed"), Changed);
			PushObject(Out, Entry);
		}
		return Out;
	}

	static FJsonValues DescribeTimelines(const UBlueprint* Blueprint)
	{
		FJsonValues Out;
		for (const UTimelineTemplate* Timeline : Blueprint->Timelines)
		{
			if (!Timeline) { continue; }
			FJsonRef Entry = MakeShared<FJsonObject>();
			Entry->SetStringField(TEXT("name"), Timeline->GetVariableName().ToString());
			Entry->SetNumberField(TEXT("length"), Timeline->TimelineLength);
			if (Timeline->bAutoPlay) { Entry->SetBoolField(TEXT("autoPlay"), true); }
			if (Timeline->bLoop) { Entry->SetBoolField(TEXT("loop"), true); }
			FJsonValues Tracks;
			auto AddTracks = [&Tracks](const auto& List, const TCHAR* Type)
			{
				for (const auto& Track : List)
				{
					PushString(Tracks, FString::Printf(TEXT("%s:%s"), Type, *Track.GetTrackName().ToString()));
				}
			};
			AddTracks(Timeline->FloatTracks, TEXT("float"));
			AddTracks(Timeline->VectorTracks, TEXT("vector"));
			AddTracks(Timeline->LinearColorTracks, TEXT("color"));
			AddTracks(Timeline->EventTracks, TEXT("event"));
			Entry->SetArrayField(TEXT("tracks"), Tracks);
			PushObject(Out, Entry);
		}
		return Out;
	}

	static void DescribeWidgetBlueprint(const UWidgetBlueprint* WidgetBlueprint, const FJsonRef& Out)
	{
		FJsonValues Widgets;
		if (WidgetBlueprint->WidgetTree)
		{
			WidgetBlueprint->WidgetTree->ForEachWidget([&Widgets](UWidget* Widget)
			{
				if (!Widget) { return; }
				FJsonRef Entry = MakeShared<FJsonObject>();
				Entry->SetStringField(TEXT("name"), Widget->GetName());
				Entry->SetStringField(TEXT("class"), ShortName(Widget->GetClass()));
				if (const UPanelWidget* Parent = Widget->GetParent()) { Entry->SetStringField(TEXT("parent"), Parent->GetName()); }
				if (Widget->bIsVariable) { Entry->SetBoolField(TEXT("variable"), true); }
				PushObject(Widgets, Entry);
			});
		}
		Out->SetArrayField(TEXT("widgets"), Widgets);

		FJsonValues Bindings;
		for (const FDelegateEditorBinding& Binding : WidgetBlueprint->Bindings)
		{
			FJsonRef Entry = MakeShared<FJsonObject>();
			Entry->SetStringField(TEXT("widget"), Binding.ObjectName);
			Entry->SetStringField(TEXT("property"), Binding.PropertyName.ToString());
			SetIf(Entry, TEXT("function"), Binding.FunctionName.IsNone() ? FString() : Binding.FunctionName.ToString());
			SetIf(Entry, TEXT("sourceProperty"), Binding.SourceProperty.IsNone() ? FString() : Binding.SourceProperty.ToString());
			PushObject(Bindings, Entry);
		}
		if (Bindings.Num() > 0) { Out->SetArrayField(TEXT("bindings"), Bindings); }

		FJsonValues Animations;
		for (const UWidgetAnimation* Animation : WidgetBlueprint->Animations)
		{
			if (Animation) { PushString(Animations, Animation->GetName()); }
		}
		if (Animations.Num() > 0) { Out->SetArrayField(TEXT("animations"), Animations); }
	}

	static FJsonRef DescribeBlueprint(const UBlueprint* Blueprint, const FAssetData& Asset)
	{
		FJsonRef Out = MakeShared<FJsonObject>();
		Out->SetStringField(TEXT("schema"), Schema);
		Out->SetStringField(TEXT("name"), Asset.AssetName.ToString());
		Out->SetStringField(TEXT("asset"), Asset.GetObjectPathString());
		Out->SetStringField(TEXT("package"), Asset.PackageName.ToString());
		Out->SetStringField(TEXT("assetClass"), Blueprint->GetClass()->GetName());
		Out->SetStringField(TEXT("blueprintType"), StaticEnum<EBlueprintType>()->GetNameStringByValue(static_cast<int64>(Blueprint->BlueprintType)));
		Out->SetStringField(TEXT("status"), StaticEnum<EBlueprintStatus>()->GetNameStringByValue(static_cast<int64>(Blueprint->Status)));
		Out->SetStringField(TEXT("engineVersion"), FEngineVersion::Current().ToString());
		Out->SetStringField(TEXT("exportedAtUtc"), FDateTime::UtcNow().ToIso8601());
		SetIf(Out, TEXT("description"), Blueprint->BlueprintDescription);

		FString File;
		if (FPackageName::TryConvertLongPackageNameToFilename(Asset.PackageName.ToString(), File, FPackageName::GetAssetPackageExtension()))
		{
			File = FPaths::ConvertRelativePathToFull(File);
			FJsonRef Source = MakeShared<FJsonObject>();
			FString Relative = File;
			FPaths::MakePathRelativeTo(Relative, *FPaths::ConvertRelativePathToFull(FPaths::ProjectDir()));
			Source->SetStringField(TEXT("file"), Relative);
			Source->SetNumberField(TEXT("bytes"), static_cast<double>(IFileManager::Get().FileSize(*File)));
			Source->SetStringField(TEXT("modifiedUtc"), IFileManager::Get().GetTimeStamp(*File).ToIso8601());
			Out->SetObjectField(TEXT("source"), Source);
		}

		// Parent chain up to the first native class: BP_Player -> BP_CharacterBase -> Character.
		FJsonValues Chain;
		for (const UClass* Class = Blueprint->ParentClass; Class; Class = Class->GetSuperClass())
		{
			PushString(Chain, PathOf(Class));
			if (Class->HasAnyClassFlags(CLASS_Native)) { break; }
		}
		Out->SetStringField(TEXT("parent"), PathOf(Blueprint->ParentClass));
		Out->SetArrayField(TEXT("parentChain"), Chain);
		Out->SetStringField(TEXT("generatedClass"), PathOf(Blueprint->GeneratedClass));

		FJsonValues Interfaces;
		for (const FBPInterfaceDescription& Implemented : Blueprint->ImplementedInterfaces)
		{
			if (Implemented.Interface) { PushString(Interfaces, PathOf(Implemented.Interface)); }
		}
		Out->SetArrayField(TEXT("interfaces"), Interfaces);

		const UClass* Generated = Blueprint->GeneratedClass;
		FJsonValues Variables;
		for (const FBPVariableDescription& Variable : Blueprint->NewVariables)
		{
			PushObject(Variables, DescribeVariable(Variable, Generated));
		}
		Out->SetArrayField(TEXT("variables"), Variables);

		Out->SetArrayField(TEXT("components"), DescribeComponents(Blueprint));
		FJsonValues Inherited = DescribeInheritedComponents(Generated);
		if (Inherited.Num() > 0) { Out->SetArrayField(TEXT("inheritedComponents"), Inherited); }
		if (Generated && Generated->GetSuperClass())
		{
			FJsonValues Defaults = DiffProperties(Generated->GetDefaultObject(false), Generated->GetSuperClass()->GetDefaultObject(false), Generated);
			if (Defaults.Num() > 0) { Out->SetArrayField(TEXT("classDefaults"), Defaults); }
		}
		FJsonValues Timelines = DescribeTimelines(Blueprint);
		if (Timelines.Num() > 0) { Out->SetArrayField(TEXT("timelines"), Timelines); }
		if (const UWidgetBlueprint* WidgetBlueprint = Cast<UWidgetBlueprint>(Blueprint))
		{
			DescribeWidgetBlueprint(WidgetBlueprint, Out);
		}

		TArray<UEdGraph*> Graphs;
		Blueprint->GetAllGraphs(Graphs);
		FJsonValues GraphValues;
		for (const UEdGraph* Graph : Graphs)
		{
			if (Graph) { PushObject(GraphValues, DescribeGraph(Blueprint, Graph)); }
		}
		Out->SetArrayField(TEXT("graphs"), GraphValues);
		return Out;
	}

	static bool WriteJson(const FString& Path, const FJsonRef& Root)
	{
		FString Text;
		const TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Text);
		if (!FJsonSerializer::Serialize(Root, Writer)) { return false; }
		return FFileHelper::SaveStringToFile(Text, *Path, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
	}

	/** Package path of a list entry: /Game/A/BP_X.BP_X -> /Game/A/BP_X. Folders keep their trailing slash. */
	static FString NormalizeEntry(FString Entry)
	{
		Entry.TrimStartAndEndInline();
		Entry.ReplaceInline(TEXT("\\"), TEXT("/"));
		if (Entry.EndsWith(TEXT("/"))) { return Entry; }
		int32 Slash = INDEX_NONE;
		int32 Dot = INDEX_NONE;
		Entry.FindLastChar(TEXT('/'), Slash);
		Entry.FindLastChar(TEXT('.'), Dot);
		if (Dot != INDEX_NONE && Dot > Slash) { Entry.LeftInline(Dot); }
		return Entry;
	}
}

TArray<TSharedPtr<FJsonValue>> DwoLensInspector::BlueprintClassDefaults(const UBlueprint* Blueprint)
{
	const UClass* Generated = Blueprint ? Blueprint->GeneratedClass.Get() : nullptr;
	if (!Generated || !Generated->GetSuperClass())
	{
		return {};
	}
	return BpExport::DiffProperties(Generated->GetDefaultObject(false), Generated->GetSuperClass()->GetDefaultObject(false), Generated);
}

TArray<TSharedPtr<FJsonValue>> DwoLensInspector::BlueprintComponents(const UBlueprint* Blueprint)
{
	return Blueprint ? BpExport::DescribeComponents(Blueprint) : TArray<TSharedPtr<FJsonValue>>();
}

int32 DwoLensInspector::ExportTrackedBlueprints(const FString& ListFile, const FString& OutDirIn)
{
	using namespace DwoLensInspector::BpExport;

	TArray<FString> Lines;
	if (!FFileHelper::LoadFileToStringArray(Lines, *ListFile))
	{
		UE_LOG(LogDwoLensBp, Error, TEXT("Cannot read the Blueprint list %s"), *ListFile);
		return 1;
	}
	const FString OutDir = FPaths::ConvertRelativePathToFull(OutDirIn);
	IFileManager::Get().MakeDirectory(*OutDir, true);

	IAssetRegistry& Registry = FModuleManager::LoadModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry")).Get();
	Registry.SearchAllAssets(true);

	FJsonValues Requested;
	FJsonValues Missing;
	TArray<FAssetData> Targets;
	TSet<FName> Seen;
	for (const FString& Line : Lines)
	{
		const FString Entry = NormalizeEntry(Line);
		if (Entry.IsEmpty() || Entry.StartsWith(TEXT("#"))) { continue; }
		PushString(Requested, Entry);

		TArray<FAssetData> Found;
		if (Entry.EndsWith(TEXT("/")))
		{
			Registry.GetAssetsByPath(FName(*Entry.LeftChop(1)), Found, true);
		}
		else
		{
			Registry.GetAssetsByPackageName(FName(*Entry), Found);
		}
		int32 Blueprints = 0;
		for (const FAssetData& Asset : Found)
		{
			const UClass* AssetClass = Asset.GetClass(EResolveClass::Yes);
			if (!AssetClass || !AssetClass->IsChildOf(UBlueprint::StaticClass())) { continue; }
			++Blueprints;
			if (!Seen.Contains(Asset.PackageName))
			{
				Seen.Add(Asset.PackageName);
				Targets.Add(Asset);
			}
		}
		if (Blueprints == 0)
		{
			UE_LOG(LogDwoLensBp, Warning, TEXT("No Blueprint found for %s"), *Entry);
			PushString(Missing, Entry);
		}
	}

	FJsonValues Exported;
	FJsonValues Failed;
	for (const FAssetData& Asset : Targets)
	{
		const UBlueprint* Blueprint = Cast<UBlueprint>(Asset.GetAsset());
		const FString Package = Asset.PackageName.ToString();
		const FString Dir = OutDir / Package.RightChop(1);
		const FString File = Dir / (Asset.AssetName.ToString() + TEXT(".json"));
		IFileManager::Get().MakeDirectory(*Dir, true);
		if (!Blueprint || !WriteJson(File, DescribeBlueprint(Blueprint, Asset)))
		{
			UE_LOG(LogDwoLensBp, Warning, TEXT("Could not export %s"), *Package);
			PushString(Failed, Package);
			continue;
		}
		FJsonRef Entry = MakeShared<FJsonObject>();
		Entry->SetStringField(TEXT("package"), Package);
		Entry->SetStringField(TEXT("name"), Asset.AssetName.ToString());
		Entry->SetStringField(TEXT("dir"), Package.RightChop(1));
		PushObject(Exported, Entry);
	}

	FJsonRef Manifest = MakeShared<FJsonObject>();
	Manifest->SetStringField(TEXT("schema"), TEXT("lens.bpexport/1"));
	Manifest->SetStringField(TEXT("engineVersion"), FEngineVersion::Current().ToString());
	Manifest->SetStringField(TEXT("exportedAtUtc"), FDateTime::UtcNow().ToIso8601());
	Manifest->SetArrayField(TEXT("requested"), Requested);
	Manifest->SetArrayField(TEXT("exported"), Exported);
	Manifest->SetArrayField(TEXT("missing"), Missing);
	Manifest->SetArrayField(TEXT("failed"), Failed);
	if (!WriteJson(OutDir / TEXT("export.json"), Manifest))
	{
		return 1;
	}
	UE_LOG(LogDwoLensBp, Display, TEXT("DwoLens Blueprint export: %d exported, %d missing, %d failed -> %s"),
		Exported.Num(), Missing.Num(), Failed.Num(), *OutDir);
	return Failed.Num();
}
