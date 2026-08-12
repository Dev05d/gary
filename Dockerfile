# Optional — see docker-compose.yml. `./start.sh` is the primary path on macOS.
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

RUN adduser --disabled-password --gecos "" gary

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY alembic.ini ./
COPY --from=frontend /build/dist ./frontend/dist

RUN mkdir -p /app/data && chown -R gary:gary /app
USER gary

EXPOSE 8000

# APP_HOST is set to 0.0.0.0 by compose; the loopback port binding is the
# actual network boundary. Set API_AUTH_TOKEN if you publish the port.
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
