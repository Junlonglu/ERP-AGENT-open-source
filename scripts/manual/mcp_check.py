"""
MCP Server 连接测试
验证 MCP Server 是否正常运行，所有工具是否可调用
"""

# 手工联调脚本：需要真实 MCP / DeepSeek Key / ERP 后端，不进 CI，也不被 pytest 收集。
import asyncio
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent.env_utils import ensure_utf8_stdio, load_env
load_env()


async def check_mcp_connection():
    """测试 MCP Server 连接和工具调用"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    mcp_url = os.getenv("MCP_SERVER_URL", "http://localhost:9000")
    print(f"[1] 连接 MCP Server: {mcp_url}/sse")

    client = MultiServerMCPClient({
        "erp": {
            "url": f"{mcp_url}/sse",
            "transport": "sse",
        }
    })

    try:
        tools = await client.get_tools()
        print(f"[2] 获取到 {len(tools)} 个工具:")
        for t in tools:
            print(f"    - {t.name}: {t.description[:50]}...")

        # 测试 supplier_query
        print("\n[3] 测试 supplier_query...")
        supplier_tool = next((t for t in tools if "supplier" in t.name), None)
        if supplier_tool:
            result = await supplier_tool.ainvoke({"name": ""})
            print(f"    结果: {str(result)[:200]}")
        else:
            print("    ⚠ 未找到 supplier 工具")

        # 测试 inventory_warning
        print("\n[4] 测试 inventory_warning...")
        inv_tool = next((t for t in tools if "inventory" in t.name), None)
        if inv_tool:
            result = await inv_tool.ainvoke({})
            print(f"    结果: {str(result)[:200]}")
        else:
            print("    ⚠ 未找到 inventory 工具")

        # 测试 part_page
        print("\n[5] 测试 part_page...")
        part_tool = next((t for t in tools if t.name == "part_page"), None)
        if part_tool:
            result = await part_tool.ainvoke({"current": 1, "size": 5})
            print(f"    结果: {str(result)[:200]}")
        else:
            print("    ⚠ 未找到 part 工具")

        print("\n✓ MCP 测试完成")
        print("[提示] 若 ERP 后端（ERP_BASE_URL）未启动，工具返回 'All connection attempts failed' 属预期，不表示 MCP 异常。")

    except Exception as exc:  # 联调脚本：失败要看得见原因，而不是默默返回
        print(f"✗ MCP 测试失败: {exc!r}")
        raise

    # MultiServerMCPClient（langchain-mcp-adapters >= 0.3）是无状态的，没有 close()；
    # get_tools() 内部自行建立并关闭会话，这里不需要清理。


if __name__ == "__main__":
    ensure_utf8_stdio()
    asyncio.run(check_mcp_connection())
