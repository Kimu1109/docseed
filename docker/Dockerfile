# docseed（api / worker 共通イメージ）。docs/design/deployment.md 2.1
#   base : api と worker。推論は ONNX Runtime（torch なし）
#   tools: モデルの取得と ONNX への変換（docseed fetch-models）。torch を含むので初回だけ使う
FROM python:3.12-slim-bookworm AS base

# pip のキャッシュは BuildKit のキャッシュマウント（/root/.cache/pip）に置く。イメージの層には残らない
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin docseed

WORKDIR /app
# 依存だけを先に入れる。この層は pyproject.toml が変わったときだけ作り直す（src・docs や README の変更では取り直さない）
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
      > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt
# 本体（依存は上で入れたので --no-deps）
COPY README.md ./
COPY src ./src
# 画面のヘルプで読む文書（wheel の docseed/_docs に入る。docseed/helpdocs.py）
COPY docs ./docs
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --no-deps .

USER docseed
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
ENTRYPOINT ["tini", "--"]
CMD ["docseed", "serve", "--port", "8000"]


# 音声・動画の文字起こし（任意）。whisper.cpp の whisper-cli を静的に作る（builder）。
# 使うときは .env の DOCSEED_IMAGE_TARGET=asr にして build する（既定の base には入れない）
FROM python:3.12-slim-bookworm AS asr-build
ARG WHISPER_CPP_REF=v1.9.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${WHISPER_CPP_REF}" https://github.com/ggml-org/whisper.cpp /src \
 && cmake -S /src -B /src/build -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF \
      -DCMAKE_BUILD_TYPE=Release \
 && cmake --build /src/build --target whisper-cli -j 4

# 文字起こしの実行環境（ffmpeg で音声を取り出し、whisper-cli で読む）
FROM base AS asr
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=asr-build /src/build/bin/whisper-cli /usr/local/bin/whisper-cli
USER docseed


FROM base AS tools
USER root
# torch は CPU 版を入れる（x86 の PyPI 版は CUDA 込みで巨大なため。arm64 はどちらも CPU 版）
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install ".[tools]"
USER docseed
HEALTHCHECK NONE
CMD ["docseed", "fetch-models"]


# JavaScript で描くページの描画（任意）。Chromium を含む Playwright の公式イメージを使う。
# compose の webclip-browser（profile webclip-render）で動かす。docseed 本体とは別のコンテナ（docs/design/extensions.md 6.10）
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble AS render
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install "httpx>=0.27"
COPY src ./src
USER pwuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "-m", "docseed.webclip.browser"]
