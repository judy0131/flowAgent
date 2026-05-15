from abc import ABC, abstractmethod
from typing import Dict, List
from langchain_core.messages import BaseMessage
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_community.chat_message_histories.in_memory import ChatMessageHistory
import logging

logger = logging.getLogger(__name__)


# --- 1. 接口定义 ---

class MemoryStore(ABC):
    """
    抽象的内存存储接口，用于规范会话历史的存取操作。
    """

    @abstractmethod
    def get_history(self, session_id: str) -> BaseChatMessageHistory:
        pass

    @abstractmethod
    def save_history(self, session_id: str, history: BaseChatMessageHistory) -> None:
        pass


# --- 2. 内存实现 ---

class InMemoryStore(MemoryStore):
    """
    一个简单的基于内存字典的存储实现。
    """

    def __init__(self):
        self.store: Dict[str, BaseChatMessageHistory] = {}
        logger.info("InMemoryStore initialized.")

    def get_history(self, session_id: str) -> BaseChatMessageHistory:
        """
        如果会话存在则返回历史，否则返回一个新的历史对象。
        """
        if session_id not in self.store:
            self.store[session_id] = ChatMessageHistory()

        return self.store[session_id]

    def save_history(self, session_id: str, history: BaseChatMessageHistory) -> None:
        """
        保存历史对象到内存。
        """
        self.store[session_id] = history


# --- 3. 会话管理器 (封装业务逻辑) ---

class SessionManager:
    """
    封装对 MemoryStore 的操作，提供给 Agent Service 使用。
    """

    def __init__(self, memory_store: MemoryStore):
        self._memory_store = memory_store

    def load_memory(self, session_id: str) -> List[BaseMessage]:
        """
        加载特定会话 ID 的所有历史消息。
        """
        history = self._memory_store.get_history(session_id)
        return history.messages

    def save_complete_history(self, session_id: str, messages: List[BaseMessage]) -> None:
        """
        将 Agent 执行后返回的完整 BaseMessage 列表保存到历史中。
        """
        history = self._memory_store.get_history(session_id)

        # 覆盖历史：清空旧历史，并用新的完整历史覆盖
        history.clear()
        for message in messages:
            history.add_message(message)

        self._memory_store.save_history(session_id, history)
        logger.debug(f"Session {session_id}: Saved {len(messages)} messages.")


# 实例化默认的会话管理器，供全局使用
session_manager = SessionManager(memory_store=InMemoryStore())