from typing import Any, Dict


def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    artifacts = ctx.setdefault("artifacts", {})
    trace = ctx.setdefault("taskbench_tool_trace", [])
    trace.append({"args": dict(args)})

    output_key = args.get("output_key")
    if isinstance(output_key, str) and output_key.strip():
        artifacts[output_key] = {
            "status": "ok",
            "args": dict(args),
        }

    return {"status": "ok", "echo_args": dict(args)}
