# /core/mcp_client.py

import logging
from typing import List
# 核心修正：移除无法导入的 LangChainRemoteTool
from fastmcp import Client
from langchain_core.tools import BaseTool
from core.config import settings

logger = logging.getLogger(__name__)

# 全局 FastMCP Client 实例
mcp_client: Client | None = None

async def get_remote_tools() -> List[BaseTool]:
    """
    通过 FastMCP Client 获取 LangChain 兼容的远程工具列表。
    """
    if mcp_client is None:
        logger.warning("FastMCP Client 未初始化，返回空列表。")
        return []

    # 核心修正：使用 FastMCP Client 的内置方法生成 LangChain 兼容工具
    try:
        remote_tools = await mcp_client.list_tools()
        if not remote_tools:
            logger.warning("FastMCP Client 已连接，但未获取到任何工具元数据。")
        return remote_tools
    except Exception as e:
        logger.error(f"获取 LangChain 工具列表失败: {e}", exc_info=True)
        return []