"""业务路由汇总：追溯固定路径优先注册，通用资源路由兜底。"""
from __future__ import annotations

from app.routers import resource, traceability

# traceability 含 /api/traceability/... 固定路径，必须排在动态 /api/{module} 之前。
ROUTERS = [traceability, resource]
