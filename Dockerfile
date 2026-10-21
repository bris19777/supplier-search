FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
# Chromium and its system libraries, matching the installed Playwright version
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY supsearch ./supsearch
COPY config ./config

# Cloud mode: listen publicly, never without APP_PASSWORD, no visible login windows,
# browser profiles (Cloudflare clearance, dealer sessions) on the /data volume
ENV HOST=0.0.0.0 \
    PORT=8080 \
    OPEN_BROWSER=0 \
    REQUIRE_PASSWORD=1 \
    INTERACTIVE_LOGIN=0 \
    SUPSEARCH_DATA=/data
EXPOSE 8080

CMD ["python", "-m", "supsearch"]
