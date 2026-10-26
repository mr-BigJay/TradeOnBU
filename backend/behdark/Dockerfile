FROM python:3.12-slim-bookworm
WORKDIR /service
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.32.1 httpx==0.28.1 pydantic==2.10.3 && useradd --uid 1000 --create-home behdark && mkdir /behdark-data && chown behdark:behdark /behdark-data
COPY backend/behdark /service/behdark
USER behdark
CMD ["uvicorn", "behdark.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
