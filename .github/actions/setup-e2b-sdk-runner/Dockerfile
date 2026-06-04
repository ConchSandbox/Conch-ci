FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1

RUN python -m pip install --upgrade pip \
    && python -m pip install \
        'e2b-code-interpreter==2.5.0' \
        grpcio-tools \
        packaging
