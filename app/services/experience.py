"""体验组队服务"""
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExperienceAssignment, Team
from app.services.team import TeamService
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


class ExperienceService:
    """体验组队自动拉人 + 自动移出"""

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

    async def join_experience(self, email: str, db_session: AsyncSession) -> Dict[str, Any]:
        """体验组队入口：自动选择体验池账号，拉人并返回倒计时"""
        normalized_email = self._normalize_email(email)
        if not normalized_email:
            return {"success": False, "error_code": "invalid_email", "error": "邮箱不能为空"}
        if "@" not in normalized_email or "." not in normalized_email.split("@")[-1]:
            return {"success": False, "error_code": "invalid_email", "error": "邮箱格式不正确"}

        now = get_now()

        # 已存在活跃体验记录，直接返回剩余时间
        existing = await self._get_active_assignment(normalized_email, db_session)
        if existing:
            team = await db_session.get(Team, existing.team_id)
            return {
                "success": True,
                "message": "该邮箱已在体验组队中",
                "already_active": True,
                "team_info": {
                    "team_id": existing.team_id,
                    "team_email": team.email if team else None,
                    "team_name": team.team_name if team else None,
                },
                "seconds_remaining": self._remaining_seconds(existing.expires_at, now),
                "expires_at": existing.expires_at.isoformat(),
            }

        # 体验池账号列表（welfare 池）
        stmt = (
            select(Team)
            .where(Team.pool_type == "welfare", Team.status.in_(["active", "full"]))
            .order_by(Team.id.asc())
        )
        result = await db_session.execute(stmt)
        teams = result.scalars().all()

        if not teams:
            return {
                "success": False,
                "error_code": "all_full",
                "error": "当前位置已满，请等待",
            }

        full_like_count = 0
        last_error: Optional[str] = None

        for team in teams:
            # 已过期账号跳过
            if team.expires_at and team.expires_at <= now:
                continue

            cap = self._team_capacity_limit(team)
            if cap <= 0 or int(team.current_members or 0) >= cap:
                full_like_count += 1
                continue

            add_result = await self.team_service.add_team_member(team.id, normalized_email, db_session)
            if add_result.get("success"):
                expires_at = get_now() + timedelta(minutes=self.duration_minutes)
                assignment = ExperienceAssignment(
                    email=normalized_email,
                    team_id=team.id,
                    expires_at=expires_at,
                    status="active",
                )
                db_session.add(assignment)
                await db_session.commit()

                return {
                    "success": True,
                    "message": f"拉入成功，倒计时已开始（{self.duration_minutes} 分钟后自动踢出）",
                    "already_active": False,
                    "team_info": {
                        "team_id": team.id,
                        "team_email": team.email,
                        "team_name": team.team_name,
                    },
                    "seconds_remaining": self.duration_minutes * 60,
                    "expires_at": expires_at.isoformat(),
                }

            error_msg = (add_result.get("error") or "").strip()
            error_msg_lower = error_msg.lower()

            # 已满 / 席位冲突：切换下一个账号
            if (
                "已满" in error_msg
                or "seat" in error_msg_lower
                or "full" in error_msg_lower
                or "maximum number of seats" in error_msg_lower
            ):
                full_like_count += 1
                continue

            # 已在该账号内（或重复邀请）：按需求切换下一个账号
            if (
                "已在" in error_msg
                or "already" in error_msg_lower
                or "invited" in error_msg_lower
                or "重复" in error_msg
            ):
                continue

            last_error = error_msg or "拉入失败"
            logger.warning("体验组队拉人失败，Team=%s, email=%s, error=%s", team.id, normalized_email, last_error)

        if full_like_count >= len(teams):
            return {
                "success": False,
                "error_code": "all_full",
                "error": "当前位置已满，请等待",
            }

        if last_error:
            return {
                "success": False,
                "error_code": "join_failed",
                "error": f"暂时无法拉入，请稍后重试（{last_error}）",
            }

        return {
            "success": False,
            "error_code": "all_full",
            "error": "当前位置已满，请等待",
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


experience_service = ExperienceService()
