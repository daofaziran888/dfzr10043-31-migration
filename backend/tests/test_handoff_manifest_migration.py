"""v1→v2 交接清单迁移的确定性测试。

覆盖题目全部约束：
- 两份存量数据：三文件齐全包可逐文件比对摘要；缺件旧包仍查得到原摘要并显示缺什么；
- 迁移期间 v1 查询始终可用，旧字段/旧摘要不被改写；
- 缺少原始文件时不能写成已验证；
- 迁移中断一次，恢复后只发布完整清单，接收端读不到半成品（不会一半 v1 一半 v2）；
- 补报缺件后按本批次版本核对，同一清单内校验结果不跨版本串用；
- 版本异常可回滚，回滚后接收端重新读取已有 v1 交接包；
- 文件归集把来源文件与批次校验结果回写交接清单。
"""
from __future__ import annotations

import pytest

from app.manifest import (
    BATCH_COMPLETE,
    BATCH_INCOMPLETE,
    FILE_MISSING,
    FILE_MISMATCH,
    FILE_VERIFIED,
    INTERRUPTED,
    PUBLISHED,
    ROLLED_BACK,
    V1,
    V2,
    ManifestError,
    manifest_store,
)
from app.services import service_for
from app.services.handoff_migration import migration_service

FULL_PACK_ID = 4   # HAND-V1-FULL：3 个原始文件齐全
GAP_PACK_ID = 5    # HAND-V1-GAP：只归集了 2 个，缺 1 个
MISSING_NAME = "gap_manifest.json"


@pytest.fixture(autouse=True)
def reset_manifest():
    manifest_store.reset()
    yield
    manifest_store.reset()


class TestLegacyV1StaysQueryable:
    """v1 旧包摘要与旧字段在整个迁移期间保持可复核。"""

    def test_legacy_summary_fields_preserved(self):
        handoff = service_for("handoff_pack")
        legacy = handoff.get_legacy_summary(FULL_PACK_ID)
        assert legacy is not None
        assert legacy["交接版本"] == V1
        assert legacy["清单文件数"] == "3"
        assert legacy["校验摘要"] == "sha256_legacy_full_pack"

    def test_gap_pack_legacy_summary_still_readable(self):
        handoff = service_for("handoff_pack")
        legacy = handoff.get_legacy_summary(GAP_PACK_ID)
        assert legacy["校验摘要"] == "sha256_legacy_gap_pack"
        # 通用资源列表查询依旧返回 v1 旧字段
        items, total = handoff.list_entries(size=1000)
        assert total >= 5
        row = next(it for it in items if it["id"] == GAP_PACK_ID)
        assert row["清单文件数"] == "3" and row["校验摘要"] == "sha256_legacy_gap_pack"

    def test_reconcile_falls_back_to_v1_before_publish(self):
        view = migration_service.receiver_view(FULL_PACK_ID)
        assert view["active_version"] == V1
        assert view["legacy"]["校验摘要"] == "sha256_legacy_full_pack"


