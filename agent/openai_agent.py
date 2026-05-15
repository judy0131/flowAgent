from abc import ABC, abstractmethod
from typing import List, Tuple
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langchain_core.runnables import RunnableConfig
from core.config import settings
import logging

logger = logging.getLogger(__name__)


# --- 抽象接口定义 ---

class McpAgent(ABC):
    @abstractmethod
    async def ainvoke(self, input_messages: List[BaseMessage], human_input: str) -> Tuple[str, List[BaseMessage]]:
        """
        异步调用 Agent 处理用户的输入，返回 AI 响应和完整的消息历史。
        """
        pass
# --- LangGraph 核心组件 ---

class AgentState(dict):
    messages: List[BaseMessage]


def run_agent_node(state: AgentState, agent_runnable) -> dict:
    """运行 Agent LLM，解析工具调用或最终回复。"""
    result = agent_runnable.invoke(state["messages"])
    return {"messages": [result]}


def route_agent_action(state: AgentState) -> str:
    """根据最新消息判断下一步走向：工具调用还是结束。"""
    last_message = state["messages"][-1]
    if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
        return "call_tool"
    return "end"


class OpenAIAgent(McpAgent):
    """
    McpAgent 的 LangGraph 实现。
    """
    def __init__(self, tools: List[BaseTool]):
        # 使用新的配置字段
        self.llm = ChatOpenAI(
            model=settings.OPENAI_MODEL_NAME,
            api_key=settings.OPENAI_API_KEY,
            temperature=0,
        ).bind_tools(tools)

        self.tools = tools
        self.agent_executor = self._initialize_agent_executor()
        logger.info(f"LangGraph Agent initialized with {len(tools)} tools using ToolNode.")

    def _initialize_agent_executor(self) -> StateGraph:
        """
        手动构建 LangGraph Agent 流程。
        """
        tool_node = ToolNode(self.tools)
        workflow = StateGraph(AgentState)

        workflow.add_node("agent", lambda state: run_agent_node(state, self.llm))
        workflow.add_node("call_tool", tool_node)

        workflow.set_entry_point("agent")

        workflow.add_conditional_edges(
            "agent",
            route_agent_action,
            {"call_tool": "call_tool", "end": END}
        )

        workflow.add_edge("call_tool", "agent")
        return workflow.compile()

    async def ainvoke(self, input_messages: List[BaseMessage], human_input: str) -> Tuple[str, List[BaseMessage]]:
        """
        实际异步执行 LangGraph Agent 逻辑。
        """
        full_messages = input_messages + [HumanMessage(content=human_input)]

        try:
            config: RunnableConfig = {"recursion_limit": 50}

            result = await self.agent_executor.ainvoke({"messages": full_messages}, config=config)

            updated_messages = result.get("messages", [])
            ai_output = "抱歉，Agent 未能生成有效的回复。"

            if updated_messages and isinstance(updated_messages[-1], AIMessage):
                ai_output = updated_messages[-1].content

            return ai_output, updated_messages

        except Exception as e:
            logger.error(f"LangGraph Agent 异步调用失败: {e}", exc_info=True)
            return "对不起，我在处理您的请求时遇到了一个系统错误。", full_messages