# Agent Platform

基于 FastAPI、React、LangGraph 和 LiteLLM 的多租户 Agent 平台。

项目源码位于 [`agent_platform/`](agent_platform/)，模型网关使用的共享代码位于
[`chat_models/`](chat_models/)。功能介绍、配置、部署和运维说明见
[项目文档](agent_platform/README.md)。

## 本地准备

在仓库根目录运行：

```bash
uv sync --frozen --all-groups
npm --prefix agent_platform/apps/web ci
cp agent_platform/.env.example agent_platform/.env
```

根据 [项目文档](agent_platform/README.md) 配置本地环境。`.env`、运行数据、
数据库和依赖目录不会提交到 Git。

## 常用命令

```bash
make -C agent_platform help
make -C agent_platform build
make -C agent_platform test
make -C agent_platform charts-validate
```

部署命令和 Kubernetes 环境要求见 [本地 Kubernetes 部署说明](agent_platform/docs/kubernetes-local.md)。
