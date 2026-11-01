"""CLI entry point for the content task planning agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .planner import OfflineMockDecisionModel, OpenAICompatibleDecisionModel, PlannerAgent

ROOT = Path(__file__).resolve().parent.parent


def load_api_config(path: str | None) -> dict[str, str]:
    if not path or not Path(path).exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            key, value = line.split("\t", 1)
        elif "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        normalized = key.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
        values[normalized] = value.strip()
    return values


def create_model(model_name: str, config_path: str | None):
    if model_name == "mock":
        return OfflineMockDecisionModel()
    config = load_api_config(config_path)
    api_key = os.environ.get("OPENAI_API_KEY") or config.get("apikey", "")
    base_url = os.environ.get("OPENAI_BASE_URL") or config.get("baseurl(openai)") or config.get("baseurl", "https://api.openai.com/v1")
    model_value = os.environ.get("OPENAI_MODEL") or config.get("model", "")
    models = [item.strip() for item in model_value.split("/") if item.strip()]
    if not model_value:
        raise ValueError("配置文件或环境变量中缺少 OPENAI_MODEL/model")
    return OpenAICompatibleDecisionModel(api_key, models, base_url)


def load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="内容任务规划 Agent")
    parser.add_argument("--input", required=True, help="任务 JSON 文件路径，使用 - 从标准输入读取")
    parser.add_argument(
        "--model",
        choices=("mock", "openai"),
        default=os.environ.get("AGENT_MODEL", "mock"),
        help="决策模型，默认为离线 mock",
    )
    parser.add_argument(
        "--api-config",
        default="apikey.txt",
        help="OpenAI 兼容 API 配置文件，默认读取仓库根目录 apikey.txt",
    )
    parser.add_argument(
        "--store",
        default=str(ROOT / ".runtime" / "plans.json"),
        help="幂等方案存储路径",
    )
    parser.add_argument("--max-tool-calls", type=int, default=12, help="单次任务工具调用上限")
    parser.add_argument("--output", help="可选，将完整 JSON 结果另存为 UTF-8 文件")
    args = parser.parse_args(argv)

    try:
        task_input = load_json(args.input)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "INVALID_INPUT", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        model = create_model(args.model, args.api_config)
        agent = PlannerAgent(
            model=model,
            catalog_path=ROOT / "data" / "campaign.json",
            store_path=args.store,
            max_tool_calls=args.max_tool_calls,
        )
    except ValueError as exc:
        print(json.dumps({"status": "CONFIG_ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    result = agent.run(task_input)

    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        try:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            print(json.dumps({"status": "OUTPUT_ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
