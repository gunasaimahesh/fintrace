# FinTrace: the API and the built UI in one image. FalkorDB runs beside it (docker-compose.yml).

FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend/ backend/
COPY pytest.ini ./
COPY --from=ui /ui/dist frontend/dist
EXPOSE 8000
# Seeds both tenants on first start (about two minutes), then serves the API and UI on $PORT (8000 by default).
CMD ["sh", "-c", "python -m backend.app.seed_data --if-missing && python -m backend.app.seed_data --tenant globex-eu --profile small --if-missing && exec uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
