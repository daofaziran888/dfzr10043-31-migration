"""交接清单迁移编排：把 v1 交接包迁移为可逐文件验证的 v2 清单。

编排职责：
- 从 v1 交接包 + 文件归集（file_drop）构造 v2 清单的期望文件与登记摘要；
- 文件归集把核对后的来源文件、批次校验结果回写清单；
- 处理补报原始文件、迁移中断/恢复、只发布完整清单；
- 为接收端提供统一核对视图（v2 优先，异常回滚 v1）。

版本隔离原则：所有逐文件核对都通过 manifest 内核完成，结果自带版本标记，
本服务不跨批次、不跨版本复用任何校验结果。
"""
from __future__ import annotations

from typing import Any

from app.manifest import (
    BATCH_INCOMPLETE,
    BUILDING,
    FILE_MISSING,
    FILE_VERIFIED,
    INTERRUPTED,
    PUBLISHED,
    V1,
    V2,
    ManifestError,
    manifest_store,
)
from app.services import service_for


class HandoffMigrationService:
    """v1 → v2 交接清单迁移用例服务。"""

    def __init__(self) -> None:
        self.store = manifest_store
        self._handoff = service_for("handoff_pack")
        self._file_drop = service_for("file_drop")
        self._capture_batch = service_for("capture_batch")

    # ── 期望清单构造 ──────────────────────────────────────────

    def expected_files_for_pack(self, pack_id: int) -> list[dict[str, Any]]:
        """根据 v1 交接包关联批次的归集文件，生成 v2 期望文件清单。

        每个期望文件带上 file_drop 主键与登记校验值（expected_checksum）。
        归集端尚未补报的原始文件不会出现在 file_drop 中，调用方需通过
        ``expected_file_names`` 显式声明，以便标记缺件。
        """
        pack = self._require_pack(pack_id)
        batch_code = pack.get("关联批次", "")
        files = self._files_for_batch(batch_code)
        return [
            {
                "file_name": f.get("文件名称", ""),
                "file_drop_id": f.get("id"),
                "expected_checksum": f.get("校验值", ""),
            }
            for f in files
        ]

    def _files_for_batch(self, batch_code: str) -> list[dict[str, Any]]:
        """通过 采集批次.关联执行 反查该批次的归集文件。"""
        batches, _ = self._capture_batch.list_entries(size=10000)
        run_code = ""
        for b in batches:
            if b.get("批次编号") == batch_code:
                run_code = b.get("关联执行", "")
                break
        files, _ = self._file_drop.list_entries(size=10000)
        if run_code:
            files = [f for f in files if f.get("关联执行") == run_code]
        return files

    # ── 迁移主流程 ────────────────────────────────────────────

    def start_migration(
        self,
        pack_id: int,
        expected_file_names: list[str] | None = None,
        *,
        version: str = V2,
    ) -> dict[str, Any]:
        """开启迁移：冻结 v1 旧字段，建立 v2 逐文件清单草稿。

        expected_file_names 用于声明该批次应交接的全部原始文件名；
        归集端尚未补报的文件会以「缺失」条目进入清单（无 file_drop_id）。
        """
        pack = self._require_pack(pack_id)
        known = self.expected_files_for_pack(pack_id)
        known_names = {f["file_name"] for f in known}

        expected = list(known)
        for name in expected_file_names or []:
            if name not in known_names:
                # 缺件：登记期望文件名，但没有来源文件与实际摘要
                expected.append({
                    "file_name": name,
                    "file_drop_id": None,
                    "expected_checksum": "",
                })
        return self.store.begin_manifest(pack, expected, version=version)

    def verify_from_file_drop(self, manifest_id: int) -> dict[str, Any]:
        """用文件归集里核对后的来源文件，逐条回写清单核对结果。

        归集文件状态为「校验不符」或没有校验值时不视为可验证来源；
        缺件条目保持「缺失」，绝不写成已验证。
        """
        manifest = self.store.get_manifest(manifest_id)
        if manifest is None:
            raise ManifestError(f"清单 {manifest_id} 不存在")
        results = []
        for entry in manifest["entries"]:
            source = self._lookup_source(entry)
            actual = None
            if source is not None and source.get("status") != "校验不符":
                actual = source.get("校验值") or None
            results.append(
                self.store.verify_entry(
                    manifest_id,
                    entry.get("file_drop_id"),
                    entry["file_name"],
                    actual,
                )
            )
        return self._summary(manifest_id, results)

    def supply_missing_file(
        self,
        manifest_id: int,
        file_name: str,
        checksum: str,
        run_code: str | None = None,
    ) -> dict[str, Any]:
        """归集端补报迁移缺失的原始文件：先入归集，再回写清单核对。

        补报文件登记进 file_drop（状态已归集），随后按本清单版本重新核对；
        摘要与期望一致才能转「已验证」，缺件期间不会产生任何已验证结论。
        """
        manifest = self.store.get_manifest(manifest_id)
        if manifest is None:
            raise ManifestError(f"清单 {manifest_id} 不存在")
        entry = self._entry_by_name(manifest, file_name)

        # 落归集：补报原始文件，校验值即登记摘要
        values = {
            "归集编号": f"FILE-SUPPLY-{manifest_id}-{entry['entry_id']}",
            "关联执行": run_code or self._run_code_of(manifest),
            "文件名称": file_name,
            "文件类型": "application/octet-stream",
            "字节大小": "0",
            "校验值": checksum,
            "归集目录": f"/captures/supply/{manifest['handoff_code']}",
            "归集时间": manifest["updated_at"],
        }
        created, missing = self._file_drop.create_entry(values)
        if missing:
            raise ManifestError(f"补报文件登记失败，缺字段：{missing}")
        self._file_drop.run_action(created["id"], "触发归集")

        # 回填清单期望摘要并按本批次版本核对
        entry["expected_checksum"] = checksum
        entry["file_drop_id"] = created["id"]
        return self.store.verify_entry(manifest_id, created["id"], file_name, checksum)

    # ── 中断 / 恢复 / 发布 / 回滚 ─────────────────────────────

    def interrupt(self, manifest_id: int, reason: str = "迁移中断") -> dict[str, Any]:
        return self.store.interrupt(manifest_id, reason)

    def resume_and_recheck(self, manifest_id: int) -> dict[str, Any]:
        """中断恢复：回到构建态，并按本清单版本重新核对全部条目。

        重新核对只使用当前清单版本的来源数据，历史条目结果若来源消失
        会重新落回「缺失」，保证不跨版本串用。
        """
        self.store.resume(manifest_id)
        return self.verify_from_file_drop(manifest_id)

    def publish(self, manifest_id: int) -> dict[str, Any]:
        return self.store.publish(manifest_id)

    def rollback(self, manifest_id: int, reason: str = "版本异常回滚") -> dict[str, Any]:
        return self.store.rollback(manifest_id, reason)

    # ── 接收端视图 ────────────────────────────────────────────

    def receiver_view(self, pack_id: int) -> dict[str, Any]:
        pack = self._require_pack(pack_id)
        view = self.store.reconcile_view(pack)
        # v1 回落时把批次级结论显式化：缺件旧包不能显示为已验证
        if view["active_version"] == V1 and view.get("missing_files"):
            view["batch_result"] = BATCH_INCOMPLETE
        return view

    def list_migration(self, pack_id: int | None = None) -> list[dict[str, Any]]:
        return self.store.list_manifests(pack_id)

    # ── 内部工具 ──────────────────────────────────────────────

    def _require_pack(self, pack_id: int) -> dict[str, Any]:
        pack = self._handoff.get_entry(pack_id)
        if pack is None:
            raise ManifestError(f"交接包 {pack_id} 不存在")
        return pack

    def _lookup_source(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        fid = entry.get("file_drop_id")
        if fid is None:
            return None
        return self._file_drop.get_entry(int(fid))

    @staticmethod
    def _entry_by_name(manifest: dict[str, Any], file_name: str) -> dict[str, Any]:
        for e in manifest["entries"]:
            if e["file_name"] == file_name:
                return e
        raise ManifestError(f"清单中找不到期望文件 {file_name}")

    def _run_code_of(self, manifest: dict[str, Any]) -> str:
        batches, _ = self._capture_batch.list_entries(size=10000)
        for b in batches:
            if b.get("批次编号") == manifest.get("batch_code"):
                return b.get("关联执行", "")
        return ""

    @staticmethod
    def _summary(manifest_id: int, results: list[dict[str, Any]]) -> dict[str, Any]:
        verified = sum(1 for r in results if r["status"] == FILE_VERIFIED)
        missing = [r["file_name"] for r in results if r["status"] == FILE_MISSING]
        mismatched = [r["file_name"] for r in results if r["status"] not in
                      (FILE_VERIFIED, FILE_MISSING)]
        return {
            "manifest_id": manifest_id,
            "total": len(results),
            "verified": verified,
            "missing": missing,
            "mismatched": mismatched,
            "complete": not missing and not mismatched,
        }


migration_service = HandoffMigrationService()
