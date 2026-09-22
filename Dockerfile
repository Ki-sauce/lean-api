FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y \
        curl \
        git \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install elan
RUN curl https://elan.lean-lang.org/elan-init.sh -sSf | \
    sh -s -- -y --default-toolchain none

ENV PATH="/root/.elan/bin:$PATH"

# Install Lean
RUN elan toolchain install stable
RUN elan default stable

# Create Lean + Mathlib project
RUN lake init leanverify math

WORKDIR /app/leanverify

# Fetch Mathlib and its precompiled artifacts
RUN lake update
RUN lake exe cache get

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]