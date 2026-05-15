from abc import abstractmethod, ABC
from typing import Tuple, List

from langchain_core.messages import BaseMessage


class McpAgent(ABC):
    @abstractmethod
    async def ainvoke(self, input_messages: List[BaseMessage], human_input: str) -> Tuple[str, List[BaseMessage]]:
        """
        异步调用 Agent 处理用户的输入，返回 AI 响应和完整的消息历史。
        """
        pass
# --- LangGraph 核心组件 ---