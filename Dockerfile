FROM python:3.11-slim

WORKDIR /app

# System deps: duckdb/dbt wheels are self-contained, but a C build toolchain is still needed
# for a couple of transitive packages that ship source-only sdists on some platforms.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 8010

ENTRYPOINT ["./docker-entrypoint.sh"]
