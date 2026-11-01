# 03 评估

自动化测试：`python -m unittest discover -s tests -v`，共 11 项，覆盖正常重规划、缺少输入、工具失败续跑、预算不可行、截止时间重规划、保存失败、重复请求、自动请求 ID、调用上限、无匹配素材和主题边界。实际命令输出保存在 `examples/test-results.txt`。

运行记录为离线 Mock 的实际输出，不是大模型生成质量证明：

| 场景 | 输入与实际输出 | 核验点 |
|---|---|---|
| 正常完成、费用超限后替代 | `examples/normal.input.json`、`examples/normal.output.json` | 初始估算 12，动作改为轻量检索；再估算 8 后保存草稿 |
| 信息不足 | `examples/missing.input.json`、`examples/missing.output.json` | 返回缺少预算与截止时间，工具调用为 0 |
| 工具失败后续跑 | `examples/tool-failure.input.json`、`examples/tool-failure.output.json` | 脚本生成失败后以精简模式重做，保留先前素材结果 |
| 预算不可行 | `examples/over-budget.input.json`、`examples/over-budget.output.json` | 最轻方案仍超预算，状态未完成且未保存 |
| 截止时间紧 | `examples/deadline-tight.input.json`、`examples/deadline-tight.output.json` | 根据估算切换轻量素材和精简生成后再检查 |
| 重复执行 | `examples/duplicate.input.json`、`examples/duplicate.output.json` | 使用与正常用例相同的存储及 request ID，返回重复标记，存储只有一份 |

另外，在本环境配置的 OpenAI 兼容服务上实际运行了两条模型路径：`examples/normal.openai.output.json` 和 `examples/tool-failure.openai.output.json`。输出顶层的 `decision_model` 记录模型适配器名称；正常路径实际观察到 `12 -> 7` 并保存，故障路径观察到一次 `generate_script` 失败后切换精简模式并保存。该服务配置属于当前执行环境，不包含在仓库，也不构成评审环境可用性的保证。

所有 Trace 均包含动作、规范化工具参数、结果摘要、下一动作和简短决策依据。决策依据是可审计摘要，不包含模型内部思维过程。

## 限制与失败边界

- 运行记录全部由离线 Mock 生成；没有配置真实模型凭据，未实测真实模型是否能稳定选对动作。
- `simulate_tool_failures` 是测试注入，不连接真实素材服务、计费系统或数据库。
- 本地 JSON 存储适用于单进程演示；没有跨进程锁，不能作为并发生产存储。
- 合成素材有限；无匹配记录会明确未完成，未实现通用主题检索。
- 保存的是待运营审核的计划草稿，计划内任务状态为 `planned`，不代表已执行。
