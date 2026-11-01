# 03 评估

自动化测试：`python -m unittest discover -s tests -v`，共 13 项，覆盖正常重规划、缺少输入、工具失败续跑、预算不可行、截止时间重规划、保存失败、重复请求、自动请求 ID、调用上限、无匹配素材、主题边界、本地 API 配置解析和候选模型回退。实际命令输出保存在 `examples/test-results.txt`。

下表输入输出由离线 Mock 实际运行生成，用于验证编排、工具契约与状态边界，不代表真实模型决策质量：

| 场景 | 输入与实际输出 | 核验点 |
|---|---|---|
| 正常完成、费用超限后替代 | `examples/normal.input.json`、`examples/normal.output.json` | 初始估算 12，动作改为轻量检索；再估算 8 后保存草稿 |
| 信息不足 | `examples/missing.input.json`、`examples/missing.output.json` | 返回缺少预算与截止时间，工具调用为 0 |
| 工具失败后续跑 | `examples/tool-failure.input.json`、`examples/tool-failure.output.json` | 脚本生成失败后以精简模式重做，保留先前素材结果 |
| 预算不可行 | `examples/over-budget.input.json`、`examples/over-budget.output.json` | 最轻方案仍超预算，状态未完成且未保存 |
| 截止时间紧 | `examples/deadline-tight.input.json`、`examples/deadline-tight.output.json` | 根据估算切换轻量素材和精简生成后再检查 |
| 重复执行 | `examples/duplicate.input.json`、`examples/duplicate.output.json` | 使用与正常用例相同的存储及 request ID，返回重复标记，存储只有一份 |

另直接从本地 `apikey.txt` 加载配置，在 OpenAI 兼容服务上实测了两条真实模型决策路径：`examples/normal.apikey.output.json` 和 `examples/tool-failure.apikey.output.json`。首选模型不可用后切换到 `deepseek-v4-pro`；正常输入完成保存，工具失败路径观察到一次 `generate_script` 失败后改用精简模式并保存。输出顶层的 `decision_model` 记录实际采用的模型名。该实测使用真实模型做动作选择，但素材检索、脚本生成、成本估算和草稿保存仍是本地合成工具。API 配置解析和候选模型回退也由自动化单测覆盖。API 凭据存放于本地忽略文件，不在仓库内。

所有 Trace 均包含动作、规范化工具参数、结果摘要、下一动作和简短决策依据。决策依据是可审计摘要，不包含模型内部思维过程。

## 限制与失败边界

- 两条 API Trace 是真实模型的有限样本，不能证明多次运行或其他主题下的决策稳定性；离线 Mock 只证明流程与工具边界。
- 评审环境没有仓库内的 API 凭据；真实模型路径需由评审者另配凭据，离线 Mock 始终可运行。
- `simulate_tool_failures` 是测试注入，不连接真实素材服务、计费系统或数据库。
- 本地 JSON 存储适用于单进程演示；没有跨进程锁，不能作为并发生产存储。
- 合成素材有限；无匹配记录会明确未完成，未实现通用主题检索。
- 保存的是待运营审核的计划草稿，计划内任务状态为 `planned`，不代表已执行。
