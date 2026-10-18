package main

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "healthcheck" {
		os.Exit(runHealthcheck())
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	config, err := loadConfig()
	if err != nil {
		logger.Error("configuration invalid", "error", err.Error())
		os.Exit(2)
	}

	redisOptions, err := redis.ParseURL(config.redisURL)
	if err != nil {
		logger.Error("redis URL invalid", "error", err.Error())
		os.Exit(2)
	}
	redisClient := redis.NewClient(redisOptions)
	defer func() { _ = redisClient.Close() }()

	publisher := &redisPublisher{client: redisClient}
	service := newHTTPService(config, publisher, logger)
	server := &http.Server{
		Addr:              config.listenAddress,
		Handler:           service.handler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			logger.Error("http server shutdown failed", "error", err.Error())
		}
	}()

	logger.Info("event ingestion service starting", "address", config.listenAddress, "stream", config.stream)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Error("http server stopped unexpectedly", "error", err.Error())
		os.Exit(1)
	}
}

func runHealthcheck() int {
	url := envOr("OPSMIND_EVENT_INGESTION_HEALTH_URL", "http://127.0.0.1:8000/health/ready")
	client := http.Client{Timeout: 2 * time.Second}
	response, err := client.Get(url)
	if err != nil {
		return 1
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

type config struct {
	listenAddress string
	redisURL      string
	stream        string
	bearerToken   string
	dedupeTTL     time.Duration
}

func loadConfig() (config, error) {
	result := config{
		listenAddress: envOr("OPSMIND_EVENT_INGESTION_LISTEN_ADDRESS", ":8000"),
		redisURL:      envOr("OPSMIND_REDIS_URL", "redis://redis:6379/0"),
		stream:        envOr("OPSMIND_EVENT_STREAM", "opsmind:events"),
		bearerToken:   os.Getenv("OPSMIND_EVENT_INGESTION_TOKEN"),
		dedupeTTL:     24 * time.Hour,
	}
	if result.bearerToken == "" {
		return config{}, errors.New("OPSMIND_EVENT_INGESTION_TOKEN must be configured")
	}
	if len(result.bearerToken) < 24 {
		return config{}, errors.New("OPSMIND_EVENT_INGESTION_TOKEN must contain at least 24 characters")
	}
	if ttl := os.Getenv("OPSMIND_EVENT_INGESTION_DEDUPE_TTL"); ttl != "" {
		parsed, err := time.ParseDuration(ttl)
		if err != nil || parsed < time.Minute {
			return config{}, errors.New("OPSMIND_EVENT_INGESTION_DEDUPE_TTL must be a duration of at least 1m")
		}
		result.dedupeTTL = parsed
	}
	if strings.TrimSpace(result.stream) == "" {
		return config{}, errors.New("OPSMIND_EVENT_STREAM cannot be empty")
	}
	return result, nil
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

type redisStreamPublisher interface {
	Ping(context.Context) error
	PublishOnce(context.Context, string, string, time.Duration, []byte) (string, bool, error)
}

type redisPublisher struct{ client redis.UniversalClient }

const publishOnceScript = `
local existing = redis.call('GET', KEYS[1])
if existing then
  return {existing, 0}
end
local entry_id = redis.call('XADD', KEYS[2], '*', 'event', ARGV[1])
redis.call('SET', KEYS[1], entry_id, 'PX', ARGV[2])
return {entry_id, 1}
`

func (p *redisPublisher) Ping(ctx context.Context) error {
	return p.client.Ping(ctx).Err()
}

func (p *redisPublisher) PublishOnce(ctx context.Context, stream, eventID string, ttl time.Duration, envelope []byte) (string, bool, error) {
	dedupeKey := "opsmind:event-ingestion:idempotency:" + eventID
	result, err := redis.NewScript(publishOnceScript).Run(
		ctx,
		p.client,
		[]string{dedupeKey, stream},
		string(envelope),
		ttl.Milliseconds(),
	).Result()
	if err != nil {
		return "", false, err
	}
	parts, ok := result.([]interface{})
	if !ok || len(parts) != 2 {
		return "", false, fmt.Errorf("unexpected Redis script response %T", result)
	}
	entryID, ok := parts[0].(string)
	if !ok {
		return "", false, fmt.Errorf("unexpected Redis entry ID response %T", parts[0])
	}
	wasPublished, ok := parts[1].(int64)
	if !ok {
		return "", false, fmt.Errorf("unexpected Redis publish flag %T", parts[1])
	}
	return entryID, wasPublished == 1, nil
}

type httpService struct {
	config    config
	publisher redisStreamPublisher
	logger    *slog.Logger
	metrics   *serviceMetrics
}

func newHTTPService(cfg config, publisher redisStreamPublisher, logger *slog.Logger) *httpService {
	return &httpService{config: cfg, publisher: publisher, logger: logger, metrics: &serviceMetrics{}}
}

func (s *httpService) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health/live", s.live)
	mux.HandleFunc("GET /health/ready", s.ready)
	mux.HandleFunc("GET /metrics", s.metrics.serveHTTP)
	mux.HandleFunc("POST /v1/events", s.ingest)
	return correlationMiddleware(mux)
}

func (s *httpService) live(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *httpService) ready(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.publisher.Ping(ctx); err != nil {
		s.logger.Warn("readiness check failed", "error", err.Error(), "correlation_id", correlationID(r))
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "not_ready", "reason": "redis_unavailable", "correlation_id": correlationID(r)})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready", "correlation_id": correlationID(r)})
}

func (s *httpService) ingest(w http.ResponseWriter, r *http.Request) {
	s.metrics.requests.Add(1)
	if !authorized(r, s.config.bearerToken) {
		s.metrics.unauthorized.Add(1)
		writeRequestError(w, r, http.StatusUnauthorized, "unauthorized", "")
		return
	}
	mediaType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || mediaType != "application/json" {
		writeRequestError(w, r, http.StatusUnsupportedMediaType, "content_type_must_be_application_json", "")
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBytes)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			writeRequestError(w, r, http.StatusRequestEntityTooLarge, "event_too_large", "")
		} else {
			writeRequestError(w, r, http.StatusBadRequest, "request_body_unreadable", "")
		}
		return
	}

	envelope, eventJSON, err := normalizeEnvelope(body)
	if err != nil {
		s.metrics.invalid.Add(1)
		writeRequestError(w, r, http.StatusBadRequest, "invalid_event_envelope", err.Error())
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	entryID, published, err := s.publisher.PublishOnce(ctx, s.config.stream, *envelope.EventID, s.config.dedupeTTL, eventJSON)
	if err != nil {
		s.metrics.failed.Add(1)
		s.logger.Error("event publish failed", "event_id", *envelope.EventID, "correlation_id", envelope.CorrelationID, "error", err.Error())
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "event_stream_unavailable", "correlation_id": envelope.CorrelationID})
		return
	}
	if published {
		s.metrics.published.Add(1)
	} else {
		s.metrics.duplicates.Add(1)
	}
	status := http.StatusAccepted
	if !published {
		status = http.StatusOK
	}
	w.Header().Set("X-Correlation-ID", envelope.CorrelationID)
	writeJSON(w, status, map[string]any{
		"event_id":        *envelope.EventID,
		"stream_entry_id": entryID,
		"published":       published,
		"correlation_id":  envelope.CorrelationID,
		"schema_version":  *envelope.SchemaVersion,
	})
	if published {
		s.logger.Info("event accepted", "event_id", *envelope.EventID, "event_type", envelope.EventType, "source", envelope.Source, "stream_entry_id", entryID, "correlation_id", envelope.CorrelationID)
	} else {
		s.logger.Info("duplicate event ignored", "event_id", *envelope.EventID, "stream_entry_id", entryID, "correlation_id", envelope.CorrelationID)
	}
}

func authorized(r *http.Request, token string) bool {
	value := r.Header.Get("Authorization")
	if !strings.HasPrefix(value, "Bearer ") {
		return false
	}
	provided := strings.TrimPrefix(value, "Bearer ")
	providedHash := sha256.Sum256([]byte(provided))
	expectedHash := sha256.Sum256([]byte(token))
	return subtle.ConstantTimeCompare(providedHash[:], expectedHash[:]) == 1
}

const maxRequestBytes = 1 << 20

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
