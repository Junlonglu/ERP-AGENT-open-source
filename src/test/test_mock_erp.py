"""Mock ERP 契约测试。

两道保险：
1. **覆盖率**：从 `src/mcp_server/tools/*.py` 的 AST 里抽出所有 ``erp_client.<verb>(path)``
   调用，逐个断言 mock 真的提供了这个方法+路径。工具代码加了新端点而 mock 没跟上时，
   这里会红，而不是等 Agent 在对话里撞 404。
2. **行为**：分页、库存预警、出入库、下单、统计等关键语义用 TestClient 实跑。
"""
import ast
import inspect
import re
import unittest

from fastapi.testclient import TestClient

from scripts.mock_erp import app
from src.mcp_server.tools import inventory_tools, order_tools, parts_tools, suppliers_tools

TOOL_MODULES = (order_tools, parts_tools, suppliers_tools, inventory_tools)
_PLACEHOLDER = re.compile(r"\{[^}]+\}")

client = TestClient(app)


def _format_path(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        chunks = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                chunks.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                chunks.append("{param}")
        return "".join(chunks)
    raise AssertionError(f"unsupported path expression: {ast.dump(node)}")


def collect_erp_calls() -> set[tuple[str, str]]:
    calls = set()
    for module in TOOL_MODULES:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = node.func.value
            if not (isinstance(owner, ast.Name) and owner.id == "erp_client"):
                continue
            calls.add((node.func.attr.upper(), _format_path(node.args[0])))
    return calls


def mock_routes() -> set[tuple[str, str]]:
    routes = set()
    for path, operations in app.openapi()["paths"].items():
        normalized = _PLACEHOLDER.sub("{param}", path)
        for method in operations:
            routes.add((method.upper(), normalized))
    return routes


class ContractCoverageTests(unittest.TestCase):
    def test_every_tool_endpoint_is_implemented(self):
        missing = collect_erp_calls() - mock_routes()
        self.assertEqual(missing, set(), f"mock ERP is missing: {sorted(missing)}")

    def test_covers_all_23_tools(self):
        self.assertEqual(len(collect_erp_calls()), 23)

    def test_no_unused_routes(self):
        # 反向检查：mock 里多出来的端点说明工具侧契约已变，别让 mock 变成死代码。
        # /health 是 mock 自己的运维端点，不属于工具契约。
        extra = {route for route in mock_routes() - collect_erp_calls() if route[1] != "/health"}
        self.assertEqual(extra, set(), f"mock ERP serves endpoints no tool calls: {sorted(extra)}")


class ReadEndpointTests(unittest.TestCase):
    def test_order_page_paginates_and_filters(self):
        page = client.get("/api/orders/page", params={"current": 1, "size": 2}).json()
        self.assertEqual(page["size"], 2)
        self.assertEqual(len(page["records"]), 2)
        self.assertGreater(page["total"], 2)

        pending = client.get("/api/orders/page", params={"status": 0}).json()
        self.assertTrue(all(row["status"] == 0 for row in pending["records"]))

        none = client.get("/api/orders/page", params={"orderNumber": "does-not-exist"}).json()
        self.assertEqual(none["total"], 0)

    def test_order_get_includes_details(self):
        order = client.get("/api/orders/get/1").json()
        self.assertEqual(order["id"], 1)
        self.assertTrue(order["orderDetail"])
        self.assertIn("partDetail", order["orderDetail"][0])

    def test_missing_order_is_404(self):
        self.assertEqual(client.get("/api/orders/get/99999").status_code, 404)

    def test_search_details_joins_part_and_supplier(self):
        rows = client.get("/api/orders/search-details", params={"partName": "火花塞"}).json()
        self.assertTrue(rows)
        self.assertTrue(all(row["partName"] == "火花塞" for row in rows))
        self.assertTrue(all(row["supplierName"] for row in rows))

    def test_statistics_sums_by_status(self):
        stats = client.get("/api/orders/statistics").json()
        self.assertEqual(sum(stats["byStatus"].values()), stats["orderCount"])
        self.assertGreater(stats["totalAmount"], 0)
        self.assertIn("topSuppliers", stats)

    def test_inventory_warning_lists_below_safety_stock(self):
        rows = client.get("/api/inventory/warning").json()
        self.assertTrue(rows)
        self.assertTrue(all(row["currentQuantity"] < row["safetyStock"] for row in rows))

    def test_inventory_check_totals(self):
        check = client.get("/api/inventory/check").json()
        self.assertEqual(check["totalSku"], len(client.get("/api/inventory/page", params={"size": 50}).json()["records"]))
        self.assertGreater(check["totalValue"], 0)

    def test_parts_search_and_by_supplier(self):
        self.assertTrue(client.get("/api/parts/search", params={"name": "滤芯"}).json())
        rows = client.get("/api/parts/supplier/3").json()
        self.assertTrue(rows)
        self.assertTrue(all(row["supplierId"] == 3 for row in rows))

    def test_supplier_page_filters_by_status(self):
        rows = client.get("/api/suppliers/page", params={"status": 0}).json()["records"]
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] == 0 for row in rows))


