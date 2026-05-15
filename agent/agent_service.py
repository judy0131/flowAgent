# /agent/agent_service.py

from typing import List, Tuple, Union, AsyncGenerator

from fastmcp import Client
from langchain_core.messages import HumanMessage, AIMessage
from langchain_mcp_adapters.tools import load_mcp_tools

from api.models import ChatRequest, ChatResponse
from agent.session_manager import session_manager, SessionManager
from langchain_core.tools import BaseTool
from core.config import settings
import logging


# 核心变更点: 导入 FastMCP Client 相关的函数
from core.mcp_client import get_remote_tools  # 只需要这个函数
from .mcp_agent import McpAgent

logger = logging.getLogger(__name__)

# --- Agent 工厂和懒加载实现 ---

# 全局 Agent Service 实例
mcp_service: Union['AgentService', None] = None

def set_mcp_service(service):
    global mcp_service
    mcp_service = service

def get_mcp_service():
    return mcp_service

# 1. 定义 AgentClass (保持不变)
if settings.LLM_PROVIDER.lower() == "alibaba":
    try:
        from .alibaba_agent import AlibabaAgent

        AgentClass = AlibabaAgent
        logger.info("Agent Factory: 准备使用 Alibaba Tongyi Agent")
    except ImportError:
        logger.error("Agent Factory: 无法导入 AlibabaAgent")

else:
    logger.warning(f"Agent Factory: 不支持的 LLM_PROVIDER: {settings.LLM_PROVIDER}。")


# --- Agent Service 类 ---

class AgentService:
    """
    负责管理 Agent 实例、会话历史和工具调用的核心服务。
    """

    def __init__(self, agent_instance: McpAgent, session_manager: SessionManager):
        self._agent = agent_instance
        self._session_manager = session_manager
        logger.info(
            f"AgentService initialized with Agent: {type(agent_instance).__name__} and {len(agent_instance.tools)} remote tools.")

    async def stream_mcp_agent(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        """
        核心方法：流式运行 Agent。
        Yields:
            str: LLM 生成的每一个 Token
        """
        if self._agent is None:
            yield "系统错误：Agent 未初始化。"
            return

        session_id = request.session_id
        user_id = request.user_id
        human_input = f"UserId: {user_id}, SessionId: {session_id}, HumanInput: {request.query}"

        # 1. 加载历史 (保持不变)
        history_messages = self._session_manager.load_memory(session_id)
        logger.debug(f"Session {session_id}: Loaded {len(history_messages)} messages.")

        # 2. 准备一个列表，专门收集本轮产生的所有新消息
        new_messages_buffer = [HumanMessage(content=human_input)]

        # 3. 调用 Agent 的流式方法 (注意：这里调用的是我们上一条讨论加的 astream_run)
        try:
            # 假设你在 AlibabaAgent 里已经加了 astream_run 方法
            async for item in self._agent.astream_run(
                    input_messages=history_messages,
                    human_input=human_input
            ):
                # case A: 收到 Token -> 实时发送给前端
                if item["type"] == "token":
                    yield item["content"]

                # case B: 收到完整消息 (AIMessage 或 ToolMessage) -> 存入缓冲区
                elif item["type"] == "message":
                    msg = item["content"]

                    new_messages_buffer.append(msg)

        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"\n[系统错误: {str(e)}]"
            return

        # 4. 流式结束后，手动保存历史记录
        if len(new_messages_buffer) > 1:  # 确保不仅仅只有 HumanMessage

            # 4. 保存到数据库
            updated_history = history_messages + new_messages_buffer
            self._session_manager.save_complete_history(session_id, updated_history)

            logger.info(f"Session {session_id}: Conversation saved ({len(new_messages_buffer)} new messages).")


# ----------------------------------------------------------------------
# 【核心变更：异步初始化 Agent】
# ----------------------------------------------------------------------
async def initialize_agent(mcp_client: Client):
    """
    异步初始化 Agent Service
    """
    global mcp_service

    if hasattr(mcp_client, "session") and mcp_client.session:
        remote_tools = await load_mcp_tools(mcp_client.session)

    if not remote_tools:
        logger.warning("未发现远程工具。Agent 将以无工具模式运行，功能受限。")

    # 2. 实例化 Agent
    agent_instance = AgentClass(tools=remote_tools)

    # 3. 实例化 AgentService
    mcp_service = AgentService(
        agent_instance=agent_instance,
        session_manager=session_manager
    )
    logger.info("Agent Service initialization complete.")