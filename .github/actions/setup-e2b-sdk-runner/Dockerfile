FROM m.daocloud.io/docker.io/python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_PROGRESS_BAR=off
ENV PIP_ROOT_USER_ACTION=ignore

RUN python -m pip install \
        --progress-bar off \
        'e2b==2.30.0' \
        'e2b-code-interpreter==2.8.1' \
        grpcio-tools \
        packaging \
        PyYAML \
        requests \
        requests-unixsocket
