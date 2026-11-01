import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from src.main import create_model, load_api_config
from src.planner import OfflineMockDecisionModel, OpenAICompatibleDecisionModel, PlannerAgent


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "campaign.json"


def task(**overrides):
    value = {
        "request_id": "test-normal-001",
        "topic": "校园迎新活动",
        "budget": 10,
        "start_at": "2026-11-01T09:00:00Z",
        "deadline": "2026-11-01T20:00:00Z",
    }
    value.update(overrides)
    return value


class PlannerTests(unittest.TestCase):
    def make_agent(self, store, max_tool_calls=12):
        return PlannerAgent(
            OfflineMockDecisionModel(), CATALOG, store, max_tool_calls=max_tool_calls
        )

    def test_normal_replans_after_budget_observation_and_saves(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_agent(Path(temp) / "plans.json").run(task())

        self.assertEqual(result["status"], "COMPLETED")
        self.assertTrue(result["saved"])
        estimates = [
            step["tool_result"]["summary"]["total_units"]
            for step in result["trace"]
            if step["decision"]["action"] == "estimate_cost"
        ]
        self.assertEqual(estimates, [12, 8])
        first_estimate = result["trace"][2]
        self.assertEqual(first_estimate["next_action"]["action"], "search_materials")
        self.assertEqual(
            first_estimate["next_action"]["arguments"]["strategy"], "budget_saving"
        )
        self.assertEqual(result["plan"]["cost_estimate"]["total_units"], 8)

    def test_missing_budget_and_deadline_returns_missing_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_agent(Path(temp) / "plans.json").run({"topic": "校园迎新"})
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertEqual(result["missing_fields"], ["budget", "deadline"])
        self.assertEqual(result["tools_called"], 0)

    def test_generation_failure_changes_action_and_keeps_material(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_agent(Path(temp) / "plans.json").run(
                task(simulate_tool_failures={"generate_script": 1})
            )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["trace"][1]["tool_result"]["ok"], False)
        self.assertEqual(result["trace"][1]["next_action"]["arguments"]["mode"], "concise")
        self.assertEqual(
            result["trace"][0]["tool_result"]["summary"]["material_ids"],
            result["trace"][2]["decision"]["arguments"]["material_ids"],
        )
        self.assertEqual(result["plan"]["cost_estimate"]["total_units"], 9)

    def test_budget_and_deadline_constraints_can_leave_task_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_agent(Path(temp) / "plans.json").run(task(budget=6))
        self.assertEqual(result["status"], "INCOMPLETE_NO_FEASIBLE_PLAN")
        self.assertFalse(result["saved"])
        self.assertEqual(result["plan"]["cost_estimate"]["total_units"], 7)
        self.assertTrue(
            any(
                step["decision"]["action"] == "search_materials"
                and step["decision"]["arguments"].get("strategy") == "budget_saving"
                for step in result["trace"]
            )
        )

    def test_deadline_observation_triggers_compact_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_agent(Path(temp) / "plans.json").run(
                task(budget=20, deadline="2026-11-01T13:00:00Z")
            )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["plan"]["script"]["mode"], "concise")
        self.assertEqual(result["plan"]["cost_estimate"]["estimated_hours"], 4)

    def test_transient_save_failure_resumes_from_successful_estimate(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_agent(Path(temp) / "plans.json").run(
                task(simulate_tool_failures={"save_plan": 1})
            )
        self.assertEqual(result["status"], "COMPLETED")
        save_steps = [
            step for step in result["trace"] if step["decision"]["action"] == "save_plan"
        ]
        self.assertEqual(save_steps[0]["tool_result"]["ok"], False)
        self.assertEqual(save_steps[0]["next_action"]["action"], "save_plan")
        self.assertEqual(save_steps[1]["tool_result"]["summary"]["duplicate"], False)

    def test_duplicate_request_creates_only_one_saved_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "plans.json"
            first = self.make_agent(store).run(task())
            second = self.make_agent(store).run(task())
            records = json.loads(store.read_text(encoding="utf-8"))["plans"]
        self.assertEqual(first["status"], "COMPLETED")
        self.assertEqual(second["status"], "COMPLETED")
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(records), 1)

    def test_generated_request_id_is_stable_without_optional_start_time(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "plans.json"
            value = {
                "topic": "校园迎新活动",
                "budget": 10,
                "deadline": "2099-11-01T20:00:00Z",
            }
            first = self.make_agent(store).run(value)
            second = self.make_agent(store).run(value)
            records = json.loads(store.read_text(encoding="utf-8"))["plans"]
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(records), 1)

    def test_tool_call_limit_returns_explicit_incomplete_status(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_agent(Path(temp) / "plans.json", max_tool_calls=2).run(task())
        self.assertEqual(result["status"], "INCOMPLETE_STEP_LIMIT")
        self.assertEqual(result["tools_called"], 2)
        self.assertEqual(result["trace"][-1]["next_action"]["action"], "stop")

    def test_no_matching_material_stops_with_insufficient_result(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.make_agent(Path(temp) / "plans.json").run(task(topic="火星探测器"))
        self.assertEqual(result["status"], "INCOMPLETE_NO_FEASIBLE_PLAN")
        self.assertEqual(result["tools_called"], 1)
        self.assertFalse(result["saved"])

    def test_model_cannot_change_the_original_topic_or_save_over_budget(self):
        class InvalidDecisionModel:
            def __init__(self):
                self.calls = 0

            def decide(self, context):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "action": "search_materials",
                        "arguments": {"topic": "another topic", "strategy": "standard"},
                        "rationale": "invalid topic",
                    }
                return {"action": "finish", "arguments": {}, "rationale": "stop"}

        with tempfile.TemporaryDirectory() as temp:
            agent = PlannerAgent(InvalidDecisionModel(), CATALOG, Path(temp) / "plans.json")
            result = agent.run(task())
        self.assertEqual(result["steps"][0]["status"], "failed")
        self.assertIn("原始主题", result["steps"][0]["result"]["error"])
        self.assertFalse(result["saved"])

    def test_next_model_decision_receives_the_previous_tool_observation(self):
        class ObservationDrivenModel:
            def __init__(self):
                self.contexts = []

            def decide(self, context):
                self.contexts.append(context)
                observation = context["last_observation"]
                if observation is None:
                    return {
                        "action": "search_materials",
                        "arguments": {"strategy": "standard"},
                        "rationale": "先检索标准素材。",
                    }
                return {
                    "action": "search_materials",
                    "arguments": {"strategy": "budget_saving"},
                    "rationale": "标准检索失败，切换轻量检索。",
                }

        with tempfile.TemporaryDirectory() as temp:
            model = ObservationDrivenModel()
            result = PlannerAgent(
                model, CATALOG, Path(temp) / "plans.json", max_tool_calls=2
            ).run(task(simulate_tool_failures={"search_materials": 1}))

        self.assertFalse(model.contexts[1]["last_observation"]["ok"])
        self.assertEqual(model.contexts[1]["last_observation"]["action"], "search_materials")
        self.assertIn("模拟工具故障", model.contexts[1]["last_observation"]["error"])
        self.assertEqual(len(model.contexts[1]["history"]), 1)
        self.assertEqual(result["trace"][0]["next_action"]["arguments"]["strategy"], "budget_saving")

    def test_same_request_id_with_different_content_does_not_overwrite_saved_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "plans.json"
            agent = self.make_agent(store)
            first = agent.run(task())
            conflicting = agent.run(task(topic="新生季"))
            saved_records = json.loads(store.read_text(encoding="utf-8"))["plans"]

        self.assertTrue(first["saved"])
        self.assertEqual(conflicting["status"], "INCOMPLETE_NO_FEASIBLE_PLAN")
        self.assertFalse(conflicting["saved"])
        self.assertIn("请求内容不同", conflicting["trace"][-1]["tool_result"]["summary"]["error"])
        self.assertEqual(len(saved_records), 1)
        self.assertEqual(saved_records[0]["topic"], "校园迎新活动")

    def test_storage_write_failure_is_not_reported_as_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            blocked_parent = Path(temp) / "not-a-directory"
            blocked_parent.write_text("file blocks parent directory creation", encoding="utf-8")
            result = self.make_agent(blocked_parent / "plans.json").run(task())

        self.assertEqual(result["status"], "INCOMPLETE_NO_FEASIBLE_PLAN")
        self.assertFalse(result["saved"])
        failed_saves = [
            step for step in result["trace"]
            if step["decision"]["action"] == "save_plan" and not step["tool_result"]["ok"]
        ]
        self.assertEqual(len(failed_saves), 2)
        self.assertTrue(all("保存方案失败" in step["tool_result"]["summary"]["error"] for step in failed_saves))

    def test_api_config_supports_tab_separated_local_file(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "apikey.txt"
            config_path.write_text(
                "base_url (OpenAI)\thttps://api.example.test/v1\n"
                "api_key\tunit-test-secret\n"
                "model\tmodel-a / model-b\n",
                encoding="utf-8",
            )
            config = load_api_config(str(config_path))
            with patch.dict("os.environ", {}, clear=True):
                model = create_model("openai", str(config_path))
        self.assertEqual(config["apikey"], "unit-test-secret")
        self.assertIsInstance(model, OpenAICompatibleDecisionModel)
        self.assertEqual(model.models, ["model-a", "model-b"])
        self.assertEqual(model.base_url, "https://api.example.test/v1")

    def test_api_model_falls_back_when_first_configured_model_is_unsupported(self):
        response_body = json.dumps(
            {"choices": [{"message": {"content": '{"action":"finish","arguments":{},"rationale":"stop","status":"incomplete"}'}}]}
        ).encode("utf-8")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return response_body

        model = OpenAICompatibleDecisionModel("test-secret", ["bad-model", "working-model"], "https://api.example.test/v1")
        with patch(
            "src.planner.urllib.request.urlopen",
            side_effect=[HTTPError("https://api.example.test", 404, "not found", {}, None), Response()],
        ) as urlopen:
            decision = model.decide({"task": {"topic": "test"}})
        self.assertEqual(decision["action"], "finish")
        self.assertEqual(model.trace_name, "openai_compatible:working-model")
        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
