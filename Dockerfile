##
# Dockerfile for the Gamma Scalper project
#
# This image installs the required system packages and Python
# dependencies to run the gamma scalping strategy.  It is based on
# Debian slim and installs only what is necessary to keep the image
# small.  The core application is run via `python main.py`.

FROM python:3.11-slim as base

# Install system dependencies needed to build and run the Python
# packages.  In particular, QuantLib requires a C++ compiler and SWIG
# for its bindings.  If a prebuilt wheel is available for your
# platform these packages may not be strictly necessary, but
# installing them ensures the build can succeed on more systems.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        swig \
        libboost-all-dev \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Copy the Python project into the image.  Using two separate COPY
# commands allows Docker to cache the dependencies layer when only
# source code changes.  If you have a requirements.txt or a
# pyproject.toml/poetry.lock, copy those first.
COPY pyproject.toml ./
COPY . .

# Upgrade pip and install project dependencies.  We pin the
# installation to avoid using cached wheels and ensure we get the
# latest compatible versions.  The dependencies here mirror those in
# pyproject.toml; specifying them explicitly avoids having to run
# `pip install .` which would require a full build of the package.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        alpaca-py \
        python-dotenv \
        QuantLib-Python \
        scipy \
        certifi

# Expose no ports; this application runs as a CLI and communicates via
# outbound API calls only.  If you later add a webserver or API
# endpoint, you can specify the appropriate EXPOSE directive here.

# Default command to run the strategy.  You can override this at
# runtime by specifying an alternate command to `docker run`.
CMD ["python", "main.py"]