class WriteEndpointTests(unittest.TestCase):
    def test_inbound_then_outbound_round_trip(self):
        before = client.get("/api/inventory/get/1").json()["currentQuantity"]

        inbound = client.post("/api/inventory/inbound", params={"partId": 1, "quantity": 25})
        self.assertEqual(inbound.status_code, 200)
        self.assertEqual(inbound.json()["data"]["currentQuantity"], before + 25)

        outbound = client.post("/api/inventory/outbound", params={"partId": 1, "quantity": 25})
        self.assertEqual(outbound.status_code, 200)
        self.assertEqual(outbound.json()["data"]["currentQuantity"], before)

    def test_outbound_beyond_stock_is_rejected(self):
        response = client.post("/api/inventory/outbound", params={"partId": 5, "quantity": 999999})
        self.assertEqual(response.status_code, 400)
        self.assertIn("insufficient stock", response.json()["detail"])

    def test_create_order_then_read_it_back(self):
        payload = {
            "orderNumber": "PO-MOCK-TEST-1",
            "status": 0,
            "remark": "contract test",
            "orderDetail": [{"partId": 1, "quantity": 10, "unitPrice": 18.5}],
        }
        created = client.post("/api/orders/create", json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        order_id = created.json()["id"]
        self.assertEqual(created.json()["totalAmount"], 185.0)

        fetched = client.get(f"/api/orders/get/{order_id}").json()
        self.assertEqual(fetched["orderNumber"], "PO-MOCK-TEST-1")
        self.assertEqual(fetched["orderDetail"][0]["partDetail"]["name"], "火花塞")

    def test_duplicate_order_number_is_rejected(self):
        payload = {"orderNumber": "PO-MOCK-TEST-1", "orderDetail": [{"partId": 1, "quantity": 1, "unitPrice": 1}]}
        response = client.post("/api/orders/create", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_create_order_with_unknown_part_is_rejected(self):
        payload = {"orderNumber": "PO-MOCK-TEST-2", "orderDetail": [{"partId": 9999, "quantity": 1, "unitPrice": 1}]}
        self.assertEqual(client.post("/api/orders/create", json=payload).status_code, 400)

    def test_order_update_status_validates_enum(self):
        ok = client.patch("/api/orders/update-status/1", params={"status": 3})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["data"]["status"], 3)

        bad = client.patch("/api/orders/update-status/1", params={"status": 9})
        self.assertEqual(bad.status_code, 400)

    def test_create_part_and_supplier_are_persisted(self):
        part = client.post("/api/parts/create", json={
            "partCode": "P-MOCK-NEW", "name": "测试雨刮片", "purchasePrice": 12.5,
            "supplierId": 1, "category": "车身系统",
        })
        self.assertEqual(part.status_code, 200)
        found = client.get("/api/parts/search", params={"name": "测试雨刮片"}).json()
        self.assertEqual([row["partCode"] for row in found], ["P-MOCK-NEW"])

        supplier = client.post("/api/suppliers/create", json={"supplierCode": "SUP-MOCK", "name": "测试供应商"})
        self.assertEqual(supplier.status_code, 200)
        self.assertEqual(client.post("/api/suppliers/create", json={
            "supplierCode": "SUP-MOCK", "name": "重复编码"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
