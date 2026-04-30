FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .

# Install Python deps + Playwright Chromium (--with-deps apt-installs the
# system libs Chromium needs: nss, atk, libdrm, etc.). This replaces the
# previous WeasyPrint setup so the Slack-PDF render uses the same engine
# as a real browser print preview.
RUN pip install --upgrade pip \
    && pip install . \
    && playwright install --with-deps chromium \
    && rm -rf /root/.cache/pip /var/lib/apt/lists/*

CMD ["sh", "-c", "uvicorn zira_dashboard.app:app --host 0.0.0.0 --port ${PORT}"]
