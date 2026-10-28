ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
ENV_FILE ?= $(ROOT)/agent_platform/.env
PORT ?= 8000
HOST ?= 127.0.0.1
GATEWAY ?= managed
LITELLM_PORT ?= 4000
STATE_DIR ?= $(ROOT)/agent_platform/.data/local
ENV_PATH := $(abspath $(ENV_FILE))
STATE_PATH := $(abspath $(STATE_DIR))
COMPOSE := docker compose --env-file "$(ENV_PATH)" -f "$(ROOT)/agent_platform/deploy/compose.yml"
LOCAL := uv run --no-sync python -m agent_platform.scripts.local
KUBE_CONTEXT ?= orbstack
KUBE_NAMESPACE ?= agent-platform
KUBE_IMAGE ?= agent-platform:0.3.0-helm
KUBE_ARGS ?=
SERVICE ?= all
KUBE := uv run --no-sync python -m agent_platform.scripts.helm
KUBE_IMPORT := uv run --no-sync python -m agent_platform.scripts.kubernetes
KUBE_OPTIONS := --context "$(KUBE_CONTEXT)" --namespace "$(KUBE_NAMESPACE)" --image "$(KUBE_IMAGE)" --env-file "$(ENV_PATH)"

.DEFAULT_GOAL := start

.PHONY: api-contract api-contract-check start run stop resume deploy status help install migrate init dev worker gateway-sync maintenance web build test infra gateway-key up down k8s-import k8s-deploy k8s-status k8s-stop k8s-logs helm-adopt charts-validate local-start local-stop local-status local-migrate local-init compose-up compose-down

start: k8s-deploy

local-start:
	@cd "$(ROOT)" && $(LOCAL) start --env-file "$(ENV_PATH)" --state-dir "$(STATE_PATH)" --host "$(HOST)" --port "$(PORT)" --gateway "$(GATEWAY)" --gateway-port "$(LITELLM_PORT)"

run: start

stop: k8s-stop

local-stop:
	@cd "$(ROOT)" && $(LOCAL) stop --state-dir "$(STATE_PATH)"

status: k8s-status

local-status:
	@cd "$(ROOT)" && $(LOCAL) status --state-dir "$(STATE_PATH)"

help:
	@echo 'make k8s-import       首次将已有本机数据迁入 OrbStack Kubernetes'
	@echo 'make helm-adopt       将已有 Kubernetes 资源接管为独立 Helm releases'
	@echo 'make start / up       构建并独立更新服务；数据库迁移单独执行'
	@echo 'make deploy SERVICE=worker  仅构建并发布 Worker'
	@echo 'make migrate          显式迁移 Job；本地停应用写入，完成后 make resume'
	@echo 'make status / stop    查看集群状态 / 停止工作负载并保留数据卷'
	@echo 'make stop / resume SERVICE=worker  单独停止 / 恢复服务，保留发布配置'
	@echo 'make k8s-logs SERVICE=api           查看指定服务日志'
	@echo 'make charts-validate  离线 lint 和渲染全部 Charts'
	@echo 'make start KUBE_ARGS=--skip-build  使用已有本地镜像'
	@echo 'make dev / web        分别运行后端 / Vite 热更新前端'
	@echo '旧 supervisor 入口为 local-start / local-stop / local-status；切库后不得直接启动旧数据副本'

k8s-import:
	@cd "$(ROOT)" && $(KUBE_IMPORT) import-local $(KUBE_OPTIONS) $(KUBE_ARGS)
	@cd "$(ROOT)" && $(KUBE) adopt $(KUBE_OPTIONS) $(KUBE_ARGS)

k8s-deploy:
	@cd "$(ROOT)" && $(KUBE) deploy --service "$(SERVICE)" $(KUBE_OPTIONS) $(KUBE_ARGS)

deploy: k8s-deploy

helm-adopt:
	@cd "$(ROOT)" && $(KUBE) adopt --service "$(SERVICE)" $(KUBE_OPTIONS) $(KUBE_ARGS)

charts-validate:
	@cd "$(ROOT)" && $(KUBE) validate --service "$(SERVICE)" $(KUBE_OPTIONS) --skip-build

k8s-status:
	@cd "$(ROOT)" && $(KUBE) status $(KUBE_OPTIONS)

k8s-stop:
	@cd "$(ROOT)" && $(KUBE) stop --service "$(SERVICE)" $(KUBE_OPTIONS)

resume:
	@cd "$(ROOT)" && $(KUBE) resume --service "$(SERVICE)" $(KUBE_OPTIONS)

k8s-logs:
	@cd "$(ROOT)" && $(KUBE) logs --service "$(SERVICE)" $(KUBE_OPTIONS)

install:
	cd "$(ROOT)" && uv sync --frozen --all-groups
	npm --prefix "$(ROOT)/agent_platform/apps/web" ci

migrate:
	@cd "$(ROOT)" && $(KUBE) migrate $(KUBE_OPTIONS) $(KUBE_ARGS)

init: k8s-import

local-migrate:
	cd "$(ROOT)" && uv run --env-file "$(ENV_PATH)" alembic -c agent_platform/alembic.ini upgrade head

local-init: local-migrate
	cd "$(ROOT)" && uv run --env-file "$(ENV_PATH)" python -m agent_platform init

dev:
	cd "$(ROOT)" && uv run --no-sync python -m agent_platform.scripts.runtime serve --env-file "$(ENV_PATH)" --host "$(HOST)" --port "$(PORT)"

worker:
	cd "$(ROOT)" && uv run --no-sync python -m agent_platform.scripts.runtime worker --env-file "$(ENV_PATH)"

gateway-sync:
	cd "$(ROOT)" && uv run --no-sync python -m agent_platform.scripts.runtime gateway-sync --env-file "$(ENV_PATH)"

maintenance:
	cd "$(ROOT)" && uv run --no-sync python -m agent_platform.scripts.runtime maintenance --env-file "$(ENV_PATH)"

web:
	PLATFORM_API_PROXY="http://$(HOST):$(PORT)" npm --prefix "$(ROOT)/agent_platform/apps/web" run dev

build:
	npm --prefix "$(ROOT)/agent_platform/apps/web" run build

test:
	cd "$(ROOT)" && uv run python -m unittest discover -s agent_platform/tests -v

infra: k8s-deploy

gateway-key:
	@echo '组织网关凭据由平台网关面板和 Kubernetes gateway-sync 管理。'

up: k8s-deploy

down: k8s-stop

compose-up:
	$(COMPOSE) up -d --build --wait

compose-down:
	$(COMPOSE) down

api-contract:
	cd "$(ROOT)" && uv run --no-sync python -m agent_platform.scripts.api_contract

api-contract-check:
	cd "$(ROOT)" && uv run --no-sync python -m agent_platform.scripts.api_contract --check
