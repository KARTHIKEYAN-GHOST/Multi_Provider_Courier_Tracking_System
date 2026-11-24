# Engine image. Build from the repository root:
#   docker build -f courier-engine/Dockerfile .
# (the Engine embeds courier-contracts/topology.yaml at compile time)

FROM rust:1-slim AS build
WORKDIR /src
COPY courier-contracts/topology.yaml courier-contracts/topology.yaml
COPY courier-engine courier-engine
WORKDIR /src/courier-engine
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/courier-engine/target \
    cargo build --release --locked \
    && cp target/release/courier-engine /usr/local/bin/courier-engine

FROM debian:stable-slim
RUN useradd --system --uid 10001 --no-create-home engine
COPY --from=build /usr/local/bin/courier-engine /usr/local/bin/courier-engine
USER engine
EXPOSE 8080
# No secrets are baked in: DATABASE_URL and ENGINE_AMQP_URL come from the environment.
ENTRYPOINT ["courier-engine"]
CMD ["serve"]
