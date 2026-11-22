# 模型与路由验收记录 · 2026-11-18

此前只覆盖配置、加密和部分协议适配，没有验证完整页面链路。本次补做实际本机 HTTP、浏览器和真实 Agent SDK 验证；测试目标使用本机模拟服务，不以此声称任何真实供应商已通过验证。

## 补测发现并修复

1. 聊天框中残留的 Claude 模型别名会覆盖第三方服务的路由模型。现在第三方请求使用配置档、路由或服务中的模型 ID；显式 API 覆盖需同时指定匹配的 `model_provider_id`。
2. 修改默认路由会改变旧会话的模型。现在会话保留服务与模型，新路由对新会话生效。
3. 原「测试连接」仅请求模型列表。现在改成短推理检测，并可测试路由表单里所选的具体模型，页面保留实际回复结果。真实服务可能产生少量费用。
4. `finish_reason=length` 的截断输出可能被标为成功。现在标记未完成并保留已收到的文本。
5. 会话后的追问建议、标题与 Chat 压缩原来绕过绑定，调用默认 Claude CLI。现在沿用会话服务及模型；相关回归测试禁止 CLI fallback。

## 实际验收

| 链路 | 观察到的结果 |
| --- | --- |
| 页面添加 OpenAI 兼容服务 | 本机服务保存成功，凭据仅显示“已保存” |
| 保存 Chat 路由 | 指定 `route-model`，服务默认模型为不同的 `provider-model` |
| 页面测试所选模型 | 返回 `LOCAL_INFERENCE_OK`，模拟服务确认收到 `route-model` 的非流式请求 |
| 新建 Chat 并发送 | 页面显示 `LOCAL_CHAT_ROUTING_OK`；服务确认收到 `/v1/chat/completions`、流式参数与路由模型 |
| 自动追问建议 | 页面显示模拟服务返回的问题；服务记录确认仍使用 `route-model` |
| Anthropic 兼容短推理 | 本机 `/v1/messages` 返回文本；配置的模型 ID 到达服务 |
| 真实安装 Agent SDK 工具回合 | SDK 收到模拟服务的 Read 工具调用，读取临时 `probe.txt`；`tool_result` 含实际文件文字 `LOCAL_READ_PROBE_CONTENT`；最终 `done.success=true` |

模型路由本机 HTTP 测试 7 项通过，完整 Python 测试 395 项通过，前端状态测试 4 项通过。

自动化入口：

```bash
NO_PROXY=127.0.0.1,localhost python -m unittest discover -s tests -p test_model_routing.py
node --test tests/workbench_state.test.cjs
```

真实 SDK 验收使用本机已安装版本和独立测试目录；没有把任何模拟服务配置添加到正式应用数据。验收原始事件、请求摘要和浏览器截图保存在 `/tmp/ccw-model-routing-e2e/` 及 `/tmp/ccw-model-routing-*-proof.jpg`，临时文件可能由系统清理。

## 结论边界

可以确认服务选择、模型选择、请求鉴权、响应适配和 SDK 工具转发主链路能工作。模拟服务只能证明协议与链路，不能证明某个真实模型的推理质量、Key 权限、计费、限流、工具支持或供应商兼容程度。实际服务仍需用用户明确指定的 URL、模型 ID 和凭据验证。
