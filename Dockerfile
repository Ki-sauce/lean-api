FROM python:3.12-slim

WORKDIR /app

# Tools needed to install Lean
RUN apt-get update && \
    apt-get install -y curl git && \
    rm -rf /var/lib/apt/lists/*

# Install elan + Lean 4
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    -sSf | sh -s -- -y --default-toolchain stable

ENV PATH="/root/.elan/bin:$PATH"

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application
COPY main.py .

# Render supplies PORT; 8000 is useful locally
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]