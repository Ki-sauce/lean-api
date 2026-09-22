FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y curl git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install elan
RUN curl https://elan.lean-lang.org/elan-init.sh -sSf | \
    sh -s -- -y --default-toolchain none

ENV PATH="/root/.elan/bin:$PATH"

# Actually install Lean during the Docker build
RUN elan toolchain install stable
RUN elan default stable

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
