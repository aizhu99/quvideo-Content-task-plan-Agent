# 内容任务规划 Agent（题目 2）

为运营人员根据主题、预算和截止时间规划内容活动。Agent 会检索合成素材、生成方案步骤、核算费用与工时，并只保存满足约束的方案草稿；不会执行真实发布。

## 环境与运行

- Python 3.10 或更高版本
- 无第三方依赖，无需 API 密钥即可运行离线 Mock

在仓库根目录执行：

```powershell
python -m src.main --input examples/normal.input.json --store .runtime/normal.json
python -m unittest discover -s tests -v
```

程序输出 JSON。业务未完成状态也以 JSON 返回；输入文件损坏或参数配置错误时返回非零退出码。`--max-tool-calls` 默认 12，达到上限返回 `INCOMPLETE_STEP_LIMIT`。方案默认写入被 Git 忽略的 `.runtime/plans.json`；可以用 `--store` 指定路径。

运行其他验证场景：

```powershell
python -m src.main --input examples/missing.input.json --store .runtime/missing.json
python -m src.main --input examples/tool-failure.input.json --store .runtime/tool-failure.json
python -m src.main --input examples/search-failure.input.json --store .runtime/search-failure.json
python -m src.main --input examples/over-budget.input.json --store .runtime/over-budget.json
python -m src.main --input examples/deadline-tight.input.json --store .runtime/deadline.json
```

## 决策模型

默认 `mock` 只用于离线检查流程、工具参数和状态处理，不证明真实模型的决策质量。真实模型模式默认读取仓库根目录的本地 `apikey.txt`（已加入 `.gitignore`，不会提交），支持制表符分隔或 `key=value` 格式：

```text
base_url (OpenAI)=https://api.example.com/v1
api_key=本地密钥
model=model-a / model-b
```

CLI 会按顺序尝试配置的模型名；遇到 HTTP 400/404 时尝试下一候选，并在输出 Trace 中记录实际模型名。进程环境变量 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 优先于文件配置。也可继续用环境变量显式配置：

```powershell
$env:OPENAI_API_KEY = "你的密钥"
$env:OPENAI_BASE_URL = "https://api.openai.com/v1"
$env:OPENAI_MODEL = "支持 JSON mode 的模型名"
python -m src.main --model openai --api-config apikey.txt --input examples/normal.input.json
```

也可设置 `AGENT_MODEL=openai`。密钥只从本地忽略的配置文件或进程环境读取，不写入仓库或 Trace。模型不可用时会明确返回未完成状态，不会静默降级为 Mock。

## 评审入口

| 内容 | 文件 |
|---|---|
| 需求与成功标准 | [docs/01-requirements.md](docs/01-requirements.md) |
| 调用流程、动态决策与取舍 | [docs/02-solution.md](docs/02-solution.md) |
| 验收结果与限制 | [docs/03-evaluation.md](docs/03-evaluation.md) |
| AI 辅助及人工验证记录 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 难点、收获和下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |
| 可执行测试 | [tests/test_planner.py](tests/test_planner.py) |
| 合成费用和素材 | [data/campaign.json](data/campaign.json) |
| 实际运行 Trace | [examples/](examples/) |

本项目未使用 Agent 框架；模型 API 适配、工具执行、状态机和 JSON 文件存储均由标准库实现。费用单位为模拟单位，不代表真实报价。

## 交付信息

- 题号：2，内容任务规划 Agent
- GitHub URL：https://github.com/aizhu99/quvideo-Content-task-plan-Agent
- 分支：`master`
- 最终 commit SHA：运行 `git rev-parse HEAD` 获取，并随提交信息一并提供
