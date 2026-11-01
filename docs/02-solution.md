# 02 方案

## 执行链

`src/main.py` 读取 JSON 并组装运行环境；`src/planner.py` 将输入、成功产物、失败计数和最近工具观察交给决策模型。模型返回一个受限动作，Agent 执行对应业务工具，再把观察结果交回模型，直到保存成功、模型选择停止或达到工具调用上限。

可选动作是 `search_materials`、`generate_script`、`estimate_cost`、`save_plan` 和 `finish`。模型只可选下一步；Agent 仍校验动作依赖、用户原始主题、费用与截止时间。没有成功素材不能生成脚本，没有有效估算不能保存，超约束方案不能保存。

## 动态决策实例

- 离线 Mock 的预算场景：基础素材方案费用为 `4 + 8 = 12`，观察到超预算后改选轻量素材；费用降到 `3 + 5 = 8` 后保存。此场景用于可重复验证编排，Mock 本身不证明模型决策质量。
- 真实 API 的素材检索故障场景：标准检索失败后，模型依据工具错误把 `strategy` 从 `standard` 改成 `budget_saving`；见 `examples/search-failure.apikey.output.json`。
- 真实 API 的脚本生成故障场景：标准生成失败后，模型保留已检索素材并把 `mode` 从 `standard` 改成 `concise`；见 `examples/tool-failure.apikey.output.json`。
- 离线 Mock 的紧截止时间场景：估算超时后改用轻量素材与精简生成，再次估算并决定保存；见 `examples/deadline-tight.output.json`。

每轮循环会把任务、已成功产物、完整工具历史和最近一次观察的深拷贝交给模型；模型返回下一动作后由 Agent 执行工具，再将结果交回下一轮模型调用。真实 API 的两条失败路径展示了不同观察导致的不同动作参数。默认离线决策器是确定性 Mock，用于测试可重复的流程与状态边界；`OpenAICompatibleDecisionModel` 使用 OpenAI 兼容 Chat Completions JSON mode。CLI 可从被忽略的 `apikey.txt` 读取模型候选、base URL 和 API key；遇到 400/404 会有限地尝试下一候选，环境变量可覆盖文件值。

## 工具与状态

业务工具位于 `src/tools.py`，测试可通过 `simulate_tool_failures` 注入一次性故障。每次工具动作都保留独立成功/失败状态；脚本失败不会清除已检索素材，重新检索会清除与旧素材关联的脚本和估算。

保存记录含请求指纹与稳定 `plan_id`。同 `request_id`、同请求内容返回已有方案；同 ID 不同内容会失败。写盘使用同目录临时文件加原子替换。默认存储在 `.runtime/plans.json`，可以通过 `--store` 改路径。

## 技术取舍

- Python 标准库实现 CLI、HTTP 请求、JSON 存储和测试，不增加依赖，便于评审环境离线运行。
- 不采用 Agent 框架；动作协议和受控状态较小，显式状态更方便核验预算边界与工具失败。
- 采用 OpenAI 兼容 Chat Completions JSON mode 作为真实模型接口，避免将特定 SDK 作为运行前提。
- 单次最多调用 12 个业务工具；上限可通过 `--max-tool-calls` 调整。到达上限总是返回未完成状态。
- 合成数据只有“校园迎新”一个主题，不能代表任意活动的素材覆盖率或真实价格。
