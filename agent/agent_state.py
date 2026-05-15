from typing import TypedDict, Annotated, List

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


class AgentState(TypedDict):
    # ✅ 使用 Annotated + add_messages 实现追加逻辑
    # add_messages 会自动处理追加，并且如果 ID 相同会进行更新
    messages: Annotated[List[BaseMessage], add_messages]
