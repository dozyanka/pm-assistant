"""Stable service facade for PM Assistant.

The implementation is split by business capability so Q&A, registry extraction,
post-meeting generation, participant roles and retrieval can evolve independently.
"""
from __future__ import annotations

from pathlib import Path

from .db import Store
from .ollama import OllamaClient
from .service_support import *  # re-export validated helpers for backwards compatibility
from .services.core import CoreServiceMixin
from .services.participants import ParticipantServiceMixin
from .services.registry_service import RegistryServiceMixin
from .services.post_meeting import PostMeetingServiceMixin
from .services.qa import QAServiceMixin
from .services.search import SearchServiceMixin


class Service(
    CoreServiceMixin,
    ParticipantServiceMixin,
    RegistryServiceMixin,
    PostMeetingServiceMixin,
    QAServiceMixin,
    SearchServiceMixin,
):
    """Application service facade used by HTTP, desktop and remote entry points."""

    def __init__(self, store: Store, root: Path, client: OllamaClient | None = None):
        self.store = store
        self.root = Path(root)
        self.client = client or OllamaClient()
