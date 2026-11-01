"""Decision loop and model adapters for content task planning."""

from __future__ import annotations

import json
import hashlib
import math
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .tools import BusinessTools, ToolError

TOOL_ACTIONS = {"search_materials", "generate_script", "estimate_cost", "save_plan"}


class DecisionModel(Protocol):
    def decide(self, context: dict[str, Any]) -> dict[str, Any]: ...


class OfflineMockDecisionModel:
    """Deterministic offline policy for exercising the observe-and-adjust loop."""

    trace_name = "offline_mock"

    def decide(self, context: dict[str, Any]) -> dict[str, Any]:
        task = context["task"]
        artifacts = context["artifacts"]
        last = context.get("last_observation") or {}

        if context.get("saved_plan"):
            return self._decision("finish", "方案已保存，结束本次任务。", status="completed")

        if last.get("ok") is False:
            failed_action = last["action"]
            attempts = context["failure_counts"].get(failed_action, 0)
            previous_args = last.get("arguments", {})
            if failed_action == "generate_script" and previous_args.get("mode") == "standard":
                return self._decision(
                    "generate_script",
                    "标准脚本工具失败；保留已检索素材，改用精简模式继续。",
                    {"mode": "concise"},
                )
            if failed_action == "search_materials" and previous_args.get("strategy") == "standard":
                return self._decision(
                    "search_materials",
                    "标准检索失败；切换到轻量素材方案再规划。",
                    {"topic": task["topic"], "strategy": "budget_saving"},
                )
            if failed_action in {"estimate_cost", "save_plan"} and attempts == 1:
                return self._decision(
                    failed_action,
                    "观察到一次性工具错误，保留前序成功结果后重试当前步骤。",
                    previous_args if failed_action == "search_materials" else {},
                )
            return self._decision(
                "finish",
                f"{failed_action} 再次失败，停止以避免把失败步骤记为成功。",
                status="incomplete",
            )

        if (
            last.get("ok") is True
            and last.get("action") == "search_materials"
            and not last.get("data", {}).get("materials")
        ):
            return self._decision(
                "finish",
                "检索结果没有匹配素材，停止并说明当前资料不足。",
                status="incomplete",
            )

        material = artifacts.get("material")
        script = artifacts.get("script")
        estimate = artifacts.get("estimate")
        if not material:
            return self._decision(
                "search_materials",
                "先检索与主题匹配的基础素材。",
                {"topic": task["topic"], "strategy": "standard"},
            )
        if not script:
            return self._decision(
                "generate_script",
                "素材检索成功，基于当前素材生成初始方案。",
                {"mode": "standard"},
            )
        if not estimate or estimate.get("script_id") != script.get("script_id"):
            return self._decision(
                "estimate_cost",
                "脚本已生成，核算费用并检查截止时间。",
            )
        if not (estimate["within_budget"] and estimate["within_deadline"]):
            if material["strategy"] != "budget_saving":
                failures = []
                if not estimate["within_budget"]:
                    failures.append("超预算")
                if not estimate["within_deadline"]:
                    failures.append("超出可用工时")
                return self._decision(
                    "search_materials",
                    f"费用检查发现{'、'.join(failures)}；改选轻量素材方案。",
                    {"topic": task["topic"], "strategy": "budget_saving"},
                )
            if script["mode"] != "concise":
                return self._decision(
                    "generate_script",
                    "轻量素材方案仍不满足约束；压缩生成步骤后重新估算。",
                    {"mode": "concise"},
                )
            return self._decision(
                "finish",
                "轻量且精简方案仍超出预算或截止时间，返回未完成及缺口。",
                status="incomplete",
            )
        return self._decision(
            "save_plan",
            "费用和截止时间均满足用户约束，保存方案草稿。",
        )

    @staticmethod
    def _decision(
        action: str,
        rationale: str,
        arguments: dict[str, Any] | None = None,
        status: str = "incomplete",
    ) -> dict[str, Any]:
        return {
            "action": action,
            "arguments": arguments or {},
            "rationale": rationale,
            "status": status,
        }


