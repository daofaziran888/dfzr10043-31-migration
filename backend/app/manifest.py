"""交接清单版本管理器：v1 汇总交接包 → v2 逐文件可验证清单的迁移内核。

设计约束（对应迁移需求）：

1. v1 交接包只有「清单文件数 + 校验摘要」两个汇总字段，迁移期间接收端仍按
   旧字段核对，因此 v1 记录始终保留、旧字段查询不被破坏。
2. v2 清单逐文件登记来源文件摘要与批次校验结果，每个文件条目带校验状态；
   缺少原始文件时只能标记为「缺失」，绝不允许写成「已验证」。
3. 清单构建可中断、可恢复：恢复后只发布完整清单。构建态（building/interrupted）
   对接收端不可见，读取方永远看不到半成品，杜绝同一清单一半 v1 一半 v2。
4. 版本隔离：每条逐文件校验结果都钉死在产生它的清单版本（v1/v2）上，
   恢复时按本批次版本重新核对，不允许跨版本串用校验结果。
5. 版本异常可回滚：回滚后读取方重新看到已有 v1 交接包的旧摘要与旧字段。

本模块只做状态机与规则，不依赖 Web 框架，便于确定性测试。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

# 清单版本
V1 = "v1"
V2 = "v2"

# 构建/发布状态：只有 published 对接收端可见
BUILDING = "building"
INTERRUPTED = "interrupted"
PUBLISHED = "published"
ROLLED_BACK = "rolled_back"

# 逐文件校验状态
FILE_VERIFIED = "已验证"      # 原始文件存在且摘要逐文件比对一致
FILE_MISSING = "缺失"         # 原始文件缺件：禁止写成已验证
FILE_MISMATCH = "摘要不符"    # 原始文件存在但摘要与清单登记值不一致

# 批次级校验结果（v2 逐文件核对后的整体结论）
BATCH_COMPLETE = "逐文件核对通过"
BATCH_INCOMPLETE = "存在缺件未完成"


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


class ManifestError(RuntimeError):
    """清单迁移规则违例。"""


class ManifestStore:
    """线程安全的清单内存仓库，构建态与发布态物理隔离。

    - ``_manifests``：构建/历史清单（含 building/interrupted），接收端不读这里。
    - ``_published_index``：pack_id → 已发布清单，接收端只从这里取可见结果。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._manifests: dict[int, dict[str, Any]] = {}
        self._published_index: dict[int, dict[str, Any]] = {}
        self._seq = 0

    def reset(self) -> None:
        with self._lock:
            self._manifests.clear()
            self._published_index.clear()
            self._seq = 0

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    # ── 构建 ──────────────────────────────────────────────────

    def begin_manifest(
        self,
        pack: dict[str, Any],
        expected_files: list[dict[str, Any]],
        *,
        version: str = V2,
    ) -> dict[str, Any]:
        """为一个 v1 交接包开启 v2 清单构建。

        幂等：同一 pack 已有构建中/已中断清单时直接返回该草稿，不重复开建；
        已发布且未回滚的包拒绝重复构建，避免覆盖接收端正在消费的结果。
        """
        pack_id = int(pack["id"])
        with self._lock:
            existing = self._find_build_draft(pack_id)
            if existing is not None:
                return existing
            if pack_id in self._published_index:
                raise ManifestError(f"交接包 {pack_id} 已发布 v2 清单，不能重复构建")

            manifest_id = self._next_id()
            manifest = {
                "manifest_id": manifest_id,
                "pack_id": pack_id,
                "handoff_code": pack.get("交接编号", ""),
                "batch_code": pack.get("关联批次", ""),
                "target_system": pack.get("目标系统", ""),
                "manifest_version": version,
                "state": BUILDING,
                # 冻结 v1 旧字段：旧摘要与汇总文件数始终可复核、可回滚
                "v1_snapshot": {
                    "交接版本": pack.get("交接版本", V1),
                    "清单文件数": pack.get("清单文件数", ""),
                    "校验摘要": pack.get("校验摘要", ""),
                    "记录行数": pack.get("记录行数", ""),
                },
                "expected_count": len(expected_files),
                "entries": [self._draft_entry(version, idx, f)
                            for idx, f in enumerate(expected_files)],
                "batch_result": None,
                "missing_files": [],
                "published_at": None,
                "updated_at": _now_iso(),
            }
            self._manifests[manifest_id] = manifest
            return dict(manifest)

    @staticmethod
    def _draft_entry(version: str, index: int, f: dict[str, Any]) -> dict[str, Any]:
        # 每条结果钉死版本，杜绝跨版本串用
        return {
            "entry_id": index + 1,
            "file_name": f.get("file_name", ""),
            "file_drop_id": f.get("file_drop_id"),
            "expected_checksum": f.get("expected_checksum", ""),
            "actual_checksum": None,
            "verify_version": version,
            "status": FILE_MISSING,
            "detail": "等待逐文件核对",
        }

    def _find_build_draft(self, pack_id: int) -> dict[str, Any] | None:
        for m in self._manifests.values():
            if m["pack_id"] == pack_id and m["state"] in (BUILDING, INTERRUPTED):
                return m
        return None

    # ── 逐文件核对（文件归集回写） ─────────────────────────────

    def verify_entry(
        self,
        manifest_id: int,
        file_drop_id: int | None,
        file_name: str,
        actual_checksum: str | None,
    ) -> dict[str, Any]:
        """核对单个来源文件并回写清单条目。

        规则：
        - actual_checksum 为空/来源文件不存在 → 维持「缺失」，不得标已验证；
        - 摘要一致 → 「已验证」；不一致 → 「摘要不符」；
        - 只允许在构建态/中断态草稿上核对，已发布清单不可改；
        - 结果版本必须与清单版本一致，跨版本写入直接拒绝。
        """
        with self._lock:
            manifest = self._require_manifest(manifest_id)
            self._assert_writable(manifest)
            entry = self._locate_entry(manifest, file_drop_id, file_name)

            if not actual_checksum:
                entry["status"] = FILE_MISSING
                entry["actual_checksum"] = None
                entry["detail"] = "原始文件缺件，未通过逐文件验证"
            elif actual_checksum == entry["expected_checksum"]:
                entry["status"] = FILE_VERIFIED
                entry["actual_checksum"] = actual_checksum
                entry["detail"] = "逐文件摘要比对一致"
            else:
                entry["status"] = FILE_MISMATCH
                entry["actual_checksum"] = actual_checksum
                entry["detail"] = "实际摘要与清单登记摘要不一致"

            # 版本钉死：写入结果的版本必须等于清单版本
            if entry["verify_version"] != manifest["manifest_version"]:
                raise ManifestError("校验结果版本与清单版本不一致，禁止跨版本串用")
            manifest["updated_at"] = _now_iso()
            return dict(entry)

    @staticmethod
    def _locate_entry(
        manifest: dict[str, Any],
        file_drop_id: int | None,
        file_name: str,
    ) -> dict[str, Any]:
        for e in manifest["entries"]:
            if file_drop_id is not None and e.get("file_drop_id") == file_drop_id:
                return e
        for e in manifest["entries"]:
            if e["file_name"] == file_name:
                return e
        raise ManifestError(
            f"清单 {manifest['manifest_id']} 中找不到文件 {file_name}（file_drop_id={file_drop_id}）"
        )

    # ── 中断与恢复 ────────────────────────────────────────────

    def interrupt(self, manifest_id: int, reason: str = "迁移中断") -> dict[str, Any]:
        """把构建中的清单标记为中断。中断态对接收端仍不可见。"""
        with self._lock:
            manifest = self._require_manifest(manifest_id)
            if manifest["state"] != BUILDING:
                raise ManifestError(f"清单当前状态 {manifest['state']}，不能中断")
            manifest["state"] = INTERRUPTED
            manifest["interrupt_reason"] = reason
            manifest["updated_at"] = _now_iso()
            return dict(manifest)

    def resume(self, manifest_id: int) -> dict[str, Any]:
        """恢复中断的清单，回到构建态继续核对。"""
        with self._lock:
            manifest = self._require_manifest(manifest_id)
            if manifest["state"] != INTERRUPTED:
                raise ManifestError(f"清单当前状态 {manifest['state']}，无需恢复")
            manifest["state"] = BUILDING
            manifest.pop("interrupt_reason", None)
            manifest["updated_at"] = _now_iso()
            return dict(manifest)

    # ── 发布：只发布完整清单（原子切换） ───────────────────────

    def publish(self, manifest_id: int) -> dict[str, Any]:
        """原子发布完整清单。

        - 存在缺件或摘要不符 → 拒绝发布，保持草稿可恢复；
        - 全部条目「已验证」→ 计算批次校验结果，整体替换发布索引（原子），
          接收端要么读到旧 v1、要么读到完整 v2，不会读到半成品。
        """
        with self._lock:
            manifest = self._require_manifest(manifest_id)
            self._assert_writable(manifest)

            missing = [e["file_name"] for e in manifest["entries"]
                       if e["status"] == FILE_MISSING]
            mismatched = [e["file_name"] for e in manifest["entries"]
                          if e["status"] == FILE_MISMATCH]
            if missing or mismatched:
                raise ManifestError(
                    "清单不完整，禁止发布："
                    f"缺件 {len(missing)} 个 {missing}；摘要不符 {len(mismatched)} 个 {mismatched}"
                )

            verified = [e for e in manifest["entries"] if e["status"] == FILE_VERIFIED]
            if len(verified) != manifest["expected_count"] or not verified:
                raise ManifestError("清单未全部逐文件验证，禁止发布")

            # 版本一致性自检：同一清单内校验结果不允许跨版本
            versions = {e["verify_version"] for e in verified}
            if versions != {manifest["manifest_version"]}:
                raise ManifestError("清单内存在跨版本校验结果，禁止发布")

            manifest["batch_result"] = BATCH_COMPLETE
            manifest["missing_files"] = []
            manifest["state"] = PUBLISHED
            manifest["published_at"] = _now_iso()
            manifest["updated_at"] = manifest["published_at"]

            # 原子切换：先完整复制，再一次性放进发布索引
            published = dict(manifest)
            published["entries"] = [dict(e) for e in manifest["entries"]]
            self._published_index[manifest["pack_id"]] = published
            return dict(published)

    # ── 回滚：接收端重新读 v1 交接包 ──────────────────────────

    def rollback(self, manifest_id: int, reason: str = "版本异常回滚") -> dict[str, Any]:
        """回滚已发布的 v2 清单：从发布索引摘除，接收端回落 v1 旧包。"""
        with self._lock:
            manifest = self._require_manifest(manifest_id)
            if manifest["state"] != PUBLISHED:
                raise ManifestError(f"清单当前状态 {manifest['state']}，不能回滚")
            manifest["state"] = ROLLED_BACK
            manifest["rollback_reason"] = reason
            manifest["updated_at"] = _now_iso()
            self._published_index.pop(manifest["pack_id"], None)
            return dict(manifest)

    # ── 接收端读取：永远只读完整结果，不会读到一半 ─────────────

    def get_published(self, pack_id: int) -> dict[str, Any] | None:
        """接收端视图：仅返回已发布的完整清单，构建/中断态返回 None。"""
        with self._lock:
            published = self._published_index.get(int(pack_id))
            if published is None:
                return None
            return dict(published)

    def get_manifest(self, manifest_id: int) -> dict[str, Any] | None:
        with self._lock:
            m = self._manifests.get(int(manifest_id))
            return dict(m) if m else None

    def list_manifests(self, pack_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._manifests.values())
            if pack_id is not None:
                items = [m for m in items if m["pack_id"] == int(pack_id)]
            return [dict(m) for m in items]

    def reconcile_view(self, pack: dict[str, Any]) -> dict[str, Any]:
        """接收端核对视图。

        - 已发布 v2：返回逐文件校验结果 + 批次结论，同时携带 v1 旧摘要备查；
        - 未发布/已回滚：回落 v1，返回旧摘要与旧字段，并显式给出缺件清单，
          缺件旧包依然查得到原摘要，且明确显示缺了什么。
        """
        pack_id = int(pack["id"])
        with self._lock:
            published = self._published_index.get(pack_id)
            base_v1 = {
                "pack_id": pack_id,
                "handoff_code": pack.get("交接编号", ""),
                "active_version": V1,
                "legacy": {
                    "交接版本": pack.get("交接版本", V1),
                    "清单文件数": pack.get("清单文件数", ""),
                    "校验摘要": pack.get("校验摘要", ""),
                    "记录行数": pack.get("记录行数", ""),
                },
            }
            if published is not None:
                return {
                    **base_v1,
                    "active_version": V2,
                    "manifest_id": published["manifest_id"],
                    "manifest_version": published["manifest_version"],
                    "state": PUBLISHED,
                    "batch_result": published["batch_result"],
                    "missing_files": [],
                    "entries": [dict(e) for e in published["entries"]],
                    "published_at": published["published_at"],
                }

            # v1 回落：若存在草稿，汇总当前缺件，告诉接收端「缺什么」
            missing: list[str] = []
            draft_state = None
            draft_id = None
            draft = self._find_build_draft(pack_id)
            if draft is not None:
                draft_id = draft["manifest_id"]
                draft_state = draft["state"]
                missing = [e["file_name"] for e in draft["entries"]
                           if e["status"] == FILE_MISSING]
            return {
                **base_v1,
                "state": draft_state or V1,
                "manifest_id": draft_id,
                "batch_result": BATCH_INCOMPLETE if missing else None,
                "missing_files": missing,
            }

    # ── 内部工具 ──────────────────────────────────────────────

    def _require_manifest(self, manifest_id: int) -> dict[str, Any]:
        manifest = self._manifests.get(int(manifest_id))
        if manifest is None:
            raise ManifestError(f"清单 {manifest_id} 不存在")
        return manifest

    @staticmethod
    def _assert_writable(manifest: dict[str, Any]) -> None:
        if manifest["state"] not in (BUILDING, INTERRUPTED):
            raise ManifestError(f"清单状态 {manifest['state']}，不可修改")


# 进程级单例；测试通过 reset() 隔离
manifest_store = ManifestStore()
