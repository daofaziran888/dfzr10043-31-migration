"""交接清单 v1→v2 迁移接口。

固定路径放在 /api/handoff-migration/ 下，与通用资源路由 /api/{module} 区分，
覆盖：开启迁移、逐文件回写、补报缺件、中断、恢复、发布、回滚、接收端核对视图。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.manifest import V2, ManifestError
from app.services.handoff_migration import migration_service
from app.services import service_for

router = APIRouter(prefix="/api/handoff-migration", tags=["交接清单迁移"])


class StartMigrationPayload(BaseModel):
    pack_id: int
    expected_file_names: list[str] = Field(default_factory=list)
    version: str = V2


class VerifyPayload(BaseModel):
    pass


class SupplyFilePayload(BaseModel):
    file_name: str
    checksum: str
    run_code: str | None = None


class ReasonPayload(BaseModel):
    reason: str = "迁移中断"


def _error(exc: ManifestError) -> dict[str, Any]:
    return {"ok": False, "reason": str(exc)}


@router.post("/start")
def start_migration(payload: StartMigrationPayload) -> dict[str, Any]:
    try:
        manifest = migration_service.start_migration(
            payload.pack_id, payload.expected_file_names, version=payload.version)
    except ManifestError as exc:
        return _error(exc)
    return {"ok": True, "manifest": manifest}


@router.post("/{manifest_id}/verify")
def verify_manifest(manifest_id: int) -> dict[str, Any]:
    try:
        summary = migration_service.verify_from_file_drop(manifest_id)
    except ManifestError as exc:
        return _error(exc)
    return {"ok": True, "summary": summary}


@router.post("/{manifest_id}/supply")
def supply_missing_file(manifest_id: int, payload: SupplyFilePayload) -> dict[str, Any]:
    try:
        entry = migration_service.supply_missing_file(
            manifest_id, payload.file_name, payload.checksum, payload.run_code)
    except ManifestError as exc:
        return _error(exc)
    return {"ok": True, "entry": entry}


@router.post("/{manifest_id}/interrupt")
def interrupt_manifest(manifest_id: int, payload: ReasonPayload) -> dict[str, Any]:
    try:
        manifest = migration_service.interrupt(manifest_id, payload.reason)
    except ManifestError as exc:
        return _error(exc)
    return {"ok": True, "manifest": manifest}


@router.post("/{manifest_id}/resume")
def resume_manifest(manifest_id: int) -> dict[str, Any]:
    try:
        summary = migration_service.resume_and_recheck(manifest_id)
    except ManifestError as exc:
        return _error(exc)
    return {"ok": True, "summary": summary}


@router.post("/{manifest_id}/publish")
def publish_manifest(manifest_id: int) -> dict[str, Any]:
    try:
        manifest = migration_service.publish(manifest_id)
    except ManifestError as exc:
        return _error(exc)
    return {"ok": True, "manifest": manifest}


@router.post("/{manifest_id}/rollback")
def rollback_manifest(manifest_id: int, payload: ReasonPayload) -> dict[str, Any]:
    try:
        manifest = migration_service.rollback(manifest_id, payload.reason)
    except ManifestError as exc:
        return _error(exc)
    return {"ok": True, "manifest": manifest}


@router.get("/manifests")
def list_manifests(pack_id: int | None = None) -> dict[str, Any]:
    return {"ok": True, "items": migration_service.list_migration(pack_id)}


@router.get("/packs/{pack_id}/reconcile")
def receiver_reconcile(pack_id: int) -> dict[str, Any]:
    """接收端核对视图：完整 v2 或回落 v1（含缺件清单），永不返回半成品。"""
    handoff = service_for("handoff_pack")
    return handoff.receiver_reconcile_view(pack_id)


@router.get("/packs/{pack_id}/legacy")
def legacy_summary(pack_id: int) -> dict[str, Any]:
    """v1 旧摘要复核：旧包摘要和已有字段始终可查。"""
    handoff = service_for("handoff_pack")
    legacy = handoff.get_legacy_summary(pack_id)
    if legacy is None:
        return {"ok": False, "reason": "交接包不存在"}
    return {"ok": True, "pack_id": pack_id, "legacy": legacy}