class OpenAICompatibleDecisionModel:
    """Optional JSON-mode adapter for OpenAI-compatible Chat Completions APIs."""

    def __init__(self, api_key: str, model: str | list[str], base_url: str, timeout: float = 30):
        models = [model] if isinstance(model, str) else list(model)
        models = [item.strip() for item in models if item and item.strip()]
        if not api_key or not models:
            raise ValueError("设置 OPENAI_API_KEY 和 OPENAI_MODEL 后才能使用 --model openai")
        self.api_key = api_key
        self.models = models
        self.model = models[0]
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def trace_name(self) -> str:
        return f"openai_compatible:{self.model}"

    def decide(self, context: dict[str, Any]) -> dict[str, Any]:
        system = (
            "你是内容任务规划 Agent 的下一步决策器。每次只根据当前任务、已成功结果和最近工具观察，"
            "选择一个动作。可选动作：search_materials(topic,strategy=standard|budget_saving)、"
            "generate_script(mode=standard|concise)、estimate_cost、save_plan、finish。"
            "先检索，再生成，再估算；若超预算或超截止时间，改选轻量素材或精简模式后重新估算；"
            "工具报错时根据错误调整或停止，并保留成功结果。只有预算与截止时间都满足时才保存；"
            "不得更改用户主题、预算、截止时间。未保存方案不得报告完成。"
            "输出 JSON 对象字段 action, arguments, rationale, status。rationale 只能是一句简短决策依据，"
            "不要输出隐藏思维过程。finish 的 status 只能是 completed 或 incomplete。"
        )
        for index, model_name in enumerate(self.models):
            body = {
                "model": model_name,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                    },
                ],
            }
            request = urllib.request.Request(
                self.base_url + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                content = payload["choices"][0]["message"]["content"]
                decision = json.loads(content)
                if not isinstance(decision, dict):
                    raise RuntimeError("模型决策响应必须是 JSON 对象")
                self.model = model_name
                return decision
            except urllib.error.HTTPError as exc:
                if exc.code in {400, 404} and index < len(self.models) - 1:
                    continue
                raise RuntimeError(f"模型决策请求失败 (HTTP {exc.code})") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"模型决策请求失败: {type(exc).__name__}") from None
        raise RuntimeError("所有配置的模型名称均不可用")


