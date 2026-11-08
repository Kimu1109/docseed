# docseed（api / worker 共通イメージ）。docs/design/deployment.md 2.1
#   base : api と worker。推論は ONNX Runtime（torch なし）
#   tools: モデルの取得と ONNX への変換（docseed fetch-models）。torch を含むので初回だけ使う
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin docseed

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

USER docseed
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
ENTRYPOINT ["tini", "--"]
CMD ["docseed", "serve", "--port", "8000"]


FROM base AS tools
USER root
# torch は CPU 版を入れる（x86 の PyPI 版は CUDA 込みで巨大なため。arm64 はどちらも CPU 版）
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install ".[tools]"
USER docseed
HEALTHCHECK NONE
CMD ["docseed", "fetch-models"]
