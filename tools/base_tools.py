from langchain_core.tools import BaseTool, tool
from typing import Optional, Type, Any
from pydantic import BaseModel, Field


# --- 1. 抽象基类定义 (LangChain Core/Community 提供) ---
# 我们通常直接继承 BaseTool 或使用 @tool 装饰器。
# 在这里我们提供一个自定义的抽象工具类，以便于添加项目特定的方法或属性（如果需要）。

class FastMCPBaseTool(BaseTool):
    """
    FastMCP 项目中所有业务工具的抽象基类。
    继承自 LangChain 的 BaseTool。
    """

    # LangChain BaseTool 要求实现 name, description, _run/_arun

    # name: str (工具的唯一名称)
    # description: str (工具的描述，对 LLM 调用至关重要)

    # args_schema: Optional[Type[BaseModel]] = None
    # 如果工具需要结构化输入，定义 Pydantic Schema

    def _run(self, *args: Any, **kwargs: Any) -> str:
        """同步执行工具逻辑。"""
        # 必须由子类实现
        raise NotImplementedError("Tool must implement the synchronous _run method.")

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        """异步执行工具逻辑 (推荐在 FastAPI/Uvicorn 环境中使用)。"""
        # 默认实现为调用 _run，但推荐子类实现真正的异步逻辑
        return self._run(*args, **kwargs)


# --- 2. 示例：用户档案查询工具的 Pydantic 输入 Schema ---
class GetUserProfileSchema(BaseModel):
    """
    用户档案查询工具的输入 Schema。
    定义清晰的输入有助于 LLM 正确调用工具。
    """
    user_id: str = Field(
        ...,
        description="要查询的用户在系统中的唯一 ID。"
    )


# --- 3. 示例：具体的工具实现 ---
# 这将放在 tools/user_management.py 中，但我们先在这里展示结构。

class UserProfileCheckerTool(FastMCPBaseTool):
    """
    用于查询系统内部用户档案信息的工具。
    """
    name: str = "user_profile_checker"
    description: str = "当你需要查询用户在系统中的姓名、注册时间或权限等档案信息时，请使用此工具。输入参数为 user_id。"
    args_schema: Optional[Type[BaseModel]] = GetUserProfileSchema

    def _run(self, user_id: str) -> str:
        """同步查询用户档案的模拟实现。"""
        # 实际代码会调用数据库或外部 API
        if user_id == "A123":
            return "用户ID A123 的档案：姓名: 张三，权限: 管理员，注册日期: 2023-01-01。"
        elif user_id == "B456":
            return "用户ID B456 的档案：姓名: 李四，权限: 普通用户，注册日期: 2024-05-15。"
        else:
            return f"错误：未找到ID为 {user_id} 的用户档案。"