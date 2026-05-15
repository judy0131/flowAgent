from pydantic import BaseModel, Field
from typing import List, Literal


# --- 1. 请求模型 ---

class ChatRequest(BaseModel):
    """
    用户发起聊天请求时发送的数据模型。
    """
    session_id: str = Field(
        ...,
        description="唯一的会话 ID，用于管理对话历史和状态。",
        min_length=1
    )
    user_id: str = Field(
        ...,
        description="发起请求的用户的唯一 ID。",
        min_length=1
    )
    query: str = Field(
        ...,
        description="用户的输入或问题。",
        min_length=1
    )


class ChatHistoryItem(BaseModel):
    """
    定义会话历史中的单条消息。
    """
    role: Literal["human", "ai", "system"] = Field(
        ...,
        description="消息的角色，如用户 (human) 或 AI (ai)。"
    )
    content: str = Field(
        ...,
        description="消息内容。"
    )


class ChatWithHistoryRequest(ChatRequest):
    """
    （可选）用于测试或提供外部历史的请求模型。
    """
    history: List[ChatHistoryItem] = Field(
        default_factory=list,
        description="当前请求附带的历史消息列表。"
    )


# --- 2. 响应模型 ---

class ToolCall(BaseModel):
    """
    定义 Agent 调用工具的详细信息。
    """
    tool_name: str
    tool_input: dict


class ChatResponse(BaseModel):
    """
    Agent 返回给用户的响应数据模型。
    """
    session_id: str = Field(
        ...,
        description="请求中使用的会话 ID。"
    )
    answer: str = Field(
        ...,
        description="Agent 生成的最终文本回复。"
    )
    is_tool_used: bool = Field(
        False,
        description="指示 Agent 在此轮是否使用了工具。"
    )
    # （可选）如果需要返回更详细的调试信息或工具调用记录
    tool_calls: List[ToolCall] = Field(
        default_factory=list,
        description="Agent 实际调用的工具记录列表。"
    )