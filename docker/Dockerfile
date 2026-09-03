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

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT}
ENV PIP_RETRIES=${PIP_RETRIES}

WORKDIR /srv/gateway

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