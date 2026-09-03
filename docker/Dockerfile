# openai-agents 网关镜像
# 说明：基础镜像里安装的是「已发布的 openai-agents 稳定版」，不直接 COPY 本仓库 main 源码，
#      以保证行为稳定、可维护、避免带入未发布改动。
#
# 构建参数（参照 lp 的 fquiz 模式，便于国内/海外 CI 通用）：
#   PIP_INDEX_URL       pip 源（默认官方 pypi；国内构建可传国内镜像源）
#   PIP_DEFAULT_TIMEOUT pip 超时（默认 120s）
#   PIP_RETRIES         pip 重试次数
ARG PYTHON_BASE_IMAGE=python:3.11-slim
FROM ${PYTHON_BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=20
# apt 源镜像（适配国内构建）；默认空 = 官方源（海外/默认 runner）。用法与 lp 一致。
ARG APT_MIRROR=""

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT}
ENV PIP_RETRIES=${PIP_RETRIES}

WORKDIR /srv/gateway

# skill scripts 运行时依赖：python3(基础镜像自带) + docker CLI + curl
# （skill 的 scripts 用纯 Python + docker/curl 操作 ACR/API，不依赖 hermes runtime）
# 注意：Debian trixie 的 docker.io 包只装 dockerd/docker-init/docker-proxy，不含 docker 客户端，
#       故直接拉取 Docker 官方静态 CLI 二进制（仅客户端，无需 daemon）。
ARG DOCKER_CLI_VERSION=27.5.1
RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list 2>/dev/null; \
    fi; \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz

# 先装依赖（利用缓存层）
COPY gateway/requirements.txt requirements.txt
RUN pip install --no-cache-dir \
        --retries "${PIP_RETRIES}" \
        --timeout "${PIP_DEFAULT_TIMEOUT}" \
        -i "${PIP_INDEX_URL}" \
        -r requirements.txt

# 拷贝网关服务代码（不包含仓库 src/，只装发布版 agents 包）
COPY gateway/ ./gateway/

EXPOSE 8080

# 运行时通过环境变量注入模型端点等（见 config.py）
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]