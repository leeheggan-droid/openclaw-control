FROM python:3.12-slim

# Install OpenSSH client so the app can open SSH tunnels to the VPS targets.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached as long as requirements don't change).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
# The repo root is the Python path — web_app.py, cheap_chat_feature.py,
# trigger_happy_proposals.py, and the openclaw_control/ package all live here.
COPY . .

# Expose the cockpit port (configurable via PORT env var, default 8001).
EXPOSE 8001

# Run the FastAPI application with uvicorn.
# PORT env var overrides the default port (8001).
CMD ["sh", "-c", "uvicorn web_app:app --host 0.0.0.0 --port ${PORT:-8001}"]
