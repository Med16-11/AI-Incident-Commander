FROM golang:1.24-alpine AS build

WORKDIR /src
COPY apps/event-ingestion/go.mod apps/event-ingestion/go.sum ./
RUN go mod download
COPY apps/event-ingestion/*.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/event-ingestion .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build --chown=nonroot:nonroot /out/event-ingestion /event-ingestion
EXPOSE 8000
USER nonroot:nonroot
ENTRYPOINT ["/event-ingestion"]
