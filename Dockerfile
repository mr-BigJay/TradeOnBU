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
RUN python -m pip install --no-cache-dir --only-binary=pandas,numpy -r backend/requirements.txt \
 && python -c 'import pandas, pandas._libs.tslib, numpy, mplfinance; print("NATIVE_IMPORT_OK", pandas.__version__)'
COPY backend/ ./backend/
COPY --from=frontend /fe/dist ./frontend/dist
ENV PYTHONPATH=/app/backend
WORKDIR /app/backend
RUN mkdir -p /app/data
RUN DISABLE_APP_BACKGROUND=1 VERIFIED_PLANS_ENABLED=1 python -c 'from app.main import app; print("APP_IMPORT_OK")'
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