class TestFullPackPerFileVerification:
    """三文件齐全的存量包：v2 清单可逐文件比对摘要。"""

    def test_three_files_compare_and_publish(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        assert manifest["manifest_version"] == V2
        assert manifest["expected_count"] == 3

        summary = migration_service.verify_from_file_drop(manifest["manifest_id"])
        assert summary["complete"] is True
        assert summary["verified"] == 3
        assert summary["missing"] == []

        published = migration_service.publish(manifest["manifest_id"])
        assert published["state"] == PUBLISHED
        assert published["batch_result"] == BATCH_COMPLETE
        assert all(e["status"] == FILE_VERIFIED for e in published["entries"])
        # 每条结果钉死 v2，且保留实际摘要
        assert all(e["verify_version"] == V2 for e in published["entries"])

    def test_receiver_sees_v2_after_publish_with_legacy_attached(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        migration_service.verify_from_file_drop(manifest["manifest_id"])
        migration_service.publish(manifest["manifest_id"])

        view = migration_service.receiver_view(FULL_PACK_ID)
        assert view["active_version"] == V2
        assert view["batch_result"] == BATCH_COMPLETE
        assert len(view["entries"]) == 3
        # 旧摘要随 v2 视图一并保留可复核
        assert view["legacy"]["校验摘要"] == "sha256_legacy_full_pack"

    def test_checksum_mismatch_is_flagged_not_verified(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        file_drop = service_for("file_drop")
        # 人为篡改来源文件校验值，制造摘要不符
        source = next(
            f for f in file_drop.list_entries(size=1000)[0]
            if f["文件名称"] == "full_orders.csv"
        )
        source["校验值"] = "tampered_checksum"
        summary = migration_service.verify_from_file_drop(manifest["manifest_id"])
        assert summary["complete"] is False
        assert "full_orders.csv" in summary["mismatched"]
        with pytest.raises(ManifestError):
            migration_service.publish(manifest["manifest_id"])


class TestGapPackMissingFile:
    """缺件旧包：查得到原摘要、显示缺什么，缺件绝不写成已验证。"""

    def test_missing_file_marked_not_verified(self):
        # 显式声明该批次应交接 3 个原始文件，其中 1 个归集端尚未补报
        manifest = migration_service.start_migration(
            GAP_PACK_ID, expected_file_names=[MISSING_NAME])
        assert manifest["expected_count"] == 3
        gap_entry = next(e for e in manifest["entries"] if e["file_name"] == MISSING_NAME)
        assert gap_entry["status"] == FILE_MISSING

        summary = migration_service.verify_from_file_drop(manifest["manifest_id"])
        assert summary["verified"] == 2
        assert summary["missing"] == [MISSING_NAME]
        # 缺件条目在任何时刻都不能是已验证
        refreshed = manifest_store.get_manifest(manifest["manifest_id"])
        gap_entry = next(e for e in refreshed["entries"] if e["file_name"] == MISSING_NAME)
        assert gap_entry["status"] == FILE_MISSING

    def test_incomplete_manifest_cannot_publish(self):
        manifest = migration_service.start_migration(
            GAP_PACK_ID, expected_file_names=[MISSING_NAME])
        migration_service.verify_from_file_drop(manifest["manifest_id"])
        with pytest.raises(ManifestError):
            migration_service.publish(manifest["manifest_id"])

    def test_receiver_v1_view_reports_what_is_missing(self):
        manifest = migration_service.start_migration(
            GAP_PACK_ID, expected_file_names=[MISSING_NAME])
        migration_service.verify_from_file_drop(manifest["manifest_id"])
        view = migration_service.receiver_view(GAP_PACK_ID)
        # 未发布 → 接收端仍读 v1，旧摘要可查
        assert view["active_version"] == V1
        assert view["legacy"]["校验摘要"] == "sha256_legacy_gap_pack"
        assert view["missing_files"] == [MISSING_NAME]
        assert view["batch_result"] == BATCH_INCOMPLETE


class TestInterruptAndAtomicRecovery:
    """迁移中断一次：恢复只发布完整清单，接收端读不到一半 v1 一半 v2。"""

    def test_interrupted_manifest_invisible_to_receiver(self):
        manifest = migration_service.start_migration(
            GAP_PACK_ID, expected_file_names=[MISSING_NAME])
        migration_service.verify_from_file_drop(manifest["manifest_id"])
        migration_service.interrupt(manifest["manifest_id"], reason="模拟迁移中断")

        mid = manifest["manifest_id"]
        assert manifest_store.get_manifest(mid)["state"] == INTERRUPTED
        # 中断态没有已发布清单，接收端只能看到完整的 v1 回落视图
        assert manifest_store.get_published(GAP_PACK_ID) is None
        view = migration_service.receiver_view(GAP_PACK_ID)
        assert view["active_version"] == V1
        assert view["state"] == INTERRUPTED

    def test_resume_then_supply_then_publish_full_manifest(self):
        manifest = migration_service.start_migration(
            GAP_PACK_ID, expected_file_names=[MISSING_NAME])
        migration_service.verify_from_file_drop(manifest["manifest_id"])
        mid = manifest["manifest_id"]
        migration_service.interrupt(mid, reason="模拟迁移中断")

        # 恢复：按本批次版本重新核对，缺件依旧缺失（不会凭空变已验证）
        summary = migration_service.resume_and_recheck(mid)
        assert summary["missing"] == [MISSING_NAME]
        with pytest.raises(ManifestError):
            migration_service.publish(mid)

        # 归集端补报缺失原始文件，回写清单并按本批次版本核对
        entry = migration_service.supply_missing_file(
            mid, MISSING_NAME, checksum="sha256_gap_manifest_003")
        assert entry["status"] == FILE_VERIFIED
        assert entry["verify_version"] == V2

        # 补报文件确实进入文件归集
        file_drop = service_for("file_drop")
        supplied = [f for f in file_drop.list_entries(size=1000)[0]
                    if f["文件名称"] == MISSING_NAME]
        assert supplied and supplied[0]["status"] == "已归集"

        published = migration_service.publish(mid)
        assert published["state"] == PUBLISHED
        assert published["batch_result"] == BATCH_COMPLETE
        assert len(published["entries"]) == 3
        assert all(e["status"] == FILE_VERIFIED for e in published["entries"])

        # 接收端切换到完整 v2，且 v1 旧摘要仍随视图保留
        view = migration_service.receiver_view(GAP_PACK_ID)
        assert view["active_version"] == V2
        assert view["missing_files"] == []
        assert view["legacy"]["校验摘要"] == "sha256_legacy_gap_pack"

    def test_no_half_published_visible_even_mid_build(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        # 构建过程中（尚未发布）接收端读到的始终是 v1
        view = migration_service.receiver_view(FULL_PACK_ID)
        assert view["active_version"] == V1
        migration_service.verify_from_file_drop(manifest["manifest_id"])
        view = migration_service.receiver_view(FULL_PACK_ID)
        assert view["active_version"] == V1
        assert manifest_store.get_published(FULL_PACK_ID) is None


class TestVersionIsolationAndRollback:
    """版本钉死、禁止跨版本串用，异常可回滚到 v1。"""

    def test_all_entries_pinned_to_manifest_version(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        migration_service.verify_from_file_drop(manifest["manifest_id"])
        migration_service.publish(manifest["manifest_id"])
        published = manifest_store.get_published(FULL_PACK_ID)
        versions = {e["verify_version"] for e in published["entries"]}
        assert versions == {V2}

    def test_cross_version_result_rejected(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        mid = manifest["manifest_id"]
        # 手动伪造一条 v1 结果混入 v2 清单：发布必须被版本一致性自检拦下
        draft = manifest_store.get_manifest(mid)
        for e in draft["entries"]:
            e["status"] = FILE_VERIFIED
            e["actual_checksum"] = e["expected_checksum"]
        draft["entries"][0]["verify_version"] = V1
        with pytest.raises(ManifestError):
            manifest_store.publish(mid)

    def test_rollback_restores_v1_handoff_view(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        mid = manifest["manifest_id"]
        migration_service.verify_from_file_drop(mid)
        migration_service.publish(mid)
        assert migration_service.receiver_view(FULL_PACK_ID)["active_version"] == V2

        rolled_back = migration_service.rollback(mid, reason="接收端核对异常")
        assert rolled_back["state"] == ROLLED_BACK
        assert manifest_store.get_published(FULL_PACK_ID) is None

        view = migration_service.receiver_view(FULL_PACK_ID)
        assert view["active_version"] == V1
        assert view["legacy"]["校验摘要"] == "sha256_legacy_full_pack"
        assert view["legacy"]["清单文件数"] == "3"

    def test_republish_after_rollback_builds_new_manifest(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        mid = manifest["manifest_id"]
        migration_service.verify_from_file_drop(mid)
        migration_service.publish(mid)
        migration_service.rollback(mid)
        # 回滚后允许重新迁移；已发布状态下重复构建则被拒绝
        new_manifest = migration_service.start_migration(FULL_PACK_ID)
        assert new_manifest["manifest_id"] != mid

    def test_double_publish_while_active_is_rejected(self):
        manifest = migration_service.start_migration(FULL_PACK_ID)
        migration_service.verify_from_file_drop(manifest["manifest_id"])
        migration_service.publish(manifest["manifest_id"])
        with pytest.raises(ManifestError):
            migration_service.start_migration(FULL_PACK_ID)


class TestIdempotentResumeAndBatchWriteback:
    """中断恢复幂等，文件归集结果回写清单形成批次校验结论。"""

    def test_start_migration_is_idempotent_on_draft(self):
        first = migration_service.start_migration(FULL_PACK_ID)
        second = migration_service.start_migration(FULL_PACK_ID)
        assert first["manifest_id"] == second["manifest_id"]

    def test_batch_result_writeback_only_on_complete_publish(self):
        manifest = migration_service.start_migration(
            GAP_PACK_ID, expected_file_names=[MISSING_NAME])
        mid = manifest["manifest_id"]
        migration_service.verify_from_file_drop(mid)
        # 发布前批次结论为空，不能宣称已验证
        assert manifest_store.get_manifest(mid)["batch_result"] is None
        migration_service.interrupt(mid)
        migration_service.resume_and_recheck(mid)
        migration_service.supply_missing_file(mid, MISSING_NAME, "sha256_gap_manifest_003")
        published = migration_service.publish(mid)
        # 批次校验结果随文件归集回写后落定
        assert published["batch_result"] == BATCH_COMPLETE
        assert published["missing_files"] == []
