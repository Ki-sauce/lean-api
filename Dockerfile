FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y \
        curl \
        git \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install elan without selecting an arbitrary Lean version
RUN curl https://elan.lean-lang.org/elan-init.sh -sSf | \
    sh -s -- -y --default-toolchain none

ENV PATH="/root/.elan/bin:$PATH"

# Create a project whose Mathlib dependency determines the
# required Lean toolchain.
RUN lake +leanprover-community/mathlib4:lean-toolchain new leanverify math

WORKDIR /app/leanverify

# Install exactly the Lean toolchain required by this Mathlib version.
RUN elan toolchain install $(cat lean-toolchain)
RUN elan default $(cat lean-toolchain)

# Download precompiled Mathlib artifacts.
RUN lake exe cache get

# Fail the image build immediately if the Mathlib artifact is missing.
RUN test -f .lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]