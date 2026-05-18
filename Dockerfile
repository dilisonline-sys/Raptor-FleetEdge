FROM python:3.13-slim

WORKDIR /app

# System deps: gcc needed for pandas-ta C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Logs land in /tmp inside the container — mount a volume to persist them
VOLUME ["/tmp"]

# Manager port + all agent ports
EXPOSE 7430 7432 7433 7434

CMD ["python", "agent_manager.py"]
