import json
import tempfile
import unittest
from pathlib import Path

from src.planner import OfflineMockDecisionModel, PlannerAgent


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


if __name__ == "__main__":
    unittest.main()