class PlannerAgent:
    def __init__(
        self,
        model: DecisionModel,
        catalog_path: str | Path,
        store_path: str | Path,
        max_tool_calls: int = 12,
    ):
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls 必须至少为 1")
        self.model = model
        self.catalog_path = Path(catalog_path)
        self.store_path = Path(store_path)
        self.max_tool_calls = max_tool_calls

    def run(self, task_input: dict[str, Any]) -> dict[str, Any]:
        validation = self._validate_input(task_input)
        if validation["status"] != "ready":
            return {
                "status": validation["status"],
                "missing_fields": validation["missing_fields"],
                "errors": validation["errors"],
                "trace": [],
                "tools_called": 0,
            }

        task = validation["task"]
        try:
            tools = BusinessTools(
                self.catalog_path,
                self.store_path,
                failures=task_input.get("simulate_tool_failures", {}),
            )
        except (ToolError, TypeError, ValueError) as exc:
            return {
                "status": "INCOMPLETE_TOOL_SETUP",
                "reason": str(exc),
                "trace": [],
                "tools_called": 0,
                "missing_fields": [],
            }

        state: dict[str, Any] = {
            "task": task,
            "available_hours": validation["available_hours"],
            "artifacts": {"material": None, "script": None, "estimate": None},
            "history": [],
            "failure_counts": {},
            "last_observation": None,
            "saved_plan": None,
            "trace": [],
            "steps": [],
            "tools_called": 0,
        }

        status = "INCOMPLETE"
        reason = "Agent 在未完成状态下停止。"
        while True:
            if state["tools_called"] >= self.max_tool_calls:
                status = "INCOMPLETE_STEP_LIMIT"
                reason = f"达到工具调用上限 {self.max_tool_calls}，任务未完成。"
                if state["trace"]:
                    state["trace"][-1]["next_action"] = {
                        "action": "stop",
                        "rationale": "已达到工具调用上限，未执行后续动作。",
                    }
                break
            try:
                decision = self.model.decide(self._model_context(state))
                self._validate_decision(decision)
            except Exception as exc:  # Model/API errors must produce an explicit incomplete result.
                status = "INCOMPLETE_MODEL_ERROR"
                reason = str(exc)
                break

            action = decision["action"]
            arguments = decision.get("arguments", {})
            rationale = str(decision.get("rationale", "模型未提供决策依据。"))[:240]
            if state["trace"]:
                state["trace"][-1]["next_action"] = {
                    "action": action,
                    "arguments": arguments,
                    "rationale": rationale,
                }

            if action == "finish":
                if state["saved_plan"]:
                    status = "COMPLETED"
                    reason = "方案草稿已保存。"
                else:
                    status = "INCOMPLETE_NO_FEASIBLE_PLAN"
                    reason = rationale or "模型判断当前约束下没有可保存方案。"
                break

            observation = self._execute(action, arguments, task, state, tools)
            state["tools_called"] += 1
            if observation["ok"]:
                self._apply_success(action, observation["data"], state)
                summary = self._summarize(action, observation["data"])
                step_status = "succeeded"
            else:
                state["failure_counts"][action] = state["failure_counts"].get(action, 0) + 1
                summary = {"error": observation["error"]}
                step_status = "failed"

            trace_step = {
                "step": state["tools_called"],
                "decision": {
                    "action": action,
                    "arguments": observation.get("arguments", arguments),
                    "rationale": rationale,
                },
                "tool_result": {"ok": observation["ok"], "summary": summary},
                "next_action": None,
            }
            state["trace"].append(trace_step)
            state["steps"].append(
                {
                    "step": state["tools_called"],
                    "action": action,
                    "status": step_status,
                    "result": summary,
                }
            )
            normalized_observation = {
                "ok": observation["ok"],
                "action": action,
                "arguments": observation.get("arguments", arguments),
                "data": observation.get("data"),
                "error": observation.get("error"),
            }
            state["last_observation"] = normalized_observation
            state["history"].append(normalized_observation)

        saved = state["saved_plan"]
        plan_data = saved.get("plan") if saved else self._draft_view(state)
        return {
            "status": status,
            "decision_model": getattr(self.model, "trace_name", type(self.model).__name__),
            "request_id": task["request_id"],
            "input": task,
            "available_hours": validation["available_hours"],
            "reason": reason,
            "missing_fields": [],
            "plan": plan_data,
            "saved": bool(saved),
            "duplicate": bool(saved and saved.get("duplicate")),
            "steps": state["steps"],
            "tools_called": state["tools_called"],
            "max_tool_calls": self.max_tool_calls,
            "trace": state["trace"],
        }

    def _execute(
        self,
        action: str,
        arguments: dict[str, Any],
        task: dict[str, Any],
        state: dict[str, Any],
        tools: BusinessTools,
    ) -> dict[str, Any]:
        artifacts = state["artifacts"]
        tool_arguments = dict(arguments)
        try:
            if action == "search_materials":
                topic = tool_arguments.get("topic", task["topic"])
                strategy = tool_arguments.get("strategy", "standard")
                if topic != task["topic"]:
                    raise ToolError("检索主题必须保持用户原始主题")
                data = tools.search_materials(topic, strategy)
                tool_arguments = {"topic": topic, "strategy": strategy}
            elif action == "generate_script":
                material = artifacts.get("material")
                if not material:
                    raise ToolError("生成脚本前必须先成功检索素材")
                mode = tool_arguments.get("mode", "standard")
                data = tools.generate_script(material, task["topic"], mode)
                tool_arguments = {"material_ids": [material["id"]], "mode": mode}
            elif action == "estimate_cost":
                material = artifacts.get("material")
                script = artifacts.get("script")
                if not material or not script:
                    raise ToolError("估算前必须先成功检索素材并生成脚本")
                data = tools.estimate_cost(
                    material, script, task["budget"], state["available_hours"]
                )
                tool_arguments = {"material_id": material["id"], "script_id": script["script_id"]}
            elif action == "save_plan":
                material = artifacts.get("material")
                script = artifacts.get("script")
                estimate = artifacts.get("estimate")
                if not material or not script or not estimate:
                    raise ToolError("保存前必须完成素材检索、脚本生成和费用估算")
                if not estimate["within_budget"] or not estimate["within_deadline"]:
                    raise ToolError("预算或截止时间未满足，拒绝保存方案")
                plan = self._build_plan(task, material, script, estimate)
                data = tools.save_plan(plan)
                tool_arguments = {
                    "request_id": task["request_id"],
                    "plan_id": data["plan"]["plan_id"],
                    "estimated_units": estimate["total_units"],
                }
            else:
                raise ToolError(f"不支持的工具动作: {action}")
            return {"ok": True, "data": data, "arguments": tool_arguments}
        except (ToolError, KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "arguments": tool_arguments}

    @staticmethod
    def _apply_success(action: str, data: dict[str, Any], state: dict[str, Any]) -> None:
        artifacts = state["artifacts"]
        if action == "search_materials":
            materials = data["materials"]
            artifacts["material"] = materials[0] if materials else None
            artifacts["script"] = None
            artifacts["estimate"] = None
        elif action == "generate_script":
            artifacts["script"] = data
            artifacts["estimate"] = None
        elif action == "estimate_cost":
            artifacts["estimate"] = data
        elif action == "save_plan":
            state["saved_plan"] = data

    @staticmethod
    def _summarize(action: str, data: dict[str, Any]) -> dict[str, Any]:
        if action == "search_materials":
            return {
                "strategy": data["strategy"],
                "found": len(data["materials"]),
                "material_ids": [material["id"] for material in data["materials"]],
            }
        if action == "generate_script":
            return {
                "script_id": data["script_id"],
                "mode": data["mode"],
                "source_material_ids": data["material_ids"],
                "task_count": len(data["tasks"]),
            }
        if action == "estimate_cost":
            return {
                "material_fee_units": data["material_fee_units"],
                "generation_fee_units": data["generation_fee_units"],
                "total_units": data["total_units"],
                "budget_units": data["budget_units"],
                "within_budget": data["within_budget"],
                "estimated_hours": data["estimated_hours"],
                "available_hours": data["available_hours"],
                "within_deadline": data["within_deadline"],
            }
        return {
            "saved": data["saved"],
            "duplicate": data["duplicate"],
            "plan_id": data["plan"]["plan_id"],
        }

    @staticmethod
    def _build_plan(
        task: dict[str, Any],
        material: dict[str, Any],
        script: dict[str, Any],
        estimate: dict[str, Any],
    ) -> dict[str, Any]:
        fingerprint_payload = {
            "topic": task["topic"],
            "budget": task["budget"],
            "deadline": task["deadline"],
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "request_id": task["request_id"],
            "fingerprint": fingerprint,
            "topic": task["topic"],
            "budget_units": task["budget"],
            "deadline": task["deadline"],
            "material_strategy": material["strategy"],
            "materials": [
                {
                    "id": material["id"],
                    "title": material["title"],
                    "facts": material["facts"],
                }
            ],
            "script": {
                "script_id": script["script_id"],
                "mode": script["mode"],
                "tasks": [{"name": name, "status": "planned"} for name in script["tasks"]],
            },
            "cost_estimate": estimate,
            "status": "saved_draft",
        }

    @staticmethod
    def _draft_view(state: dict[str, Any]) -> dict[str, Any] | None:
        material = state["artifacts"].get("material")
        script = state["artifacts"].get("script")
        estimate = state["artifacts"].get("estimate")
        if not (material and script):
            return None
        return {
            "topic": state["task"]["topic"],
            "material_ids": [material["id"]],
            "tasks": [{"name": name, "status": "planned"} for name in script["tasks"]],
            "cost_estimate": estimate,
            "status": "unsaved",
        }

    @staticmethod
    def _model_context(state: dict[str, Any]) -> dict[str, Any]:
        artifacts = state["artifacts"]
        return {
            "task": state["task"],
            "available_hours": state["available_hours"],
            "artifacts": artifacts,
            "last_observation": state["last_observation"],
            "failure_counts": state["failure_counts"],
            "saved_plan": state["saved_plan"],
            "history": state["history"],
        }

    @staticmethod
    def _validate_decision(decision: dict[str, Any]) -> None:
        if not isinstance(decision, dict):
            raise ValueError("模型决策必须为 JSON 对象")
        action = decision.get("action")
        if action not in TOOL_ACTIONS | {"finish"}:
            raise ValueError(f"模型选择了未知动作: {action}")
        if not isinstance(decision.get("arguments", {}), dict):
            raise ValueError("模型 arguments 必须为 JSON 对象")
        if action == "finish" and decision.get("status", "incomplete") not in {
            "completed",
            "incomplete",
        }:
            raise ValueError("finish.status 只能是 completed 或 incomplete")

    @staticmethod
    def _validate_input(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"status": "INVALID_INPUT", "missing_fields": [], "errors": ["输入必须是 JSON 对象"]}
        missing = [field for field in ("topic", "budget", "deadline") if field not in value]
        errors: list[str] = []
        topic = value.get("topic")
        if "topic" in value and (not isinstance(topic, str) or not topic.strip()):
            errors.append("topic 必须是非空字符串")
        budget = value.get("budget")
        if "budget" in value and (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or not math.isfinite(budget)
            or budget < 0
        ):
            errors.append("budget 必须是大于或等于 0 的数字")
        try:
            start_at = PlannerAgent._parse_datetime(value.get("start_at"))
            deadline = PlannerAgent._parse_datetime(value.get("deadline"))
            if start_at and deadline and deadline <= start_at:
                errors.append("deadline 必须晚于 start_at")
        except ValueError as exc:
            errors.append(str(exc))
            start_at = None
            deadline = None
        if missing:
            return {"status": "NEEDS_INPUT", "missing_fields": missing, "errors": errors}
        if errors:
            return {"status": "INVALID_INPUT", "missing_fields": [], "errors": errors}

        start_at = start_at or datetime.now(timezone.utc)
        assert deadline is not None
        available_hours = (deadline - start_at).total_seconds() / 3600
        request_id = value.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            return {
                "status": "INVALID_INPUT",
                "missing_fields": [],
                "errors": ["request_id 必须是非空字符串"],
            }
        task = {
            "topic": topic.strip(),
            "budget": float(budget),
            "deadline": deadline.isoformat(),
            "start_at": start_at.isoformat(),
        }
        canonical = json.dumps(
            {"topic": task["topic"], "budget": task["budget"], "deadline": task["deadline"]},
            ensure_ascii=False,
            sort_keys=True,
        )
        task["request_id"] = request_id or "req-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return {
            "status": "ready",
            "missing_fields": [],
            "errors": [],
            "task": task,
            "available_hours": available_hours,
        }

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("start_at 和 deadline 必须使用 ISO 8601 字符串")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("start_at 和 deadline 必须使用有效 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValueError("start_at 和 deadline 必须包含时区")
        return parsed


def model_from_environment(name: str) -> DecisionModel:
    if name == "mock":
        return OfflineMockDecisionModel()
    if name == "openai":
        return OpenAICompatibleDecisionModel(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get("OPENAI_MODEL", ""),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
    raise ValueError(f"未知模型类型: {name}")
