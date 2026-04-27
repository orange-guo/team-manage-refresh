import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
import pytz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Team
from app.services.codex_quota import codex_quota_service
from app.services.encryption import encryption_service
from app.services.settings import settings_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


@dataclass
class CliproxyapiConfig:
    base_url: str
    api_key: str
    proxy: Optional[str] = None


class CliproxyapiService:
    MANAGEMENT_PREFIX = "/v0/management"
    DEFAULT_TIMEOUT = 20.0

    @staticmethod
    def normalize_base_url(base_url: Optional[str]) -> str:
        value = str(base_url or "").strip()
        if not value:
            return ""
        return value.rstrip("/")

    @staticmethod
    def is_valid_base_url(base_url: Optional[str]) -> bool:
        value = CliproxyapiService.normalize_base_url(base_url)
        if not value:
            return True

        try:
            parsed = urlparse(value)
        except Exception:
            return False

        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _sanitize_email_for_filename(email: str) -> str:
        normalized = str(email or "").strip().lower()
        sanitized = re.sub(r"[^A-Za-z0-9._@-]+", "_", normalized)
        return sanitized.strip("._-") or "team"

    @staticmethod
    def _canonical_json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _to_local_iso(dt) -> str:
        if not dt:
            return ""

        local_tz = pytz.timezone(settings.timezone)
        if dt.tzinfo is None:
            localized = local_tz.localize(dt)
        else:
            localized = dt.astimezone(local_tz)
        return localized.isoformat()

    async def _load_config(self, db_session: AsyncSession) -> Optional[CliproxyapiConfig]:
        base_url = self.normalize_base_url(
            await settings_service.get_setting(db_session, "cliproxyapi_base_url", "")
        )
        api_key = str(
            await settings_service.get_setting(db_session, "cliproxyapi_api_key", "")
            or ""
        ).strip()

        if not base_url or not api_key:
            return None

        proxy_config = await settings_service.get_proxy_config(db_session)
        proxy_url = proxy_config["proxy"] if proxy_config.get("enabled") and proxy_config.get("proxy") else None

        return CliproxyapiConfig(
            base_url=base_url,
            api_key=api_key,
            proxy=proxy_url,
        )

    @staticmethod
    def _build_warning_message(missing_fields: list[str]) -> str:
        if not missing_fields:
            return ""

        field_labels = {
            "id_token": "id_token",
            "refresh_token": "refresh_token",
        }
        labels = [field_labels.get(field, field) for field in missing_fields]
        joined = "、".join(labels)
        return f"当前 Team 缺少 {joined}，已按空值推送，CliproxyAPI 刷新额度时可能失败"

    def _build_payload(
        self,
        team: Team,
        access_token: str,
        id_token: str,
        refresh_token: str,
    ) -> Dict[str, Any]:
        # last_refresh 取同步时间而不是推送时间，避免重复推送因时间戳变化而失去幂等。
        last_refresh_time = team.last_sync or team.created_at or get_now()
        return {
            "access_token": access_token,
            "account_id": team.account_id or "",
            "email": team.email or "",
            "expired": self._to_local_iso(team.expires_at),
            "id_token": id_token,
            "last_refresh": self._to_local_iso(last_refresh_time),
            "refresh_token": refresh_token,
            "type": "codex",
        }

    def _build_filename(self, team: Team) -> str:
        safe_email = self._sanitize_email_for_filename(team.email or "")
        if team.expires_at:
            return f"{safe_email}__exp-{team.expires_at.strftime('%Y%m%d%H%M%S')}.json"
        return f"{safe_email}__team-{team.id}.json"

    def _normalize_downloaded_payload(self, content: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = json.loads(content)
        except Exception:
            return None

        if isinstance(parsed, dict):
            return parsed
        return None

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        expected_status: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        response = await client.request(method, url, **kwargs)
        if expected_status is not None and response.status_code != expected_status:
            raise httpx.HTTPStatusError(
                f"unexpected status: {response.status_code}",
                request=response.request,
                response=response,
            )
        response.raise_for_status()
        if not response.content:
            return {}
        data = response.json()
        if isinstance(data, dict):
            return data
        raise ValueError("响应不是 JSON 对象")

    async def _get_remote_file(self, client: httpx.AsyncClient, base_url: str, filename: str) -> Optional[str]:
        url = f"{base_url}/auth-files/download"
        response = await client.get(url, params={"name": filename})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def _list_remote_files(self, client: httpx.AsyncClient, base_url: str) -> Dict[str, Any]:
        return await self._request_json(client, "GET", f"{base_url}/auth-files")

    async def _delete_remote_file(self, client: httpx.AsyncClient, base_url: str, filename: str) -> None:
        response = await client.delete(f"{base_url}/auth-files", params={"name": filename})
        if response.status_code == 404:
            return
        response.raise_for_status()

    async def _upload_remote_file(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        filename: str,
        canonical_payload: str,
    ) -> None:
        response = await client.post(
            f"{base_url}/auth-files",
            params={"name": filename},
            content=canonical_payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

    @staticmethod
    def _extract_remote_account_id(remote_entry: Dict[str, Any]) -> str:
        id_token = remote_entry.get("id_token")
        if isinstance(id_token, dict):
            value = id_token.get("chatgpt_account_id") or id_token.get("chatgpt_accountId")
            if value:
                return str(value).strip()

        value = remote_entry.get("account_id") or remote_entry.get("accountId")
        return str(value or "").strip()

    @staticmethod
    def _extract_remote_plan_type(remote_entry: Dict[str, Any]) -> str:
        id_token = remote_entry.get("id_token")
        if isinstance(id_token, dict):
            value = id_token.get("plan_type") or id_token.get("chatgpt_plan_type")
            if value:
                return str(value).strip().lower()

        value = remote_entry.get("plan_type") or remote_entry.get("planType")
        return str(value or "").strip().lower()

    def _find_remote_auth_entry(
        self,
        remote_files: list[Dict[str, Any]],
        team: Team,
    ) -> Optional[Dict[str, Any]]:
        email = str(team.email or "").strip().lower()
        account_id = str(team.account_id or "").strip()

        account_match_entry = None
        email_match_entry = None

        for item in remote_files:
            if not isinstance(item, dict):
                continue

            item_type = str(item.get("type") or "").strip().lower()
            if item_type and item_type != "codex":
                continue

            remote_email = str(item.get("email") or item.get("account") or "").strip().lower()
            remote_account_id = self._extract_remote_account_id(item)

            email_match = bool(email) and remote_email == email
            account_match = bool(account_id) and remote_account_id == account_id

            if email_match and account_match:
                return item
            if account_match and account_match_entry is None:
                account_match_entry = item
            if email_match and email_match_entry is None:
                email_match_entry = item

        return account_match_entry or email_match_entry

    async def fetch_team_quota(self, team: Team, db_session: AsyncSession) -> Dict[str, Any]:
        config = await self._load_config(db_session)
        if not config:
            return {
                "success": False,
                "configured": False,
                "error": "未配置 CliproxyAPI 额度源",
            }

        if not self.is_valid_base_url(config.base_url):
            return {
                "success": False,
                "configured": False,
                "error": "CliproxyAPI 地址格式错误，仅支持 http/https",
            }

        management_base_url = f"{config.base_url}{self.MANAGEMENT_PREFIX}"
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.DEFAULT_TIMEOUT,
                headers=headers,
                proxy=config.proxy,
            ) as client:
                listing = await self._list_remote_files(client, management_base_url)
                remote_files = listing.get("files") or []
                remote_entry = self._find_remote_auth_entry(remote_files, team)
                if remote_entry is None:
                    return {
                        "success": False,
                        "configured": True,
                        "error": "CliproxyAPI 中未找到该 Team 对应的 Codex 认证文件",
                    }

                if remote_entry.get("runtime_only"):
                    return {
                        "success": False,
                        "configured": True,
                        "error": "匹配到的 CliproxyAPI 认证为 runtime_only，当前无法下载额度凭据",
                    }

                filename = str(remote_entry.get("name") or "").strip()
                if not filename:
                    return {
                        "success": False,
                        "configured": True,
                        "error": "CliproxyAPI 返回的认证文件缺少文件名",
                    }

                remote_content = await self._get_remote_file(client, management_base_url, filename)
                remote_payload = self._normalize_downloaded_payload(remote_content or "")
                if remote_payload is None:
                    return {
                        "success": False,
                        "configured": True,
                        "error": "无法解析 CliproxyAPI 返回的认证文件内容",
                    }

            access_token = str(remote_payload.get("access_token") or "").strip()
            if not access_token:
                return {
                    "success": False,
                    "configured": True,
                    "error": "CliproxyAPI 认证文件中缺少可用的 access_token",
                }

            account_id = (
                str(remote_payload.get("account_id") or "").strip()
                or self._extract_remote_account_id(remote_entry)
                or str(team.account_id or "").strip()
            )
            if not account_id:
                return {
                    "success": False,
                    "configured": True,
                    "error": "CliproxyAPI 认证文件中缺少 account_id",
                }

            id_token = str(remote_payload.get("id_token") or "").strip()
            fallback_plan_type = (
                codex_quota_service.extract_plan_type_from_token(id_token)
                or self._extract_remote_plan_type(remote_entry)
                or str(team.plan_type or "").strip().lower()
            )

            quota_result = await codex_quota_service.fetch_quota(
                access_token=access_token,
                account_id=account_id,
                db_session=db_session,
                fallback_email=str(remote_payload.get("email") or team.email or "").strip(),
                fallback_plan_type=fallback_plan_type,
            )
            quota_result["configured"] = True

            if quota_result.get("success") and isinstance(quota_result.get("quota"), dict):
                quota_result["quota"]["source"] = "cliproxyapi"
                quota_result["quota"]["auth_filename"] = filename

            return quota_result
        except httpx.HTTPStatusError as exc:
            response_text = ""
            try:
                response_text = exc.response.text.strip()
            except Exception:
                response_text = ""

            logger.error(
                "从 CliproxyAPI 获取 Team %s 额度失败，status=%s, body=%s",
                team.id,
                getattr(exc.response, "status_code", "unknown"),
                response_text,
            )
            error_message = response_text or f"HTTP {getattr(exc.response, 'status_code', 'unknown')}"
            return {
                "success": False,
                "configured": True,
                "error": f"CliproxyAPI 请求失败: {error_message}",
            }
        except Exception as exc:
            logger.error("从 CliproxyAPI 获取 Team %s 额度异常: %s", team.id, exc)
            return {
                "success": False,
                "configured": True,
                "error": f"获取额度失败: {str(exc)}",
            }

    async def push_team_auth_file(self, team_id: int, db_session: AsyncSession) -> Dict[str, Any]:
        config = await self._load_config(db_session)
        if not config:
            return {"success": False, "error": "请先在系统设置中填写 CliproxyAPI 地址和管理密钥"}

        if not self.is_valid_base_url(config.base_url):
            return {"success": False, "error": "CliproxyAPI 地址格式错误，仅支持 http/https"}

        result = await db_session.execute(select(Team).where(Team.id == team_id))
        team = result.scalar_one_or_none()
        if not team:
            return {"success": False, "error": "Team 不存在"}

        email = str(team.email or "").strip()
        if not email:
            return {"success": False, "error": "Team 缺少邮箱，无法生成认证文件", "email": ""}

        try:
            access_token = encryption_service.decrypt_token(team.access_token_encrypted)
        except Exception as exc:
            logger.error("解密 Team %s access_token 失败: %s", team_id, exc)
            access_token = ""

        if not access_token:
            return {"success": False, "error": "Team 缺少 Access Token，无法推送", "email": email}

        refresh_token = ""
        try:
            if team.refresh_token_encrypted:
                refresh_token = encryption_service.decrypt_token(team.refresh_token_encrypted)
        except Exception as exc:
            logger.warning("解密 Team %s refresh_token 失败，将按空值推送: %s", team_id, exc)

        id_token = ""
        try:
            if team.id_token_encrypted:
                id_token = encryption_service.decrypt_token(team.id_token_encrypted)
        except Exception as exc:
            logger.warning("解密 Team %s id_token 失败，将按空值推送: %s", team_id, exc)

        missing_fields = []
        if not id_token:
            missing_fields.append("id_token")
        if not refresh_token:
            missing_fields.append("refresh_token")
        warning_message = self._build_warning_message(missing_fields)

        filename = self._build_filename(team)
        payload = self._build_payload(team, access_token, id_token, refresh_token)
        canonical_payload = self._canonical_json(payload)
        management_base_url = f"{config.base_url}{self.MANAGEMENT_PREFIX}"

        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.DEFAULT_TIMEOUT,
                headers=headers,
                proxy=config.proxy,
            ) as client:
                listing = await self._list_remote_files(client, management_base_url)
                remote_files = listing.get("files") or []
                remote_entry = next(
                    (
                        item for item in remote_files
                        if isinstance(item, dict) and str(item.get("name") or "").strip() == filename
                    ),
                    None,
                )

                if remote_entry is None:
                    await self._upload_remote_file(client, management_base_url, filename, canonical_payload)
                    return {
                        "success": True,
                        "message": f"已推送到 CliproxyAPI：{filename}",
                        "email": email,
                        "filename": filename,
                        "action": "uploaded",
                        "warning": warning_message or None,
                        "warnings": missing_fields,
                    }

                if remote_entry.get("runtime_only"):
                    return {
                        "success": False,
                        "error": f"远端已存在同名 runtime_only 凭据，无法通过文件接口覆盖：{filename}",
                        "email": email,
                        "filename": filename,
                    }

                remote_content = await self._get_remote_file(client, management_base_url, filename)
                remote_payload = self._normalize_downloaded_payload(remote_content or "")

                if remote_payload is not None and remote_payload == payload:
                    return {
                        "success": True,
                        "message": f"远端认证文件已是最新，跳过推送：{filename}",
                        "email": email,
                        "filename": filename,
                        "action": "skipped",
                        "warning": warning_message or None,
                        "warnings": missing_fields,
                    }

                await self._delete_remote_file(client, management_base_url, filename)
                await self._upload_remote_file(client, management_base_url, filename, canonical_payload)
                return {
                    "success": True,
                    "message": f"已更新远端认证文件：{filename}",
                    "email": email,
                    "filename": filename,
                    "action": "updated",
                    "warning": warning_message or None,
                    "warnings": missing_fields,
                }
        except httpx.HTTPStatusError as exc:
            response_text = ""
            try:
                response_text = exc.response.text.strip()
            except Exception:
                response_text = ""

            logger.error(
                "推送 Team %s 到 CliproxyAPI 失败，status=%s, body=%s",
                team_id,
                getattr(exc.response, "status_code", "unknown"),
                response_text,
            )
            error_message = response_text or f"HTTP {getattr(exc.response, 'status_code', 'unknown')}"
            return {"success": False, "error": f"CliproxyAPI 请求失败: {error_message}", "email": email, "filename": filename}
        except Exception as exc:
            logger.error("推送 Team %s 到 CliproxyAPI 异常: %s", team_id, exc)
            return {"success": False, "error": f"推送失败: {str(exc)}", "email": email, "filename": filename}


cliproxyapi_service = CliproxyapiService()
