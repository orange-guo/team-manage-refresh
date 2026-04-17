"""体验组队服务"""
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExperienceAssignment, ExperienceQueue, Team
from app.services.team import TeamService
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


class ExperienceService:
    """体验组队自动拉人 + 自动移出 + 排队"""

    def __init__(self):
        self.team_service = TeamService()
        self.max_members_per_team = 5
        self.duration_minutes = 10

    @staticmethod
    def _normalize_email(email: str) -> str:
        return (email or "").strip().lower()

    @staticmethod
    def _remaining_seconds(expires_at, now) -> int:
        return max(0, int((expires_at - now).total_seconds()))

    @staticmethod
    def _is_full_error(error_msg: str) -> bool:
        error_msg = (error_msg or "").strip()
        lower = error_msg.lower()
        return (
            "已满" in error_msg
            or "seat" in lower
            or "full" in lower
            or "maximum number of seats" in lower
        )

    @staticmethod
    def _is_duplicate_like_error(error_msg: str) -> bool:
        error_msg = (error_msg or "").strip()
        lower = error_msg.lower()
        return (
            "已在" in error_msg
            or "already" in lower
            or "invited" in lower
            or "重复" in error_msg
        )

    def _team_capacity_limit(self, team: Team) -> int:
        return min(max(0, int(team.max_members or 0)), self.max_members_per_team)

    async def _get_active_assignment(self, email: str, db_session: AsyncSession) -> Optional[ExperienceAssignment]:
        now = get_now()
        stmt = (
            select(ExperienceAssignment)
            .where(
                ExperienceAssignment.email == email,
                ExperienceAssignment.status == "active",
                ExperienceAssignment.expires_at > now,
            )
            .order_by(ExperienceAssignment.id.desc())
            .limit(1)
        )
        result = await db_session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_queue_item(self, email: str, db_session: AsyncSession) -> Optional[ExperienceQueue]:
        stmt = (
            select(ExperienceQueue)
            .where(
                ExperienceQueue.email == email,
                ExperienceQueue.status == "queued",
            )
            .order_by(ExperienceQueue.id.asc())
            .limit(1)
        )
        result = await db_session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_queue_position(self, queue_item_id: int, db_session: AsyncSession) -> Optional[int]:
        stmt = (
            select(func.count())
            .select_from(ExperienceQueue)
            .where(
                ExperienceQueue.status == "queued",
                ExperienceQueue.id <= queue_item_id,
            )
        )
        result = await db_session.execute(stmt)
        count_val = result.scalar_one_or_none()
        if count_val is None:
            return None
        return int(count_val)

    async def _try_assign_one_now(self, email: str, db_session: AsyncSession) -> Dict[str, Any]:
        """尝试立刻把邮箱拉进可用 Team。"""
        now = get_now()

        stmt = (
            select(Team)
            .where(Team.pool_type == "welfare", Team.status.in_(["active", "full"]))
            .order_by(Team.id.asc())
        )
        result = await db_session.execute(stmt)
        teams = result.scalars().all()

        if not teams:
            return {"assigned": False, "all_full": True, "error": None}

        full_like_count = 0
        valid_team_count = 0
        last_error: Optional[str] = None

        for team in teams:
            if team.expires_at and team.expires_at <= now:
                continue

            valid_team_count += 1
            cap = self._team_capacity_limit(team)
            if cap <= 0 or int(team.current_members or 0) >= cap:
                full_like_count += 1
                continue

            add_result = await self.team_service.add_team_member(team.id, email, db_session)
            if add_result.get("success"):
                expires_at = get_now() + timedelta(minutes=self.duration_minutes)
                assignment = ExperienceAssignment(
                    email=email,
                    team_id=team.id,
                    expires_at=expires_at,
                    status="active",
                )
                db_session.add(assignment)
                await db_session.flush()

                return {
                    "assigned": True,
                    "assignment": assignment,
                    "team": team,
                    "all_full": False,
                    "error": None,
                }

            error_msg = (add_result.get("error") or "").strip()

            if self._is_full_error(error_msg):
                full_like_count += 1
                continue

            if self._is_duplicate_like_error(error_msg):
                continue

            last_error = error_msg or "拉入失败"
            logger.warning("体验组队拉人失败，Team=%s, email=%s, error=%s", team.id, email, last_error)

        if valid_team_count == 0:
            return {"assigned": False, "all_full": True, "error": None}

        if full_like_count >= valid_team_count:
            return {"assigned": False, "all_full": True, "error": None}

        return {"assigned": False, "all_full": False, "error": last_error}

    async def get_total_available_slots(self, db_session: AsyncSession) -> int:
        """获取体验池剩余席位总数（单账号最多按 5 人计算）"""
        now = get_now()
        stmt = (
            select(Team)
            .where(Team.pool_type == "welfare", Team.status.in_(["active", "full"]))
            .order_by(Team.id.asc())
        )
        result = await db_session.execute(stmt)
        teams = result.scalars().all()

        total = 0
        for team in teams:
            if team.expires_at and team.expires_at <= now:
                continue
            cap = self._team_capacity_limit(team)
            if cap <= 0:
                continue
            total += max(0, cap - int(team.current_members or 0))

        return total

    async def get_active_entries(self, db_session: AsyncSession) -> Dict[str, Any]:
        """获取当前体验池中已加入邮箱（公开展示用）"""
        now = get_now()
        stmt = (
            select(ExperienceAssignment, Team)
            .join(Team, Team.id == ExperienceAssignment.team_id)
            .where(
                ExperienceAssignment.status == "active",
                ExperienceAssignment.expires_at > now,
            )
            .order_by(ExperienceAssignment.expires_at.asc(), ExperienceAssignment.id.asc())
        )
        result = await db_session.execute(stmt)
        rows = result.all()

        items = []
        for assignment, team in rows:
            items.append(
                {
                    "email": assignment.email,
                    "team_id": assignment.team_id,
                    "team_email": team.email if team else None,
                    "expires_at": assignment.expires_at.isoformat(),
                    "seconds_remaining": self._remaining_seconds(assignment.expires_at, now),
                }
            )

        return {
            "count": len(items),
            "items": items,
            "server_time": now.isoformat(),
        }

    async def get_queue_entries(self, db_session: AsyncSession) -> Dict[str, Any]:
        """获取当前排队列表（公开展示）"""
        now = get_now()
        stmt = (
            select(ExperienceQueue)
            .where(ExperienceQueue.status == "queued")
            .order_by(ExperienceQueue.id.asc())
        )
        result = await db_session.execute(stmt)
        items = result.scalars().all()

        rows = []
        for idx, item in enumerate(items, start=1):
            rows.append(
                {
                    "queue_id": item.id,
                    "email": item.email,
                    "position": idx,
                    "queued_at": item.created_at.isoformat() if item.created_at else None,
                }
            )

        return {
            "count": len(rows),
            "items": rows,
            "server_time": now.isoformat(),
        }

    async def join_experience(self, email: str, db_session: AsyncSession) -> Dict[str, Any]:
        """体验组队入口：有位即拉入，无位则入队并返回排队位次"""
        normalized_email = self._normalize_email(email)
        if not normalized_email:
            return {"success": False, "error_code": "invalid_email", "error": "邮箱不能为空"}
        if "@" not in normalized_email or "." not in normalized_email.split("@")[-1]:
            return {"success": False, "error_code": "invalid_email", "error": "邮箱格式不正确"}

        existing_active = await self._get_active_assignment(normalized_email, db_session)
        if existing_active:
            team = await db_session.get(Team, existing_active.team_id)
            return {
                "success": True,
                "status": "active",
                "message": "该邮箱已在体验组队中",
                "already_active": True,
                "team_info": {
                    "team_id": existing_active.team_id,
                    "team_email": team.email if team else None,
                    "team_name": team.team_name if team else None,
                },
                "seconds_remaining": self._remaining_seconds(existing_active.expires_at, now),
                "expires_at": existing_active.expires_at.isoformat(),
            }

        existing_queue = await self._get_queue_item(normalized_email, db_session)
        if existing_queue:
            pos = await self._get_queue_position(existing_queue.id, db_session)
            return {
                "success": True,
                "status": "queued",
                "message": "该邮箱已在排队中",
                "queue_position": pos,
                "queue_id": existing_queue.id,
            }

        assign_result = await self._try_assign_one_now(normalized_email, db_session)
        if assign_result.get("assigned"):
            assignment = assign_result["assignment"]
            team = assign_result["team"]
            await db_session.commit()
            return {
                "success": True,
                "status": "active",
                "message": f"拉入成功，倒计时已开始（{self.duration_minutes} 分钟后自动踢出）",
                "already_active": False,
                "team_info": {
                    "team_id": team.id,
                    "team_email": team.email,
                    "team_name": team.team_name,
                },
                "seconds_remaining": self.duration_minutes * 60,
                "expires_at": assignment.expires_at.isoformat(),
            }

        if assign_result.get("all_full"):
            queue_item = ExperienceQueue(
                email=normalized_email,
                status="queued",
            )
            db_session.add(queue_item)
            await db_session.flush()
            queue_position = await self._get_queue_position(queue_item.id, db_session)
            await db_session.commit()

            return {
                "success": True,
                "status": "queued",
                "message": "当前位置已满，已加入排队",
                "queue_position": queue_position,
                "queue_id": queue_item.id,
            }

        await db_session.rollback()
        err = assign_result.get("error") or "暂时无法拉入，请稍后重试"
        return {
            "success": False,
            "error_code": "join_failed",
            "error": f"暂时无法拉入，请稍后重试（{err}）",
        }

    async def process_queue(self, db_session: AsyncSession, limit: int = 10) -> Dict[str, int]:
        """按排队顺序拉人。仅在有可用位置时处理。"""
        stmt = (
            select(ExperienceQueue)
            .where(ExperienceQueue.status == "queued")
            .order_by(ExperienceQueue.id.asc())
            .limit(limit)
        )
        result = await db_session.execute(stmt)
        queue_items = result.scalars().all()

        if not queue_items:
            return {"processed": 0, "assigned": 0, "failed": 0, "still_queued": 0}

        processed = 0
        assigned = 0
        failed = 0
        still_queued = 0

        for item in queue_items:
            processed += 1

            active = await self._get_active_assignment(item.email, db_session)
            if active:
                item.status = "assigned"
                item.assignment_id = active.id
                item.assigned_team_id = active.team_id
                item.assigned_at = get_now()
                item.note = "邮箱已在体验组队中，自动出队"
                assigned += 1
                continue

            assign_result = await self._try_assign_one_now(item.email, db_session)
            if assign_result.get("assigned"):
                assignment = assign_result["assignment"]
                team = assign_result["team"]
                item.status = "assigned"
                item.assignment_id = assignment.id
                item.assigned_team_id = team.id
                item.assigned_at = get_now()
                item.note = "按队列顺序自动拉入"
                assigned += 1
                continue

            if assign_result.get("all_full"):
                still_queued += 1
                break

            item.status = "failed"
            item.note = assign_result.get("error") or "自动排队拉入失败"
            failed += 1

        await db_session.commit()

        return {
            "processed": processed,
            "assigned": assigned,
            "failed": failed,
            "still_queued": still_queued,
        }

    async def cleanup_expired_assignments(self, db_session: AsyncSession, limit: int = 50) -> Dict[str, int]:
        """清理到期体验记录：自动移出成员/撤回邀请"""
        now = get_now()
        stmt = (
            select(ExperienceAssignment)
            .where(
                ExperienceAssignment.status == "active",
                ExperienceAssignment.expires_at <= now,
            )
            .order_by(ExperienceAssignment.expires_at.asc(), ExperienceAssignment.id.asc())
            .limit(limit)
        )
        result = await db_session.execute(stmt)
        assignments = result.scalars().all()

        if not assignments:
            return {"processed": 0, "removed": 0, "failed": 0}

        processed = 0
        removed = 0
        failed = 0

        for item in assignments:
            processed += 1
            try:
                remove_result = await self.team_service.remove_invite_or_member(item.team_id, item.email, db_session)
                item.removed_at = get_now()

                if remove_result.get("success"):
                    item.status = "expired"
                    item.auto_remove_result = remove_result.get("message") or "已自动移出"
                    removed += 1
                else:
                    item.status = "failed"
                    item.auto_remove_result = remove_result.get("error") or "自动移出失败"
                    failed += 1
            except Exception as exc:
                item.removed_at = get_now()
                item.status = "failed"
                item.auto_remove_result = f"自动移出异常: {exc}"
                failed += 1
                logger.exception("体验组队自动移出异常: assignment_id=%s", item.id)

        await db_session.commit()

        return {"processed": processed, "removed": removed, "failed": failed}

    async def run_scheduled_tick(self, db_session: AsyncSession) -> Dict[str, int]:
        """调度 tick：先踢过期，再按队列顺序补位。"""
        cleanup_stats = await self.cleanup_expired_assignments(db_session)
        queue_stats = await self.process_queue(db_session)

        return {
            "expired_processed": cleanup_stats.get("processed", 0),
            "expired_removed": cleanup_stats.get("removed", 0),
            "expired_failed": cleanup_stats.get("failed", 0),
            "queue_processed": queue_stats.get("processed", 0),
            "queue_assigned": queue_stats.get("assigned", 0),
            "queue_failed": queue_stats.get("failed", 0),
            "queue_still": queue_stats.get("still_queued", 0),
        }

    async def clear_queue(self, db_session: AsyncSession) -> Dict[str, int]:
        """清空体验组队队列（仅 queued 状态）。"""
        stmt = select(func.count()).select_from(ExperienceQueue).where(ExperienceQueue.status == "queued")
        result = await db_session.execute(stmt)
        queued_count = int(result.scalar_one_or_none() or 0)

        if queued_count <= 0:
            return {"cleared": 0}

        await db_session.execute(
            delete(ExperienceQueue).where(ExperienceQueue.status == "queued")
        )
        await db_session.commit()

        return {"cleared": queued_count}


experience_service = ExperienceService()
