# MinIO Client (mc): same discontinued distribution as the minio server image, see
# .github/mirrors/minio/Dockerfile and truealpha#1041. Builds the exact pinned release
# from mc's still-public AGPLv3 GitHub source.
ARG MC_RELEASE_TAG

FROM golang:1.24-alpine AS build
ARG MC_RELEASE_TAG
RUN apk add -U --no-cache git
WORKDIR /src
RUN git clone --depth 1 --branch "${MC_RELEASE_TAG}" https://github.com/minio/mc.git .
ENV CGO_ENABLED=0
RUN LDFLAGS="$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go)" && \
    go build -trimpath --ldflags "${LDFLAGS}" -o /out/mc .

FROM alpine:3.20
RUN apk add -U --no-cache ca-certificates
COPY --from=build /src/LICENSE /licenses/LICENSE
COPY --from=build /out/mc /usr/bin/mc
ENTRYPOINT ["/usr/bin/mc"]
