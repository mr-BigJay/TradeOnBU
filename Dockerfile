FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends curl fontconfig && rm -rf /var/lib/apt/lists/*
ENV MPLCONFIGDIR=/tmp/mpl
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ ./backend/
COPY --from=frontend /fe/dist ./frontend/dist
ENV PYTHONPATH=/app/backend
WORKDIR /app/backend
RUN mkdir -p /app/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
