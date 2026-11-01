"""Synthetic business tools used by the planning agent."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ToolError(RuntimeError):
    """A business tool failed and can be observed by the decision model."""


class PlanStore:
    """Small local JSON store with request-level idempotency."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def find(self, request_id: str) -> dict[str, Any] | None:
        for record in self._read():
            if record["request_id"] == request_id:
                return record
        return None

    def save(self, plan: dict[str, Any]) -> dict[str, Any]:
        records = self._read()
        request_id = plan["request_id"]
        fingerprint = plan["fingerprint"]
        for record in records:
            if record["request_id"] == request_id:
                if record["fingerprint"] != fingerprint:
                    raise ToolError("request_id 已存在，但请求内容不同")
                return {"saved": True, "duplicate": True, "plan": record}

        record = dict(plan)
        record["plan_id"] = "plan-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:12]
        records.append(record)
        self._write(records)
        return {"saved": True, "duplicate": False, "plan": record}

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            records = data["plans"]
            if not isinstance(records, list):
                raise ValueError("plans must be a list")
            return records
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"读取方案存储失败: {exc}") from exc

    def _write(self, records: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"plans": records}, ensure_ascii=False, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as handle:
                handle.write(payload)
                temp_path = Path(handle.name)
            os.replace(temp_path, self.path)
        except OSError as exc:
            try:
                if "temp_path" in locals() and temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            raise ToolError(f"保存方案失败: {exc}") from exc


class BusinessTools:
    def __init__(
        self,
        catalog_path: str | Path,
        store_path: str | Path,
        failures: dict[str, int] | None = None,
    ):
        try:
            payload = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
            self.campaign = payload["campaign"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ToolError(f"读取合成素材或费用表失败: {exc}") from exc
        self.store = PlanStore(store_path)
        self.failures = {key: max(0, int(value)) for key, value in (failures or {}).items()}

    def search_materials(self, topic: str, strategy: str) -> dict[str, Any]:
        self._maybe_fail("search_materials")
        if strategy not in self.campaign["materials"]:
            raise ToolError(f"不支持的素材检索策略: {strategy}")
        aliases = self.campaign["topic_aliases"]
        if not any(alias in topic or topic in alias for alias in aliases):
            return {"topic": topic, "strategy": strategy, "materials": []}
        material = dict(self.campaign["materials"][strategy])
        material["strategy"] = strategy
        return {"topic": topic, "strategy": strategy, "materials": [material]}

    def generate_script(
        self, material: dict[str, Any], topic: str, mode: str
    ) -> dict[str, Any]:
        self._maybe_fail("generate_script")
        if mode not in self.campaign["generation"][material["strategy"]]:
            raise ToolError(f"不支持的脚本生成模式: {mode}")
        generation = self.campaign["generation"][material["strategy"]][mode]
        signature = f"{material['id']}:{mode}:{topic}"
        script_id = "script-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10]
        tasks = list(self.campaign["plan_tasks"])
        if mode == "concise":
            tasks = tasks[:2] + ["负责人确认发布渠道"]
        return {
            "script_id": script_id,
            "topic": topic,
            "material_ids": [material["id"]],
            "material_strategy": material["strategy"],
            "mode": mode,
            "tasks": tasks,
            "generation_fee_units": generation["fee_units"],
            "generation_hours": generation["hours"],
        }

    def estimate_cost(
        self,
        material: dict[str, Any],
        script: dict[str, Any],
        budget: float,
        available_hours: float,
    ) -> dict[str, Any]:
        self._maybe_fail("estimate_cost")
        total = material["license_units"] + script["generation_fee_units"]
        hours = material["search_hours"] + script["generation_hours"]
        return {
            "material_fee_units": material["license_units"],
            "generation_fee_units": script["generation_fee_units"],
            "total_units": total,
            "budget_units": budget,
            "within_budget": total <= budget,
            "estimated_hours": hours,
            "available_hours": available_hours,
            "within_deadline": hours <= available_hours,
            "material_id": material["id"],
            "script_id": script["script_id"],
        }

    def save_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail("save_plan")
        return self.store.save(plan)

    def _maybe_fail(self, tool_name: str) -> None:
        remaining = self.failures.get(tool_name, 0)
        if remaining > 0:
            self.failures[tool_name] = remaining - 1
            raise ToolError(f"模拟工具故障: {tool_name}")
