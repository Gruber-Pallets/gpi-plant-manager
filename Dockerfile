FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# WeasyPrint system deps (Pango/HarfBuzz; Cairo + GLib pulled transitively)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --upgrade pip && pip install .

CMD ["sh", "-c", "uvicorn zira_dashboard.app:app --host 0.0.0.0 --port ${PORT}"]
