FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости ставятся отдельным слоем: правка кода не должна тянуть
# переустановку всего дерева пакетов при каждой сборке.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY migrations ./migrations
COPY alembic.ini ./
COPY schema ./schema

EXPOSE 8000

CMD ["flowlens", "serve", "--host", "0.0.0.0", "--port", "8000"]
