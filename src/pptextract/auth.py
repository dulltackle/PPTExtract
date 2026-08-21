from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from pptextract.config import Settings

ACTOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ActorContext:
    actor_id: str
    display_name: str

    def idempotency_scope(self, key: str) -> tuple[str, str]:
        return (self.actor_id, key)


class ActorProvider(Protocol):
    def resolve(self, request: Request) -> ActorContext: ...


class HeaderActorProvider:
    """初版单机鉴权适配器；未来 OAuth/SSO 只需替换这一边界。"""

    def __init__(self, settings: Settings) -> None:
        self.default_actor_id = settings.default_actor_id

    def resolve(self, request: Request) -> ActorContext:
        candidate = request.headers.get("X-Actor-ID", self.default_actor_id).strip()
        actor_id = candidate if ACTOR_ID_PATTERN.fullmatch(candidate) else self.default_actor_id
        return ActorContext(actor_id=actor_id, display_name=f"操作者 {actor_id}")
