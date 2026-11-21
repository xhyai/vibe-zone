# syntax=docker/dockerfile:1
# Vibe Zone — Rust implementation.
# Build context is this directory (starter/), as in the participant guide:
#   docker buildx build --platform linux/amd64 --load -t vibe-zone:local starter
#
# Static musl binary in a scratch image: no libc, no shell, no package
# manager — the whole image is one ~3 MB binary.

FROM rust:1.93-alpine AS build
WORKDIR /build
# rust:alpine targets x86_64-unknown-linux-musl natively; gcc is musl-gcc.
ENV CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=gcc \
    CC_x86_64_unknown_linux_musl=gcc
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked --target x86_64-unknown-linux-musl \
    && cp target/x86_64-unknown-linux-musl/release/vibe-zone /vibe-zone

FROM scratch
COPY --from=build /vibe-zone /vibe-zone
ENV PORT=8080 DATA_DIR=/data
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["/vibe-zone"]
