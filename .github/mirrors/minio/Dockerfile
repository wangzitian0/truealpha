# MinIO archived open-source community distribution (quay.io, Docker Hub, Bitnami, and
# MinIO's own dl.min.io binary server all return unauthorized/410/"object not found" as of
# 2026-09-24 -- MinIO Inc. discontinued free container/binary distribution, confirmed via
# https://dl.min.io/server/minio/release/linux-amd64/minio returning "410 Gone: The
# open-source MinIO Server ... projects are archived and no longer maintained." The AGPLv3
# source remains on GitHub; this builds the exact pinned release from that source instead
# of depending on a registry MinIO no longer serves. See truealpha#1041.
ARG MINIO_RELEASE_TAG

FROM golang:1.24-alpine AS build
ARG MINIO_RELEASE_TAG
RUN apk add -U --no-cache git
WORKDIR /src
RUN git clone --depth 1 --branch "${MINIO_RELEASE_TAG}" https://github.com/minio/minio.git .
ENV CGO_ENABLED=0
RUN LDFLAGS="$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go)" && \
    go build -trimpath --ldflags "${LDFLAGS}" -o /out/minio .

FROM alpine:3.20
# docker-compose.yml's healthcheck runs `curl` inside this container.
RUN apk add -U --no-cache curl
COPY --from=build /src/LICENSE /licenses/LICENSE
COPY --from=build /src/CREDITS /licenses/CREDITS
COPY --from=build /out/minio /usr/bin/minio
EXPOSE 9000
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/minio"]
