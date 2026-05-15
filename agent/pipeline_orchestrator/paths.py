from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
AGENT_ROOT = PACKAGE_ROOT.parent
REPO_ROOT = AGENT_ROOT.parent
DEFAULT_SKILLS_ROOT = REPO_ROOT / "skills" / "operators"
TOOL_GRAPH_EDGES_PATH = REPO_ROOT / "data" / "workflowhub" / "tool_tool_edges_enriched.csv"
