"""业务路由汇总：回归准备专用路由先注册，随后是所有模块共用的资源路由。"""
from __future__ import annotations

from app.routers import regression_api, resource

ROUTERS = [regression_api, resource]
