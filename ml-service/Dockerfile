# Контекст сборки — корень репозитория: нужны ml/models и код признаков моделей.
FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.19
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 MODEL_DIR=/app/ml/models PYTHONPATH=/app
WORKDIR /app/ml-service
COPY ml-service/pyproject.toml ml-service/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY ml-service/app ./app
COPY ml/models /app/ml/models
COPY ml/baseline /app/ml/baseline
COPY ml/external /app/ml/external
# v3 ML-разработчика: их код признаков как есть и адаптер к сервису (docs/architecture/ml-service.md §3)
COPY ml/v3/src /app/ml/v3/src
COPY ml/v3_bridge /app/ml/v3_bridge
# Ежедневный прогноз по типам (15.4): адаптер и код входов и признаков отдельных моделей ML-разработчика — только модули
COPY ml/daily_routing /app/ml/daily_routing
# модель доступа (lct-8f): только код признаков — dataset и train зависят от backend и архива
COPY ml/access/__init__.py ml/access/features.py /app/ml/access/
COPY ml/each_channel/__init__.py ml/each_channel/features.py ml/each_channel/prepare.py ml/each_channel/train.py \
     ml/each_channel/advanced.py ml/each_channel/calendar_study.py ml/each_channel/hard_channel_study.py \
     ml/each_channel/six_channel_study.py /app/ml/each_channel/
COPY ml/remaining_channels/__init__.py ml/remaining_channels/run.py /app/ml/remaining_channels/
COPY ml/v3_inputs/__init__.py ml/v3_inputs/ingest.py ml/v3_inputs/prepare_episodes.py /app/ml/v3_inputs/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
