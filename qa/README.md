# 独立协作 QA 样例

这是为跨客户端协作验证生成的测试材料，不包含研究数据，不代表任何真实业务结果。

- case_id: TEAM-QA-20261117
- continuity_marker: ag-chief-blue-lantern-47
- values: 17, 26
- expected_sum: 43

测试工作者只需读取本文件并返回 case_id、continuity_marker 和 values 的和。第二轮只能使用该工作者前轮上下文回忆 marker，并将前轮的和加 5；第二轮不要再次读取文件。

成功标准为真实模型两轮返回和同一官方会话 ID。由 Codex 宿主启动的探针只验证官方工作者能力，不能当作 Antigravity 自动委派验收。
