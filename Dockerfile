# Builds the React UI, then serves UI + API from one FastAPI process on :8000
#   docker build -t tradeshield .
#   docker run -p 8000:8000 --env-file .env -v tradeshield-data:/data tradeshield
FROM node:20-alpine AS ui
WORKDIR /ui
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=ui /ui/dist /app/frontend/dist
ENV TS_DB_PATH=/data/tradeshield.db TS_MOCK_STATE=/data/mock_exchange_state.json \
    O21_MAP_PATH=/data/o21_order_map.json O21_INSTRUMENTS_CACHE=/data/o21_instruments.csv
VOLUME /data
EXPOSE 8000
# ONE worker only: the 021 API allows one live token per account, a second process would log the first one out.
CMD ["uvicorn", "tradeshield.app:app", "--host", "0.0.0.0", "--port", "8000"]
