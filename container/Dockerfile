FROM docker.io/nvidia/cuda:13.2.1-devel-ubuntu24.04@sha256:0e1f7b8e96fa9ec5e36d4709a38c62df7b5665977446081811c12b8234d874bf AS build
ARG STRATA_REV=257727d58ff85a9aef6f314f9a95f75e4fc330d2
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git python3 python3-pip python3-venv unzip \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/sergqwer/strata-nvfp4.git /opt/strata \
    && git -C /opt/strata checkout --detach "$STRATA_REV"
WORKDIR /opt/strata
RUN python3 -m venv .venv && .venv/bin/pip install --no-cache-dir -r requirements.txt
RUN .venv/bin/python -c 'import setup; print(setup.get_llama_cpp())'
ARG BUILD_JOBS=5
RUN --mount=type=cache,id=strata-vast-engine-257727d5,target=/opt/strata/build .venv/bin/cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_MAKE_PROGRAM=/opt/strata/.venv/bin/ninja -DSTRATA_ENABLE_CUDA=ON \
    -DSTRATA_BUILD_TESTS=OFF -DSTRATA_PORTABLE=ON \
    '-DCMAKE_CUDA_ARCHITECTURES=75;80;86;89;120' -DSTRATA_GGML_DIR=/opt/strata/third_party/llama.cpp \
    && .venv/bin/cmake --build build --target strata -j "$BUILD_JOBS" \
    && mkdir -p engine && cp build/strata engine/strata \
    && if [ -f build/libstrata_kq.so ]; then cp build/libstrata_kq.so engine/; fi
RUN --mount=type=cache,id=strata-vast-vision-257727d5,target=/opt/strata/build-vision .venv/bin/cmake -S tools/vision -B build-vision -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_MAKE_PROGRAM=/opt/strata/.venv/bin/ninja -DSTRATA_PORTABLE=ON \
    -DSTRATA_VISION_CUDA=OFF -DLLAMA_DIR=/opt/strata/third_party/llama.cpp \
    && .venv/bin/cmake --build build-vision --target strata-vision -j "$BUILD_JOBS" \
    && cp build-vision/bin/strata-vision engine/strata-vision
RUN rm -rf build build-vision third_party/llama.cpp .git

FROM docker.io/vastai/base-image:cuda-13.2-mini-py312-2026-10-23@sha256:f9644f212dd2af0777a19902c2d8076fa4b8c863ee09ab99ae3022ffa9aa4a42
LABEL org.opencontainers.image.source="https://github.com/sieciowiecxyz/vast-swiss-knife" \
      org.opencontainers.image.description="Vast SSH and Strata NVFP4, with model on a mounted volume"
COPY --from=build /opt/strata /opt/strata
COPY --from=build /usr/local/cuda-13.2/targets/x86_64-linux/lib/libcublas.so.13.4.0.1 /usr/local/cuda-13.2/targets/x86_64-linux/lib/
COPY --from=build /usr/local/cuda-13.2/targets/x86_64-linux/lib/libcublasLt.so.13.4.0.1 /usr/local/cuda-13.2/targets/x86_64-linux/lib/
RUN ln -s libcublas.so.13.4.0.1 /usr/local/cuda-13.2/targets/x86_64-linux/lib/libcublas.so.13 \
    && ln -s libcublasLt.so.13.4.0.1 /usr/local/cuda-13.2/targets/x86_64-linux/lib/libcublasLt.so.13 \
    && ldconfig
COPY start.py /opt/strata-vast/start.py
COPY strata.conf /etc/supervisor/conf.d/strata.conf
ENV PYTHONUNBUFFERED=1
