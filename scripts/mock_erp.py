#!/usr/bin/env python
"""Mock ERP 后端：实现 MCP 工具依赖的 23 个 REST 端点。

为什么需要它：`src/mcp_server/tools/*.py` 里的 23 个工具都会打 ``ERP_BASE_URL``
（默认 http://localhost:8081），而真正的 Java ERP 不在本仓库里。没有这个 mock，
Agent 一碰订单/库存/供应商就会拿到 ``All connection attempts failed``。

契约按工具代码里的实际调用生成（方法 + 路径 + 参数位置），响应是**裸 JSON**
（``src/mcp_server/http_base.py`` 只把 200 的 JSON 原样透传给模型，不做二次包装）。

用法：
    .venv/Scripts/python.exe scripts/dev.py mock-erp          # :8081
    .venv/Scripts/python.exe scripts/dev.py mock-erp --port 18081

数据只在内存里，进程退出即重置。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query

DEFAULT_PORT = 8081

app = FastAPI(title="Mock ERP", version="1.0.0", description="In-memory stand-in for the Java ERP backend")

ORDER_STATUS = {0: "待审核", 1: "已审核", 2: "已发货", 3: "已收货", 4: "已完成"}

# --------------------------------------------------------------------------- 种子数据

_suppliers: list[dict] = [
    {"id": 1, "supplierCode": "SUP-BOSCH", "name": "博世汽车部件", "contactPerson": "张伟",
     "phone": "021-88881234", "email": "sales@bosch.example", "address": "上海市浦东新区金桥路 1 号",
     "creditRating": "A", "status": 1},
    {"id": 2, "supplierCode": "SUP-DENSO", "name": "电装（中国）投资", "contactPerson": "李娜",
     "phone": "020-66665555", "email": "sales@denso.example", "address": "广州市黄埔区科学城 8 号",
     "creditRating": "A", "status": 1},
    {"id": 3, "supplierCode": "SUP-MANN", "name": "曼牌滤清器", "contactPerson": "王强",
     "phone": "0512-55778899", "email": "sales@mann.example", "address": "苏州市工业园区星湖街 66 号",
     "creditRating": "B", "status": 1},
    {"id": 4, "supplierCode": "SUP-MAHLE", "name": "马勒贸易", "contactPerson": "赵敏",
     "phone": "010-44556677", "email": "sales@mahle.example", "address": "北京市朝阳区酒仙桥路 5 号",
     "creditRating": "B", "status": 0},
    {"id": 5, "supplierCode": "SUP-VALEO", "name": "法雷奥汽车零部件", "contactPerson": "陈刚",
     "phone": "027-33445566", "email": "sales@valeo.example", "address": "武汉市经济技术开发区 12 号",
     "creditRating": "C", "status": 1},
]

_parts: list[dict] = [
    {"id": 1, "partCode": "P-SPK-001", "name": "火花塞", "model": "FR7DC+", "specification": "M14x1.25",
     "unit": "个", "purchasePrice": 18.5, "suggestedRetailPrice": 39.0, "stockWarningValue": 200,
     "supplierId": 1, "category": "发动机系统", "description": "铱金火花塞，适配 1.6L 自然吸气发动机"},
    {"id": 2, "partCode": "P-BRK-002", "name": "刹车片", "model": "BP-320i", "specification": "前轮",
     "unit": "套", "purchasePrice": 128.0, "suggestedRetailPrice": 260.0, "stockWarningValue": 80,
     "supplierId": 2, "category": "制动系统", "description": "陶瓷配方前刹车片"},
    {"id": 3, "partCode": "P-FLT-003", "name": "机油滤芯", "model": "W712/75", "specification": "直径 76mm",
     "unit": "个", "purchasePrice": 22.0, "suggestedRetailPrice": 45.0, "stockWarningValue": 300,
     "supplierId": 3, "category": "滤清系统", "description": "旋装式机油滤清器"},
    {"id": 4, "partCode": "P-FLT-004", "name": "空气滤芯", "model": "C25114", "specification": "方形",
     "unit": "个", "purchasePrice": 35.0, "suggestedRetailPrice": 72.0, "stockWarningValue": 250,
     "supplierId": 3, "category": "滤清系统", "description": "高流量纸质空气滤芯"},
    {"id": 5, "partCode": "P-ELE-005", "name": "发电机", "model": "ALT-110A", "specification": "110A/14V",
     "unit": "台", "purchasePrice": 680.0, "suggestedRetailPrice": 1280.0, "stockWarningValue": 20,
     "supplierId": 5, "category": "电气系统", "description": "交流发电机总成"},
    {"id": 6, "partCode": "P-ELE-006", "name": "起动机", "model": "STR-1.4KW", "specification": "1.4KW/12V",
     "unit": "台", "purchasePrice": 560.0, "suggestedRetailPrice": 1050.0, "stockWarningValue": 20,
     "supplierId": 5, "category": "电气系统", "description": "减速起动机"},
    {"id": 7, "partCode": "P-BRK-007", "name": "刹车盘", "model": "BD-280", "specification": "直径 280mm",
     "unit": "个", "purchasePrice": 210.0, "suggestedRetailPrice": 420.0, "stockWarningValue": 60,
     "supplierId": 2, "category": "制动系统", "description": "通风打孔刹车盘"},
    {"id": 8, "partCode": "P-SPK-008", "name": "点火线圈", "model": "IC-004", "specification": "四缸独立",
     "unit": "个", "purchasePrice": 145.0, "suggestedRetailPrice": 298.0, "stockWarningValue": 50,
     "supplierId": 1, "category": "发动机系统", "description": "笔式点火线圈"},
    {"id": 9, "partCode": "P-FLT-009", "name": "空调滤芯", "model": "CU2545", "specification": "活性炭",
     "unit": "个", "purchasePrice": 28.0, "suggestedRetailPrice": 59.0, "stockWarningValue": 200,
     "supplierId": 3, "category": "滤清系统", "description": "带活性炭层空调滤芯"},
    {"id": 10, "partCode": "P-ENG-010", "name": "正时皮带", "model": "TB-135", "specification": "135 齿",
     "unit": "根", "purchasePrice": 96.0, "suggestedRetailPrice": 198.0, "stockWarningValue": 100,
     "supplierId": 1, "category": "发动机系统", "description": "HNBR 材质正时皮带"},
    {"id": 11, "partCode": "P-ELE-011", "name": "氧传感器", "model": "O2-4W", "specification": "四线加热型",
     "unit": "个", "purchasePrice": 175.0, "suggestedRetailPrice": 358.0, "stockWarningValue": 40,
     "supplierId": 2, "category": "电气系统", "description": "前氧传感器"},
    {"id": 12, "partCode": "P-BRK-012", "name": "制动液", "model": "DOT4-1L", "specification": "1L/瓶",
     "unit": "瓶", "purchasePrice": 32.0, "suggestedRetailPrice": 68.0, "stockWarningValue": 150,
     "supplierId": 4, "category": "制动系统", "description": "DOT4 级合成制动液"},
]

# partId -> 库存
_inventory: list[dict] = [
    {"id": 1, "partId": 1, "currentQuantity": 320, "safetyStock": 200, "warehouseLocation": "A-01-03"},
    {"id": 2, "partId": 2, "currentQuantity": 45, "safetyStock": 80, "warehouseLocation": "B-02-11"},
    {"id": 3, "partId": 3, "currentQuantity": 410, "safetyStock": 300, "warehouseLocation": "A-03-02"},
    {"id": 4, "partId": 4, "currentQuantity": 180, "safetyStock": 250, "warehouseLocation": "A-03-05"},
    {"id": 5, "partId": 5, "currentQuantity": 8, "safetyStock": 20, "warehouseLocation": "C-01-01"},
    {"id": 6, "partId": 6, "currentQuantity": 14, "safetyStock": 20, "warehouseLocation": "C-01-02"},
    {"id": 7, "partId": 7, "currentQuantity": 96, "safetyStock": 60, "warehouseLocation": "B-04-07"},
    {"id": 8, "partId": 8, "currentQuantity": 72, "safetyStock": 50, "warehouseLocation": "C-02-03"},
    {"id": 9, "partId": 9, "currentQuantity": 60, "safetyStock": 200, "warehouseLocation": "A-03-09"},
    {"id": 10, "partId": 10, "currentQuantity": 130, "safetyStock": 100, "warehouseLocation": "D-01-04"},
    {"id": 11, "partId": 11, "currentQuantity": 26, "safetyStock": 40, "warehouseLocation": "C-02-08"},
    {"id": 12, "partId": 12, "currentQuantity": 210, "safetyStock": 150, "warehouseLocation": "B-05-02"},
]

_now = datetime(2026, 11, 9, 10, 0, 0)


def _part(part_id: int) -> Optional[dict]:
    return next((p for p in _parts if p["id"] == part_id), None)


def _order(order_id: int, number: str, days_ago: int, status: int, rows: list[tuple[int, int, float]]) -> dict:
    order_time = (_now - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    details = [
        {"id": order_id * 100 + index, "partId": part_id, "quantity": quantity,
         "unitPrice": unit_price, "remark": None, "partDetail": _part(part_id)}
        for index, (part_id, quantity, unit_price) in enumerate(rows, start=1)
    ]
    return {
        "id": order_id,
        "orderNumber": number,
        "totalAmount": round(sum(quantity * unit_price for _, quantity, unit_price in rows), 2),
        "status": status,
        "remark": None,
        "orderTime": order_time,
        "orderDetail": details,
    }


_orders: list[dict] = [
    _order(1, "PO20261001001", 39, 4, [(1, 500, 18.5), (3, 300, 22.0)]),
    _order(2, "PO20261015002", 25, 3, [(2, 120, 128.0), (7, 80, 210.0)]),
    _order(3, "PO20261028003", 12, 2, [(5, 10, 680.0), (6, 15, 560.0)]),
    _order(4, "PO20261102004", 7, 1, [(4, 200, 35.0), (9, 150, 28.0)]),
    _order(5, "PO20261105005", 4, 0, [(8, 60, 145.0), (10, 100, 96.0)]),
    _order(6, "PO20261108006", 1, 0, [(12, 100, 32.0), (11, 30, 175.0)]),
]


# --------------------------------------------------------------------------- 内部工具


def _next_id(rows: list[dict]) -> int:
    return max((row["id"] for row in rows), default=0) + 1


def _supplier(supplier_id: int) -> Optional[dict]:
    return next((s for s in _suppliers if s["id"] == supplier_id), None)


def _inventory_row(part_id: int) -> Optional[dict]:
    return next((row for row in _inventory if row["partId"] == part_id), None)


def _inventory_view(row: dict) -> dict:
    """库存记录对外形态：partDetail + safetyStock（安全库存取两者较大值，便于演示预警）。"""
    part = _part(row["partId"]) or {}
    return {
        **row,
        "safetyStock": max(row["safetyStock"], part.get("stockWarningValue") or 0),
        "stockValue": round(row["currentQuantity"] * (part.get("purchasePrice") or 0), 2),
        "partDetail": part,
    }


def _page(rows: list[dict], current: int, size: int) -> dict:
    size = max(1, size)
    current = max(1, current)
    total = len(rows)
    pages = (total + size - 1) // size
    start = (current - 1) * size
    return {"records": rows[start:start + size], "total": total, "current": current,
            "size": size, "pages": pages}


def _contains(haystack: Optional[str], needle: Optional[str]) -> bool:
    if not needle:
        return True
    return needle.casefold() in (haystack or "").casefold()


def _in_range(order_time: str, start_date: Optional[str], end_date: Optional[str]) -> bool:
    day = order_time[:10]
    if start_date and day < start_date[:10]:
        return False
    if end_date and day > end_date[:10]:
        return False
    return True


def _not_found(what: str, ident) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} {ident} not found")


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _order_summary(order: dict) -> dict:
    return {key: value for key, value in order.items() if key != "orderDetail"} | {
        "itemCount": len(order["orderDetail"]),
        "statusLabel": ORDER_STATUS.get(order["status"], "未知"),
    }


# --------------------------------------------------------------------------- 订单


@app.post("/api/orders/create")
async def order_create(payload: dict = Body(...)):
    number = payload.get("orderNumber")
    rows = payload.get("orderDetail") or []
    if not number:
        raise _bad_request("orderNumber is required")
    if any(order["orderNumber"] == number for order in _orders):
        raise _bad_request(f"orderNumber {number} already exists")
    if not rows:
        raise _bad_request("orderDetail must not be empty")
    for row in rows:
        if _part(row.get("partId")) is None:
            raise _bad_request(f"partId {row.get('partId')} not found")
        if int(row.get("quantity") or 0) < 1:
            raise _bad_request("quantity must be >= 1")

    order_id = _next_id(_orders)
    total = round(sum(float(row.get("quantity", 0)) * float(row.get("unitPrice", 0)) for row in rows), 2)
    order = {
        "id": order_id,
        "orderNumber": number,
        "totalAmount": payload.get("totalAmount", total),
        "status": int(payload.get("status", 0)),
        "remark": payload.get("remark"),
        "orderTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "orderDetail": [
            {"id": order_id * 100 + index, "partId": row["partId"], "quantity": int(row["quantity"]),
             "unitPrice": float(row.get("unitPrice", 0)), "remark": row.get("remark"),
             "partDetail": _part(row["partId"])}
            for index, row in enumerate(rows, start=1)
        ],
    }
    _orders.append(order)
    return order


@app.put("/api/orders/update/{order_id}")
async def order_update(order_id: int, payload: dict = Body(...)):
    order = next((item for item in _orders if item["id"] == order_id), None)
    if order is None:
        raise _not_found("order", order_id)
    for field in ("totalAmount", "status", "remark"):
        if payload.get(field) is not None:
            order[field] = payload[field]
    if payload.get("orderDetail"):
        order["orderDetail"] = [
            {"id": order_id * 100 + index, "partId": row.get("partId"), "quantity": row.get("quantity"),
             "unitPrice": row.get("unitPrice"), "remark": row.get("remark"),
             "partDetail": _part(row.get("partId"))}
            for index, row in enumerate(payload["orderDetail"], start=1)
        ]
        order["totalAmount"] = round(
            sum(float(row["quantity"]) * float(row["unitPrice"]) for row in payload["orderDetail"]), 2)
    return order


@app.get("/api/orders/page")
async def order_page(
    current: int = Query(1),
    size: int = Query(10),
    orderNumber: Optional[str] = Query(None),
    status: Optional[int] = Query(None),
    startDate: Optional[str] = Query(None),
    endDate: Optional[str] = Query(None),
):
    rows = [
        _order_summary(order) for order in _orders
        if _contains(order["orderNumber"], orderNumber)
        and (status is None or order["status"] == status)
        and _in_range(order["orderTime"], startDate, endDate)
    ]
    rows.sort(key=lambda item: item["orderTime"], reverse=True)
    return _page(rows, current, size)


@app.get("/api/orders/get/{order_id}")
async def order_get(order_id: int):
    order = next((item for item in _orders if item["id"] == order_id), None)
    if order is None:
        raise _not_found("order", order_id)
    return {**_order_summary(order), "orderDetail": order["orderDetail"]}


@app.get("/api/orders/search-details")
async def order_search_details(
    partName: Optional[str] = Query(None),
    startDate: Optional[str] = Query(None),
    endDate: Optional[str] = Query(None),
):
    results = []
    for order in _orders:
        if not _in_range(order["orderTime"], startDate, endDate):
            continue
        for row in order["orderDetail"]:
            part = _part(row["partId"]) or {}
            if not _contains(part.get("name"), partName):
                continue
            supplier = _supplier(part.get("supplierId")) or {}
            results.append({
                "orderId": order["id"], "orderNumber": order["orderNumber"],
                "orderTime": order["orderTime"], "orderStatus": order["status"],
                "partId": row["partId"], "partName": part.get("name"),
                "partCode": part.get("partCode"), "category": part.get("category"),
                "quantity": row["quantity"], "unitPrice": row["unitPrice"],
                "amount": round(row["quantity"] * row["unitPrice"], 2),
                "supplierId": supplier.get("id"), "supplierName": supplier.get("name"),
            })
    results.sort(key=lambda item: item["orderTime"], reverse=True)
    return results


@app.get("/api/orders/statistics")
async def order_statistics(startDate: Optional[str] = Query(None), endDate: Optional[str] = Query(None)):
    rows = [order for order in _orders if _in_range(order["orderTime"], startDate, endDate)]
    by_status = {ORDER_STATUS[key]: 0 for key in ORDER_STATUS}
    by_category: dict[str, float] = {}
    supplier_amount: dict[str, float] = {}
    for order in rows:
        by_status[ORDER_STATUS.get(order["status"], "未知")] = by_status.get(
            ORDER_STATUS.get(order["status"], "未知"), 0) + 1
        for row in order["orderDetail"]:
            part = _part(row["partId"]) or {}
            amount = row["quantity"] * row["unitPrice"]
            category = part.get("category") or "未分类"
            by_category[category] = round(by_category.get(category, 0) + amount, 2)
            supplier = _supplier(part.get("supplierId")) or {}
            name = supplier.get("name") or "未知供应商"
            supplier_amount[name] = round(supplier_amount.get(name, 0) + amount, 2)
    total_amount = round(sum(order["totalAmount"] for order in rows), 2)
    return {
        "orderCount": len(rows),
        "totalAmount": total_amount,
        "averageAmount": round(total_amount / len(rows), 2) if rows else 0.0,
        "byStatus": by_status,
        "byCategory": by_category,
        "topSuppliers": [
            {"supplierName": name, "amount": amount}
            for name, amount in sorted(supplier_amount.items(), key=lambda kv: kv[1], reverse=True)[:5]
        ],
        "startDate": startDate,
        "endDate": endDate,
    }


@app.patch("/api/orders/update-status/{order_id}")
async def order_update_status(order_id: int, status: int = Query(...)):
    order = next((item for item in _orders if item["id"] == order_id), None)
    if order is None:
        raise _not_found("order", order_id)
    if status not in ORDER_STATUS:
        raise _bad_request(f"status must be one of {sorted(ORDER_STATUS)}")
    order["status"] = status
    return {"code": 200, "message": "ok", "data": _order_summary(order)}


# --------------------------------------------------------------------------- 库存


@app.get("/api/inventory/warning")
async def inventory_warning():
    rows = [_inventory_view(row) for row in _inventory]
    return [row for row in rows if row["currentQuantity"] < row["safetyStock"]]


@app.get("/api/inventory/page")
async def inventory_page(
    current: int = Query(1),
    size: int = Query(10),
    partName: Optional[str] = Query(None),
    warehouseLocation: Optional[str] = Query(None),
):
    rows = [
        _inventory_view(row) for row in _inventory
        if _contains((_part(row["partId"]) or {}).get("name"), partName)
        and _contains(row["warehouseLocation"], warehouseLocation)
    ]
    rows.sort(key=lambda item: item["partId"])
    return _page(rows, current, size)


@app.get("/api/inventory/check")
async def inventory_check():
    rows = [_inventory_view(row) for row in _inventory]
    by_warehouse: dict[str, dict] = {}
    for row in rows:
        zone = (row["warehouseLocation"] or "?")[0]
        bucket = by_warehouse.setdefault(zone, {"skuCount": 0, "quantity": 0, "value": 0.0})
        bucket["skuCount"] += 1
        bucket["quantity"] += row["currentQuantity"]
        bucket["value"] = round(bucket["value"] + row["stockValue"], 2)
    return {
        "totalSku": len(rows),
        "warningCount": sum(1 for row in rows if row["currentQuantity"] < row["safetyStock"]),
        "outOfStock": sum(1 for row in rows if row["currentQuantity"] == 0),
        "totalQuantity": sum(row["currentQuantity"] for row in rows),
        "totalValue": round(sum(row["stockValue"] for row in rows), 2),
        "byWarehouse": by_warehouse,
        "checkedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.post("/api/inventory/inbound")
async def inventory_inbound(
    partId: int = Query(...),
    quantity: int = Query(...),
    warehouseLocation: Optional[str] = Query(None),
):
    if quantity <= 0:
        raise _bad_request("quantity must be > 0")
    if _part(partId) is None:
        raise _not_found("part", partId)
    row = _inventory_row(partId)
    if row is None:
        row = {"id": _next_id(_inventory), "partId": partId, "currentQuantity": 0,
               "safetyStock": 0, "warehouseLocation": warehouseLocation or "A-00-00"}
        _inventory.append(row)
    row["currentQuantity"] += quantity
    if warehouseLocation:
        row["warehouseLocation"] = warehouseLocation
    return {"code": 200, "message": "inbound ok", "data": _inventory_view(row)}


@app.post("/api/inventory/outbound")
async def inventory_outbound(partId: int = Query(...), quantity: int = Query(...)):
    if quantity <= 0:
        raise _bad_request("quantity must be > 0")
    row = _inventory_row(partId)
    if row is None:
        raise _not_found("inventory for part", partId)
    if row["currentQuantity"] < quantity:
        raise _bad_request(
            f"insufficient stock for part {partId}: have {row['currentQuantity']}, need {quantity}")
    row["currentQuantity"] -= quantity
    return {"code": 200, "message": "outbound ok", "data": _inventory_view(row)}


@app.get("/api/inventory/get/{inventory_id}")
async def inventory_get(inventory_id: int):
    row = next((item for item in _inventory if item["id"] == inventory_id), None) or _inventory_row(inventory_id)
    if row is None:
        raise _not_found("inventory", inventory_id)
    return _inventory_view(row)


# --------------------------------------------------------------------------- 零部件


@app.get("/api/parts/get/{part_id}")
async def parts_get(part_id: int):
    part = _part(part_id)
    if part is None:
        raise _not_found("part", part_id)
    return {**part, "supplierName": (_supplier(part["supplierId"]) or {}).get("name")}


@app.get("/api/parts/search")
async def parts_search(name: str = Query(...)):
    rows = [
        {**part, "supplierName": (_supplier(part["supplierId"]) or {}).get("name")}
        for part in _parts if _contains(part["name"], name) or _contains(part["partCode"], name)
    ]
    rows.sort(key=lambda item: item["id"])
    return rows


@app.get("/api/parts/supplier/{supplier_id}")
async def parts_by_supplier(supplier_id: int):
    if _supplier(supplier_id) is None:
        raise _not_found("supplier", supplier_id)
    return [{**part} for part in _parts if part["supplierId"] == supplier_id]


@app.get("/api/parts/page")
async def parts_page(
    current: int = Query(1),
    size: int = Query(10),
    name: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    supplierId: Optional[int] = Query(None),
):
    rows = [
        {**part, "supplierName": (_supplier(part["supplierId"]) or {}).get("name")}
        for part in _parts
        if _contains(part["name"], name)
        and _contains(part["category"], category)
        and (supplierId is None or part["supplierId"] == supplierId)
    ]
    rows.sort(key=lambda item: item["id"])
    return _page(rows, current, size)


@app.post("/api/parts/create")
async def parts_create(payload: dict = Body(...)):
    code = payload.get("partCode")
    if not code or not payload.get("name"):
        raise _bad_request("partCode and name are required")
    if any(part["partCode"] == code for part in _parts):
        raise _bad_request(f"partCode {code} already exists")
    part = {
        "id": _next_id(_parts),
        "partCode": code,
        "name": payload["name"],
        "model": payload.get("model"),
        "specification": payload.get("specification"),
        "unit": payload.get("unit"),
        "purchasePrice": float(payload.get("purchasePrice") or 0),
        "suggestedRetailPrice": payload.get("suggestedRetailPrice"),
        "stockWarningValue": payload.get("stockWarningValue"),
        "supplierId": payload.get("supplierId"),
        "category": payload.get("category"),
        "description": payload.get("description"),
    }
    _parts.append(part)
    return part


# --------------------------------------------------------------------------- 供应商


@app.get("/api/suppliers/search")
async def suppliers_search(name: str = Query(...)):
    return [
        {**supplier, "partCount": sum(1 for part in _parts if part["supplierId"] == supplier["id"])}
        for supplier in _suppliers
        if _contains(supplier["name"], name) or _contains(supplier["supplierCode"], name)
    ]


@app.get("/api/suppliers/page")
async def suppliers_page(
    current: int = Query(1),
    size: int = Query(10),
    name: Optional[str] = Query(None),
    status: Optional[int] = Query(None),
    creditRating: Optional[str] = Query(None),
):
    rows = [
        {**supplier, "partCount": sum(1 for part in _parts if part["supplierId"] == supplier["id"])}
        for supplier in _suppliers
        if _contains(supplier["name"], name)
        and (status is None or supplier["status"] == status)
        and (not creditRating or supplier["creditRating"] == creditRating.upper())
    ]
    rows.sort(key=lambda item: item["id"])
    return _page(rows, current, size)


@app.get("/api/suppliers/get/{supplier_id}")
async def suppliers_get(supplier_id: int):
    supplier = _supplier(supplier_id)
    if supplier is None:
        raise _not_found("supplier", supplier_id)
    return {**supplier, "parts": [part for part in _parts if part["supplierId"] == supplier_id]}


@app.post("/api/suppliers/create")
async def suppliers_create(payload: dict = Body(...)):
    code = payload.get("supplierCode")
    if not code or not payload.get("name"):
        raise _bad_request("supplierCode and name are required")
    if any(supplier["supplierCode"] == code for supplier in _suppliers):
        raise _bad_request(f"supplierCode {code} already exists")
    supplier = {
        "id": _next_id(_suppliers),
        "supplierCode": code,
        "name": payload["name"],
        "contactPerson": payload.get("contactPerson"),
        "phone": payload.get("phone"),
        "email": payload.get("email"),
        "address": payload.get("address"),
        "creditRating": (payload.get("creditRating") or "B").upper(),
        "status": int(payload.get("status", 1)),
    }
    _suppliers.append(supplier)
    return supplier


@app.patch("/api/suppliers/update-status/{supplier_id}")
async def suppliers_update_status(supplier_id: int, status: int = Query(...)):
    supplier = _supplier(supplier_id)
    if supplier is None:
        raise _not_found("supplier", supplier_id)
    if status not in (0, 1):
        raise _bad_request("status must be 0 or 1")
    supplier["status"] = status
    return {"code": 200, "message": "ok", "data": supplier}


@app.get("/health")
async def health():
    return {"status": "ok", "parts": len(_parts), "orders": len(_orders),
            "suppliers": len(_suppliers), "inventory": len(_inventory)}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mock ERP backend for the MCP tools")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    import uvicorn

    print(f"Mock ERP listening on http://{args.host}:{args.port} "
          f"({len(_parts)} parts, {len(_suppliers)} suppliers, {len(_orders)} orders)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
