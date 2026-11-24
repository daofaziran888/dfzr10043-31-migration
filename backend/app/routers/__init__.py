"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import handoff_migration, resource

# 迁移路由为固定前缀，先于通用 /api/{module} 动态路由注册，避免路径抢先匹配
ROUTERS = [handoff_migration, resource]
