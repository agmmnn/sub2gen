"""Project resource for Google Flow."""

from __future__ import annotations

import asyncio

from ...core.config import config
from ...core.logger import debug_logger
from .base import FlowResource


class FlowProjectsResource(FlowResource):
    async def create(self, session_token: str, title: str) -> str:
        payload = {"json": {"projectTitle": title, "toolName": "PINHOLE"}}
        max_retries = self.client._resolve_generation_retry_budget(config.flow_max_retries)
        timeout = max(self.client._get_control_plane_timeout(), min(self.client.timeout, 15))
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                result = await self.client.transport.request(
                    method="POST",
                    url=f"{self.client.labs_base_url}/trpc/project.createProject",
                    json_data=payload,
                    use_st=True,
                    st_token=session_token,
                    timeout=timeout,
                )
                project_id = result.get("result", {}).get("data", {}).get("json", {}).get("result", {}).get("projectId")
                if not project_id:
                    raise RuntimeError("Invalid project.createProject response: missing projectId")
                return str(project_id)
            except Exception as error:
                last_error = error
                reason = (
                    "网络超时" if self.client._is_timeout_error(error) else self.client._get_retry_reason(str(error))
                )
                if reason and attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[PROJECT] 创建项目失败，准备重试 ({attempt + 2}/{max_retries}) "
                        f"title={title!r}, reason={reason}: {error}"
                    )
                    await asyncio.sleep(1)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("创建项目失败")

    async def delete(self, session_token: str, project_id: str) -> None:
        await self.client.transport.request(
            method="POST",
            url=f"{self.client.labs_base_url}/trpc/project.deleteProject",
            json_data={"json": {"projectToDeleteId": project_id}},
            use_st=True,
            st_token=session_token,
            timeout=self.client._get_control_plane_timeout(),
        )
