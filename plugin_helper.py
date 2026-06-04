import json


def extract_system(messages):
    for i in messages:
        if i["role"] == "system":
            return i["content"]
    return None


def extract_tools(tools):
    final_tools = ""
    for i in tools:
        if i["type"] == "function":
            final_tools += (
                "Tool Type: function. Tool name: "
                + i["function"]["name"]
                + ", description: "
                + i["function"].get("description")
                + ". Params needed: "
                + json.dumps(i["function"].get("parameters"))
                + "\n"
            )

        elif i["type"] == "computer_use":
            final_tools += (
                "Tool name: computer_use. Description: Use this to interact with the screen, mouse, and keyboard. Other whole info: "
                + json.dumps(i)
                + "\n"
            )
        elif i["type"] == "text_editor":
            final_tools += "Tool name: text_editor. Description: Use this to view and edit text files\n"
        elif i["type"] == "bash":
            final_tools += (
                "Tool name: bash. Description: Use this to run bash commands\n"
            )
        else:
            final_tools += "Tool: " + json.dumps(i)
    return final_tools if final_tools != "" else None


def extract_tool_results(messages):
    tools_final = ""
    for i in messages:
        if i["role"] == "tool":
            tools_final += f"""Tool result for {i["name"]}:\n{i["content"]}. Tool Call ID: {i["tool_call_id"]}\n"""
    return tools_final if tools_final != "" else None
