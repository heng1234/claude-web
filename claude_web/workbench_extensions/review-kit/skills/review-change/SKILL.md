---
name: review-change
description: 审查当前项目改动，定位有证据的缺陷和回归风险。用于用户请求 code review、提交前审查或检查 Diff。
---

读取当前项目指引与 Diff，结合调用方和测试验证可达路径。按 references/checklist.md 检查。
只报告能够具体说明触发条件和影响的问题，给出文件与行号；区分缺陷和建议。
不修改文件。不执行会改变 Git 工作区、依赖、数据库或外部系统的操作。
