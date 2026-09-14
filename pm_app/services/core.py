"""Focused PM Assistant service operations.

Kept as a mixin so ``pm_app.service.Service`` remains the stable public facade.
"""
from __future__ import annotations
from ..service_support import *  # noqa: F401,F403 - shared validated pipeline primitives

class CoreServiceMixin:
    def demo_import(self) -> dict:
        path = self.root / "samples" / "messages.jsonl"
        projects_path = self.root / "samples" / "projects.json"
        if not path.is_file() or not projects_path.is_file():
            raise ValueError("Starter samples are missing. Extract the original starter into the project root first.")
        rows = parse_jsonl(path.read_text(encoding="utf-8-sig"))
        projects = json.loads(projects_path.read_text(encoding="utf-8-sig"))
        names = {p["project_id"]: p["name"] for p in projects}
        # Stress fixtures and the post-meeting template are intentionally NOT loaded.
        return self.store.import_rows(rows, names=names)

    def full_demo_import(self) -> dict:
        from ..full_sample import load_full_sample
        rows, manifest = load_full_sample(self.root)
        result = self.store.import_rows(rows, names={manifest["project_id"]: manifest["project_name"]})
        return {**result, "project_id": manifest["project_id"], "message_count": len(rows)}

    def source_project_1_import(self) -> dict:
        from ..source_examples import load_source_project_1
        rows, manifest = load_source_project_1(self.root)
        result = self.store.import_rows(rows, names={manifest["project_id"]: manifest["project_name"]})
        return {**result, "project_id": manifest["project_id"], "message_count": len(rows),
                "source_count": manifest["source_count"], "original_project_name": manifest["original_project_name"]}

