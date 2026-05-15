# /agent/alibaba_agent.py
from abc import abstractmethod
from typing import List, Tuple, Dict, Any
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_community.chat_models import ChatTongyi
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langchain_core.runnables import RunnableConfig

from agent.agent_state import AgentState
from agent.mcp_agent import McpAgent
from agent.system_prompt import SYSTEM_PROMPT
from core.config import settings
import logging

logger = logging.getLogger(__name__)


class AlibabaAgent(McpAgent):
    """
    McpAgent 的阿里云通义千问实现，使用 LangGraph。
    """

    def __init__(self, tools: List[BaseTool]):
        # 1. 初始化 LLM
        # 建议使用 api_key 参数，dashscope_api_key 也兼容但 api_key 更通用
        self.llm = ChatTongyi(
            model_name=settings.TONGYI_MODEL_NAME,
            api_key=settings.TONGYI_API_KEY,  # 修正参数名
            temperature=0,
            streaming=True  # 建议开启流式，虽然这里没用到流式输出，但底层机制更稳
        ).bind_tools(tools)

        self.tools = tools
        self.agent_executor = self._initialize_agent_executor()
        logger.info(f"Alibaba Tongyi Agent initialized with {len(tools)} tools.")

    def _initialize_agent_executor(self) -> StateGraph:
        """
        构建 LangGraph Agent 流程。
        """
        tool_node = ToolNode(self.tools)
        workflow = StateGraph(AgentState)

        # 2. 修正：直接注册类方法或异步函数，避免 lambda 阻塞
        workflow.add_node("agent", self.call_model)
        workflow.add_node("call_tool", tool_node)

        workflow.set_entry_point("agent")

        # 路由逻辑保持不变
        workflow.add_conditional_edges(
            "agent",
            route_agent_action,
            {"call_tool": "call_tool", "end": END}
        )
        workflow.add_edge("call_tool", "agent")

        return workflow.compile()

    async def call_model(self, state: AgentState) -> Dict[str, List[BaseMessage]]:
        """
        运行 Agent LLM 的节点函数 (异步版本)。
        """
        current_messages = state["messages"]

        # 将 SystemMessage 放在列表开头，然后跟上所有其他消息
        # 这样可以确保 System Prompt 总是第一个被 LLM 看到
        system_message = SystemMessage(content=SYSTEM_PROMPT)
        messages  = [system_message] + current_messages


        #  【调试代码：打印完整消息内容】
        if settings.DEBUG:
            print(f"\n======   LLM Context Dump (Msg Count: {len(messages)}) ======")
            for i, msg in enumerate(messages):
                role_tag = f"[{msg.type.upper()}]"
                content_full = str(msg.content)

                # 1. 打印元数据 (索引、角色、内容长度)
                print(f"  Msg {i:<2} {role_tag:<12} (Length: {len(content_full)})")

                # 2. 打印完整内容 (保留换行符，方便查看 JSON 结构)
                if content_full:
                    print(content_full)
                else:
                    print("(Content is Empty)")

                #   场景 A: AI 决定调用工具 (详细打印)
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    print("    ️  [Tool Calls Request]:")
                    for tc in msg.tool_calls:
                        tool_name = tc.get('name', 'Unknown')
                        tool_args = tc.get('args', {})
                        tool_id = tc.get('id', 'No-ID')
                        print(f"       • Name : {tool_name}")
                        print(f"       • Args : {tool_args}")
                        print(f"       • ID   : {tool_id}")

                #   场景 B: 工具执行完毕返回结果 (显示对应 ID)
                elif hasattr(msg, 'tool_call_id'):  # ToolMessage
                    print(f"     [Tool Result] linked to ID: {msg.tool_call_id}")

                # 打印分割线，避免长文本混淆
                print("-" * 60)

            print("============================================================\n")
            print("============================================================\n")

        # 3. 修正：使用 ainvoke (异步调用)
        # 注意：ChatTongyi 内部会自动处理 DashScope 的格式要求
        try:
            response = await self.llm.ainvoke(messages)
            return {"messages": [response]}
        except Exception as e:
            logger.error(f"调用通义千问 API 失败: {e}")
            raise e

    async def astream_run(self, input_messages: List[BaseMessage], human_input: str):
        """
        流式生成器：只返回 LLM 给用户的最终回复 Token。
        """
        # 1. 构造输入
        full_messages = input_messages + [HumanMessage(content=human_input)]
        config: RunnableConfig = {"recursion_limit": 50}

        # 2. 使用 LangGraph 的 astream_events 监听所有事件
        # version="v1" 是必须的
        async for event in self.agent_executor.astream_events(
                {"messages": full_messages},
                config=config,
                version="v1"
        ):
            # 3. 关键过滤：我们只关心 LLM 正在吐字的事件 (on_chat_model_stream)
            # 并且要过滤掉 ToolNode 内部可能产生的干扰
            kind = event["event"]
            data = event["data"]

            if kind == "on_chat_model_stream":
                # 获取当前 chunk
                chunk = data.get("chunk")

                # 确保是内容 chunk，而不是空的元数据
                if hasattr(chunk, "content") and chunk.content:
                    # ⚡️ Yield 出去，交给 FastAPI 发送给前端
                    yield {"type": "token", "content": chunk.content}

            elif kind == "on_chat_model_end":
                output = data.get("output")
                # output 是一个字典，结构如你所发：{'generations': [[{'message': AIMessage(...)}]]}
                ai_message = None

                if isinstance(output, dict) and "generations" in output:
                    generations = output.get("generations", [])
                    if generations and len(generations) > 0:
                        # 通常 generations 是一个列表的列表 (n 个 candidate)
                        first_gen_list = generations[0]
                        if first_gen_list and len(first_gen_list) > 0:
                            first_gen = first_gen_list[0]
                            # first_gen 可能是 ChatGeneration 对象，也可能是字典
                            if isinstance(first_gen, dict):
                                ai_message = first_gen.get("message")

                # ⚡️ 只要提取到了 AIMessage，并且它有内容或者有工具调用，就抛出
                if isinstance(ai_message, AIMessage):
                    # 哪怕 content 是空字符串，只要有 tool_calls，也必须保存！
                    if ai_message.content or ai_message.tool_calls:
                        yield {"type": "message", "content": ai_message}

            elif kind == "on_tool_end":
                output = event["data"]["output"]
                # 标准 ToolNode 通常返回 ToolMessage，但有时可能是 raw output
                # 如果是 ToolMessage 直接捕获
                if isinstance(output, ToolMessage):
                    yield {"type": "message", "content": output}
                # 如果是 ToolNode 返回的列表 (比如多工具并行)，这里可能需要遍历
                elif isinstance(output, list):
                    for item in output:
                        if isinstance(item, BaseMessage):
                            yield {"type": "message", "content": item}

    async def ainvoke(self, input_messages: List[BaseMessage], human_input: str) -> Tuple[str, List[BaseMessage]]:
        """
        实际异步执行 LangGraph Agent 逻辑。
        """
        # 构造当前轮次的完整输入
        full_messages = input_messages + [HumanMessage(content=human_input)]

        try:
            config: RunnableConfig = {"recursion_limit": 50}

            # 异步调用 Graph
            result = await self.agent_executor.ainvoke({"messages": full_messages}, config=config)

            updated_messages = result.get("messages", [])
            ai_output = "抱歉，Agent 未能生成有效的回复。"

            if updated_messages and isinstance(updated_messages[-1], AIMessage):
                ai_output = updated_messages[-1].content

            return ai_output, updated_messages

        except Exception as e:
            logger.error(f"LangGraph Agent 异步调用失败: {e}", exc_info=True)
            # 如果是 Parameter Invalid 错误，通常这里会捕获到
            if "InvalidParameter" in str(e):
                return "系统错误：上下文消息顺序异常（DashScope校验失败）。请尝试清除历史记录重试。", full_messages

            return "对不起，我在处理您的请求时遇到了一个系统错误。", full_messages


def route_agent_action(state: AgentState) -> str:
    """根据最新消息判断下一步走向：工具调用还是结束。"""
    last_message = state["messages"][-1]
    # 检查是否有 tool_calls
    if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
        return "call_tool"
    return "end"