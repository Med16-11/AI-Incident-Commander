package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

type fakePublisher struct {
	pingErr    error
	publishErr error
	entryID    string
	published  bool
	calls      int
	stream     string
	eventID    string
	eventJSON  []byte
	dedupeTTL  time.Duration
}

func (p *fakePublisher) Ping(context.Context) error { return p.pingErr }

func (p *fakePublisher) PublishOnce(_ context.Context, stream, eventID string, ttl time.Duration, payload []byte) (string, bool, error) {
	p.calls++
	p.stream = stream
	p.eventID = eventID
	p.eventJSON = append([]byte(nil), payload...)
	p.dedupeTTL = ttl
	return p.entryID, p.published, p.publishErr
}

func testService(publisher *fakePublisher) *httpService {
	return newHTTPService(config{
		stream:      "opsmind:events",
		bearerToken: "0123456789abcdef0123456789abcdef",
		dedupeTTL:   24 * time.Hour,
	}, publisher, slog.New(slog.NewJSONHandler(io.Discard, nil)))
}

func validFixture(t *testing.T) []byte {
	t.Helper()
	data, err := os.ReadFile("../../packages/models/contracts/event-envelope-v1.fixture.json")
	if err != nil {
		t.Fatalf("read shared event fixture: %v", err)
	}
	return data
}

func request(service *httpService, body []byte, token string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(http.MethodPost, "/v1/events", strings.NewReader(string(body)))
	request.Header.Set("Content-Type", "application/json")
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response := httptest.NewRecorder()
	service.handler().ServeHTTP(response, request)
	return response
}

func TestSharedPythonEventFixtureIsAccepted(t *testing.T) {
	publisher := &fakePublisher{entryID: "1740000000000-0", published: true}
	response := request(testService(publisher), validFixture(t), "0123456789abcdef0123456789abcdef")
	if response.Code != http.StatusAccepted {
		t.Fatalf("status = %d, body = %s", response.Code, response.Body.String())
	}
	if publisher.calls != 1 || publisher.stream != "opsmind:events" {
		t.Fatalf("publish call mismatch: %#v", publisher)
	}
	var normalized eventEnvelope
	if err := json.Unmarshal(publisher.eventJSON, &normalized); err != nil {
		t.Fatalf("decode Redis payload: %v", err)
	}
	if normalized.EventType != "MetricAnomalyDetected" || *normalized.EventID != "5f7622a1-5d2f-4b6f-b981-64ea003d195a" {
		t.Fatalf("unexpected normalized envelope: %#v", normalized)
	}
	if response.Header().Get("X-Correlation-ID") != "sample-run-01" {
		t.Fatalf("missing propagated event correlation ID: %q", response.Header().Get("X-Correlation-ID"))
	}
}

func TestIngestionRequiresBearerToken(t *testing.T) {
	publisher := &fakePublisher{}
	response := request(testService(publisher), validFixture(t), "wrong-token")
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", response.Code)
	}
	if publisher.calls != 0 {
		t.Fatal("unauthorized request reached Redis publisher")
	}
}

func TestDuplicateEventReturnsExistingStreamEntry(t *testing.T) {
	publisher := &fakePublisher{entryID: "1740000000000-0", published: false}
	response := request(testService(publisher), validFixture(t), "0123456789abcdef0123456789abcdef")
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", response.Code)
	}
	if !strings.Contains(response.Body.String(), `"published":false`) {
		t.Fatalf("response should identify the replay: %s", response.Body.String())
	}
}

func TestInvalidEventEnvelopesAreRejectedBeforePublishing(t *testing.T) {
	fixture := string(validFixture(t))
	cases := map[string]string{
		"invalid event ID":      strings.Replace(fixture, "5f7622a1-5d2f-4b6f-b981-64ea003d195a", "not-a-uuid", 1),
		"unsafe correlation ID": strings.Replace(fixture, "sample-run-01", "bad id", 1),
		"array payload":         strings.Replace(fixture, `"payload": {"service": "payment-service", "score": 91}`, `"payload": ["bad"]`, 1),
		"null timestamp":        strings.Replace(fixture, `"timestamp": "2026-10-16T10:30:00Z"`, `"timestamp": null`, 1),
		"multiple JSON values":  fixture + `{}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			publisher := &fakePublisher{}
			response := request(testService(publisher), []byte(body), "0123456789abcdef0123456789abcdef")
			if response.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400: %s", response.Code, response.Body.String())
			}
			if publisher.calls != 0 {
				t.Fatal("invalid envelope reached Redis publisher")
			}
		})
	}
}

func TestRedisFailureReturnsRetryableServiceUnavailable(t *testing.T) {
	publisher := &fakePublisher{publishErr: errors.New("redis down")}
	response := request(testService(publisher), validFixture(t), "0123456789abcdef0123456789abcdef")
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", response.Code)
	}
}

func TestReadinessDependsOnRedis(t *testing.T) {
	service := testService(&fakePublisher{pingErr: errors.New("redis down")})
	response := httptest.NewRecorder()
	service.handler().ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/health/ready", nil))
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", response.Code)
	}
}

func TestMissingOptionalEnvelopeValuesReceiveContractDefaults(t *testing.T) {
	body := []byte(`{"event_type":"IncidentCreated","correlation_id":"c-1","source":"test","payload":{}}`)
	envelope, normalized, err := normalizeEnvelope(body)
	if err != nil {
		t.Fatalf("normalize event: %v", err)
	}
	if envelope.EventID == nil || envelope.SchemaVersion == nil || envelope.Timestamp == nil {
		t.Fatalf("expected generated defaults: %#v", envelope)
	}
	if _, err := time.Parse(time.RFC3339Nano, *envelope.Timestamp); err != nil {
		t.Fatalf("generated timestamp invalid: %v", err)
	}
	if !json.Valid(normalized) {
		t.Fatalf("normalized JSON invalid: %s", normalized)
	}
}
