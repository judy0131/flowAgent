from typing import Any, Dict


def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    artifacts = ctx.setdefault("artifacts", {})
    source_ref = str(args.get("source_ref", "external_input"))
    output_key = str(args.get("output_key", "snpeff_output"))
    operation = "SnpEff eff"
    tool_id = "toolshed_g2_bx_psu_edu_repos_iuc_snpeff_snpEff_4_3+T_galaxy1"

    source = artifacts.get(source_ref, source_ref)
    produced = {
        "operation": operation,
        "tool_id": tool_id,
        "source_ref": source_ref,
        "source": source,
        "output_key": output_key,
        "status": "simulated_ok",
    }
    artifacts[output_key] = produced
    return produced
