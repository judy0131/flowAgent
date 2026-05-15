# FastMCP 项目结构文档

## 项目目录结构
## 模块说明

### 1. 协议层 (`/api`)
- **models.py**: 定义 Pydantic 数据模型，用于 API 请求和响应的验证和序列化（如 `ChatRequest`, `ChatResponse`）。
- **routers.py**: 定义 FastAPI 路由，处理 HTTP 请求并调用核心业务逻辑。

### 2. 核心协调层 (`/agent`)
- **alibaba_agent.py**: 定义 `McpAgent` 通义千问 实现，包含 LLM 和 AgentExecutor 的实例化。
- **openai_agent.py**: `McpAgent` 的 OpenAI 实现，包含 LLM 和 AgentExecutor 的实例化。
- **agent_service.py**: 封装 `McpService` 类，提供 `run_mcp_agent` 核心方法。
- **session_manager.py**: 管理会话状态和内存存储（`SessionManager` 和 `MemoryStore` 接口及实现）。

### 3. 能力层 (`/tools`)
- **base_tools.py**: 定义核心工具的基类（如使用 LangChain 的 `BaseTool`）。
- **user_management.py**: 实现用户档案查询工具（如 `get_user_profile`）。
- **...**: 其他业务工具（如 `inventory_checker.py`）。

### 4. RAG 子模块 (`/retrieval`)
- **embeddings.py**: 初始化嵌入模型和向量存储（`VectorStore`）。
- **retriever_tool.py**: 提供 RAG 检索器工具，作为 Agent 的工具接口。
- **retriever_service.py**: 封装 RAG 检索流程，提供统一的检索服务。

### 5. 基础设施/配置 (`/core`)
- **config.py**: 使用 Pydantic 的 `Settings` 管理应用配置（如数据库连接、API 密钥）。
- **logging_setup.py**: 配置和初始化日志系统。

### 应用入口 (`main.py`)
- 加载配置、初始化 FastAPI 应用、挂载路由，并启动服务。