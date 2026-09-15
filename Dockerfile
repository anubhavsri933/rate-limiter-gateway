# Slim base image keeps the final image small — matters for CI speed
# and deployment cost, and is worth mentioning in an interview as a
# deliberate choice over the full python:3.12 image.
FROM python:3.12-slim

WORKDIR /app

# Copy requirements first (before app code) so Docker's layer cache
# can skip the pip install step on rebuilds where only app code
# changed, not dependencies. This is a real speed optimization, not
# just convention.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# The SQLite file lives here; docker-compose mounts a volume at this
# path so data survives container restarts/rebuilds.
ENV GATEWAY_DB_PATH=/data/gateway.db
RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
