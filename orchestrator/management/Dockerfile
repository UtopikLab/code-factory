FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY ui/package*.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    groupadd --gid 1001 management && \
    useradd --uid 1001 --gid 1001 --no-create-home management
COPY app.py ./
COPY --from=frontend /build/dist /app/ui/dist
USER 1001:1001
EXPOSE 8081
CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8081", "--no-access-log", "--no-proxy-headers"]