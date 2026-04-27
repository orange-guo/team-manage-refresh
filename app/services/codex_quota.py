import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
import jwt
import pytz
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.settings import settings_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


class CodexQuotaService:
    QUOTA_URL = "https://chatgpt.com/backend-api/wham/usage"
    USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
    DEFAULT_TIMEOUT = 20.0
    FIVE_HOUR_WINDOW_SECONDS = 5 * 60 * 60
    WEEKLY_WINDOW_SECONDS = 7 * 24 * 60 * 60

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _safe_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return bool(value)
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return None

    @staticmethod
    def extract_account_id_from_token(token: Optional[str]) -> Optional[str]:
        if not token:
            return None

        try:
            payload = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                },
            )
        except Exception:
            return None

        auth_info = payload.get("https://api.openai.com/auth") or {}
        value = auth_info.get("chatgpt_account_id") or auth_info.get("chatgpt_accountId")
        if not value:
            return None
        return str(value).strip() or None

    @staticmethod
    def extract_plan_type_from_token(token: Optional[str]) -> Optional[str]:
        if not token:
            return None

        try:
            payload = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                },
            )
        except Exception:
            return None

        auth_info = payload.get("https://api.openai.com/auth") or {}
        value = auth_info.get("chatgpt_plan_type") or auth_info.get("chatgpt_planType")
        if not value:
            return None
        return str(value).strip().lower() or None

    @staticmethod
    def _label_for_plan_type(plan_type: Optional[str]) -> Optional[str]:
        normalized = str(plan_type or "").strip().lower()
        mapping = {
            "free": "Free",
            "plus": "Plus",
            "team": "Team",
            "pro": "Pro 20x",
            "prolite": "Pro 5x",
        }
        if not normalized:
            return None
        return mapping.get(normalized, normalized.upper())

    @staticmethod
    def _slugify(value: str) -> str:
        cleaned = []
        last_dash = False
        for char in str(value or "").strip().lower():
            if char.isalnum():
                cleaned.append(char)
                last_dash = False
                continue
            if not last_dash:
                cleaned.append("-")
                last_dash = True
        return "".join(cleaned).strip("-") or "extra"

    def _to_local_iso_from_unix(self, unix_seconds: Optional[int]) -> Optional[str]:
        if not unix_seconds or unix_seconds <= 0:
            return None

        local_tz = pytz.timezone(settings.timezone)
        dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc).astimezone(local_tz)
        return dt.isoformat()

    def _normalize_window(
        self,
        window: Optional[Dict[str, Any]],
        *,
        window_id: str,
        label: str,
        limit_reached: Optional[bool],
        allowed: Optional[bool],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(window, dict):
            return None

        used_percent = self._safe_float(window.get("used_percent", window.get("usedPercent")))
        limit_window_seconds = self._safe_int(
            window.get("limit_window_seconds", window.get("limitWindowSeconds"))
        )
        reset_after_seconds = self._safe_int(
            window.get("reset_after_seconds", window.get("resetAfterSeconds"))
        )
        reset_at = self._safe_int(window.get("reset_at", window.get("resetAt")))

        return {
            "id": window_id,
            "label": label,
            "used_percent": used_percent,
            "limit_window_seconds": limit_window_seconds,
            "reset_after_seconds": reset_after_seconds,
            "reset_at": self._to_local_iso_from_unix(reset_at),
            "limit_reached": limit_reached,
            "allowed": allowed,
        }

    def _pick_primary_and_weekly_windows(
        self,
        rate_limit: Optional[Dict[str, Any]],
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        primary = None
        secondary = None
        if isinstance(rate_limit, dict):
            primary = rate_limit.get("primary_window") or rate_limit.get("primaryWindow")
            secondary = rate_limit.get("secondary_window") or rate_limit.get("secondaryWindow")

        candidates = [primary, secondary]
        five_hour = None
        weekly = None

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            limit_window_seconds = self._safe_int(
                candidate.get("limit_window_seconds", candidate.get("limitWindowSeconds"))
            )
            if limit_window_seconds == self.FIVE_HOUR_WINDOW_SECONDS and five_hour is None:
                five_hour = candidate
            elif limit_window_seconds == self.WEEKLY_WINDOW_SECONDS and weekly is None:
                weekly = candidate

        if five_hour is None and isinstance(primary, dict) and primary is not weekly:
            five_hour = primary
        if weekly is None and isinstance(secondary, dict) and secondary is not five_hour:
            weekly = secondary

        return {
            "five_hour": five_hour,
            "weekly": weekly,
        }

    def _build_windows(self, payload: Dict[str, Any]) -> list[Dict[str, Any]]:
        windows: list[Dict[str, Any]] = []

        rate_limit = payload.get("rate_limit") or payload.get("rateLimit")
        if isinstance(rate_limit, dict):
            allowed = self._safe_bool(rate_limit.get("allowed"))
            limit_reached = self._safe_bool(
                rate_limit.get("limit_reached", rate_limit.get("limitReached"))
            )
            selected = self._pick_primary_and_weekly_windows(rate_limit)
            five_hour = self._normalize_window(
                selected["five_hour"],
                window_id="five-hour",
                label="5 小时限额",
                limit_reached=limit_reached,
                allowed=allowed,
            )
            weekly = self._normalize_window(
                selected["weekly"],
                window_id="weekly",
                label="周限额",
                limit_reached=limit_reached,
                allowed=allowed,
            )
            if five_hour:
                windows.append(five_hour)
            if weekly:
                windows.append(weekly)

        code_review_rate_limit = payload.get("code_review_rate_limit") or payload.get("codeReviewRateLimit")
        if isinstance(code_review_rate_limit, dict):
            allowed = self._safe_bool(code_review_rate_limit.get("allowed"))
            limit_reached = self._safe_bool(
                code_review_rate_limit.get("limit_reached", code_review_rate_limit.get("limitReached"))
            )
            selected = self._pick_primary_and_weekly_windows(code_review_rate_limit)
            five_hour = self._normalize_window(
                selected["five_hour"],
                window_id="code-review-five-hour",
                label="代码审查 5 小时限额",
                limit_reached=limit_reached,
                allowed=allowed,
            )
            weekly = self._normalize_window(
                selected["weekly"],
                window_id="code-review-weekly",
                label="代码审查周限额",
                limit_reached=limit_reached,
                allowed=allowed,
            )
            if five_hour:
                windows.append(five_hour)
            if weekly:
                windows.append(weekly)

        additional_rate_limits = payload.get("additional_rate_limits") or payload.get("additionalRateLimits") or []
        if isinstance(additional_rate_limits, list):
            for index, item in enumerate(additional_rate_limits, start=1):
                if not isinstance(item, dict):
                    continue
                nested_rate_limit = item.get("rate_limit") or item.get("rateLimit")
                if not isinstance(nested_rate_limit, dict):
                    continue

                raw_name = (
                    item.get("limit_name")
                    or item.get("limitName")
                    or item.get("metered_feature")
                    or item.get("meteredFeature")
                    or f"附加限额 {index}"
                )
                label_name = str(raw_name).strip() or f"附加限额 {index}"
                slug = self._slugify(label_name)
                allowed = self._safe_bool(nested_rate_limit.get("allowed"))
                limit_reached = self._safe_bool(
                    nested_rate_limit.get("limit_reached", nested_rate_limit.get("limitReached"))
                )
                selected = self._pick_primary_and_weekly_windows(nested_rate_limit)

                five_hour = self._normalize_window(
                    selected["five_hour"],
                    window_id=f"{slug}-five-hour-{index}",
                    label=f"{label_name} 5 小时限额",
                    limit_reached=limit_reached,
                    allowed=allowed,
                )
                weekly = self._normalize_window(
                    selected["weekly"],
                    window_id=f"{slug}-weekly-{index}",
                    label=f"{label_name} 周限额",
                    limit_reached=limit_reached,
                    allowed=allowed,
                )
                if five_hour:
                    windows.append(five_hour)
                if weekly:
                    windows.append(weekly)

        return windows

    async def fetch_quota(
        self,
        *,
        access_token: str,
        account_id: str,
        db_session: AsyncSession,
        fallback_email: Optional[str] = None,
        fallback_plan_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not access_token:
            return {"success": False, "error": "缺少可用的 Access Token"}

        if not account_id:
            return {"success": False, "error": "缺少 ChatGPT Account ID"}

        proxy_config = await settings_service.get_proxy_config(db_session)
        proxy_url = proxy_config["proxy"] if proxy_config.get("enabled") and proxy_config.get("proxy") else None

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Chatgpt-Account-Id": account_id,
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.DEFAULT_TIMEOUT,
                headers=headers,
                proxy=proxy_url,
            ) as client:
                response = await client.get(self.QUOTA_URL)

            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return {"success": False, "error": "额度接口返回的不是 JSON 对象"}

            plan_type = str(
                payload.get("plan_type")
                or payload.get("planType")
                or fallback_plan_type
                or ""
            ).strip().lower()
            windows = self._build_windows(payload)

            credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
            reached_type = payload.get("rate_limit_reached_type") or payload.get("rateLimitReachedType") or {}

            return {
                "success": True,
                "quota": {
                    "account_id": str(payload.get("account_id") or account_id).strip(),
                    "email": str(payload.get("email") or fallback_email or "").strip(),
                    "plan_type": plan_type or None,
                    "plan_label": self._label_for_plan_type(plan_type or fallback_plan_type),
                    "rate_limit_allowed": self._safe_bool(
                        (payload.get("rate_limit") or {}).get("allowed")
                        if isinstance(payload.get("rate_limit"), dict)
                        else None
                    ),
                    "limit_reached": self._safe_bool(
                        (payload.get("rate_limit") or {}).get("limit_reached")
                        if isinstance(payload.get("rate_limit"), dict)
                        else None
                    ),
                    "no_access": plan_type == "free",
                    "windows": windows,
                    "credits": {
                        "has_credits": self._safe_bool(credits.get("has_credits")),
                        "unlimited": self._safe_bool(credits.get("unlimited")),
                        "balance": self._safe_float(credits.get("balance")),
                    },
                    "rate_limit_reached_type": reached_type if isinstance(reached_type, dict) else None,
                    "fetched_at": get_now().isoformat(),
                },
            }
        except httpx.HTTPStatusError as exc:
            body_text = ""
            try:
                body_text = exc.response.text.strip()
            except Exception:
                body_text = ""

            logger.warning(
                "Codex 额度请求失败: status=%s body=%s",
                getattr(exc.response, "status_code", "unknown"),
                body_text,
            )
            message = body_text or f"HTTP {getattr(exc.response, 'status_code', 'unknown')}"
            return {"success": False, "error": f"额度请求失败: {message}"}
        except Exception as exc:
            logger.error("Codex 额度请求异常: %s", exc)
            return {"success": False, "error": f"额度请求失败: {str(exc)}"}


codex_quota_service = CodexQuotaService()
