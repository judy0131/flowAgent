# main.py

import sys, os
import uvicorn
from fastapi import FastAPI
from fastmcp import Client

from langchain_mcp_adapters.tools import load_mcp_tools

from core.config import settings
from core.logging_setup import setup_logging
from api.routers import router as api_router
import logging
from contextlib import asynccontextmanager


# 获取当前脚本的绝对路径
current_dir = os.path.dirname(os.path.abspath(__file__))

# 获取项目根目录 (假设 services 文件夹就在项目根目录下)
project_root = os.path.dirname(current_dir) # 根据实际层级调整，可能需要两层

# 将项目根目录加入系统路径
if project_root not in sys.path:
    sys.path.insert(0, project_root)


# --- 1. 导入异步资源管理函数 (假设导入路径为 ecoflow_tool) ---
try:
    from ecoflow_tool import (
        init_async_session,
        close_async_session,
        init_db_pool,
        close_db_pool
    )
except ImportError:
    logging.warning("警告：无法导入异步资源管理函数 (ecoflow_tool)。将使用空函数占位。")


    # 定义空函数占位
    async def init_async_session():
        pass


    async def close_async_session():
        pass


    async def init_db_pool():
        pass


    async def close_db_pool():
        pass

# --- 2. 导入 Agent 和 Client 初始化函数 ---
from agent.agent_service import initialize_agent  # 异步初始化 Agent
import core.mcp_client  # 导入 Client 生命周期函数

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 【核心修正：定义 lifespan 上下文管理器】
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期事件处理器：启动 (yield 之前) 和 关闭 (yield 之后)。
    """
    # STARTUP 逻辑
    logger.info("Initializing asynchronous resources (DB Pool, HTTP Session, MCP Client)...")

    # 1. 数据库和 HTTP Session 初始化
    await init_db_pool()
    await init_async_session()

    # 2. FastMCP Client 初始化（连接 Tool Server 并获取工具元数据）
    server_url = settings.fastmcp.SERVER_URL + settings.fastmcp.CLIENT_ENDPOINT

    logger.info(f"正在连接 FastMCP Server: {server_url}")

    # 使用 async with 保持连接
    try:
        async with Client(server_url) as client:
            # A. 连接建立成功，赋值给全局变量
            core.mcp_client = client

            tools = await client.list_tools()
            tool_names = [t.name for t in tools]
            logger.info(f"FastMCP 连接成功，加载工具: {tool_names}")

            # B. 初始化 Agent

            await initialize_agent(client)

            logger.info("FastMCP Agent Service started successfully and resources initialized.")

            yield  # 应用开始处理请求

            # SHUTDOWN 逻辑
            logger.info("Shutting down asynchronous resources...")

            # 关闭 DB Pool 和 HTTP Session
            await close_async_session()
            await close_db_pool()

            logger.info("FastMCP Agent Service shutting down gracefully.")

    except Exception as e:
        logger.error(f"FastMCP 连接失败，应用启动受阻: {e}", exc_info=True)
        raise e
    finally:
        # 清理全局引用
        core.mcp_client = None


# ----------------------------------------------------------------------

def create_app() -> FastAPI:
    """
    初始化并配置 FastAPI 应用。
    """
    # 1. 初始化日志系统
    setup_logging(log_level=settings.LOG_LEVEL)
    logger.info("Starting FastMCP Application initialization...")

    # 根据配置决定是否使用 lifespan
    # ⚠️ 确保 settings 中定义了 USE_LIFESPAN 字段
    app_lifespan = lifespan  # 移除条件判断，强制使用 lifespan

    # 2. 初始化 FastAPI 应用
    app = FastAPI(
        title="FastMCP Agent Service",
        description="基于 LangChain/LangGraph 和 FastAPI 的高级 Agent 服务。",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=app_lifespan  # 传入 lifespan
    )

    # 3. 挂载路由
    app.include_router(api_router)

    # 4. 【其他中间件和事件处理逻辑保持不变】

    return app


if __name__ == "__main__":
    app = create_app()
    # 确保 Uvicorn 运行参数正确
    uvicorn.run(
        app,
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        log_level=settings.LOG_LEVEL,
        # reload=settings.DEBUG # 根据需要保留或移除 reload
    )