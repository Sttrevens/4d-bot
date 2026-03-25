"""插件系统 —— NanoClaw 启发的可插拔工具架构

核心理念：
- 工具模块自描述（每个 tool 文件声明自己的 group、platform、权限要求）
- 按需加载（租户只加载 tools_enabled 中的工具，不在内存中保留不用的）
- 新增工具零改动核心代码（放到 app/tools/ 并声明 TOOL_MANIFEST 即可）
"""
