# Частые команды. Подробности — DEPLOY.md.
-include infra/.env
API_PORT ?= 18000
WEB_DEV_PORT ?= 15173
COMPOSE = docker compose -f infra/docker-compose.yml
PY_SERVICES = backend ml-service emulator

.PHONY: env up down ps logs health test lint fmt web-dev api-types api-types-check emulator-slices emulator-summaries external bootstrap e2e

env:  ## создать infra/.env из шаблона, если его нет
	@test -f infra/.env || cp infra/.env.example infra/.env

up: env  ## собрать и поднять стенд
	$(COMPOSE) up -d --build

down:  ## остановить стенд (данные в томах сохраняются)
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=100

bootstrap:  ## установка с нуля: env → up → build-archive → load-db → emulator-slices → health (DEPLOY.md)
	@bash scripts/bootstrap.sh

health:  ## состояние всех частей через API
	@curl -s http://localhost:$(API_PORT)/api/v1/health | python3 -m json.tool

test:  ## тесты всех сервисов
	@for s in $(PY_SERVICES); do echo "== $$s"; (cd $$s && uv run pytest -q) || exit 1; done
	@$(MAKE) --no-print-directory api-types-check
	cd web && npm run build

lint:
	@for s in $(PY_SERVICES); do echo "== $$s"; (cd $$s && uv run ruff check . && uv run ruff format --check .) || exit 1; done
	cd web && npm run lint

fmt:
	@for s in $(PY_SERVICES); do (cd $$s && uv run ruff check --fix . && uv run ruff format .); done

web-dev:  ## фронтенд с горячей перезагрузкой на WEB_DEV_PORT, API — со стенда этого worktree
	cd web && VITE_API_PROXY=http://localhost:$(API_PORT) npm run dev -- --port $(WEB_DEV_PORT)

external:  ## внешний набор D=<ключ>: скачать с проверкой sha256 и подготовить data/external/<ключ>/prepared/
	@test -n "$(D)" || (echo "укажите набор: make external D=magdeburg" && exit 1)
	cd backend && uv run python -m app.cli external download --dataset $(D) && \
	uv run python -m app.cli external prepare --dataset $(D)

emulator-slices:  ## срезы сценариев эмулятора из data/raw в data/processed/emulator; один сценарий — S=<имя>
	cd emulator && uv run python -m app.prepare $(S)

emulator-summaries:  ## сводки готовых срезов на диск, без пересборки срезов: эмулятор берёт их, а не считает при просмотре
	cd emulator && uv run python -m app.prepare --summaries-only $(S)

# Типы API для интерфейса (D15, 8.7). OpenAPI — из кода backend (app.openapi()): стенд не нужен, типы совпадают
# с кодом ветки. С работающего стенда: make api-types API_TYPES_URL=http://localhost:$(API_PORT)/api/v1/openapi.json
# openapi-typescript 7 требует TypeScript 5, в web — TypeScript 6, поэтому генератор запускается через npx
# в своём окружении и в зависимости web не попадает.
API_TYPES_GEN = npx --yes -p openapi-typescript@7.13.0 -p typescript@5.9 openapi-typescript
OPENAPI_FROM_CODE = cd backend && uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), ensure_ascii=False, indent=1))"

api-types:  ## типы API из OpenAPI → web/src/lib/api/schema.d.ts
	@if [ -n "$(API_TYPES_URL)" ]; then curl -sf "$(API_TYPES_URL)" -o web/openapi.json; \
	else $(OPENAPI_FROM_CODE) > ../web/openapi.json; fi
	cd web && $(API_TYPES_GEN) openapi.json -o src/lib/api/schema.d.ts; \
	status=$$?; rm -f openapi.json; exit $$status

# Дрейф контракта: закоммиченный schema.d.ts должен совпадать с генерацией из кода backend. Иначе ручные типы
# интерфейса сверяются (lib/api/contract.ts) со старой схемой и расхождение проходит незамеченным.
api-types-check:  ## schema.d.ts совпадает с кодом backend (входит в make test)
	@tmp=$$(mktemp -d); \
	($(OPENAPI_FROM_CODE)) > $$tmp/openapi.json && \
	(cd web && $(API_TYPES_GEN) $$tmp/openapi.json -o $$tmp/schema.d.ts >/dev/null 2>&1) && \
	if cmp -s $$tmp/schema.d.ts web/src/lib/api/schema.d.ts; then echo "== api-types: schema.d.ts актуален"; rm -rf $$tmp; \
	else echo "schema.d.ts устарел: схемы backend изменились — выполните make api-types и поправьте web/src/lib/api/types.ts"; rm -rf $$tmp; exit 1; fi

e2e:  ## сквозная проверка сценария: SCENARIO=<имя> или UPLOAD=<журнал CSV/XLSX> [ARGS=--interrupt] [E2E_ENV=<infra/.env другого стенда>] (11.1, 13.1)
	cd backend && uv run python -u ../scripts/e2e_scenario.py $(SCENARIO) $(ARGS) $(if $(UPLOAD),--upload "$(abspath $(UPLOAD))") $(if $(E2E_ENV),--env $(abspath $(E2E_ENV)))
