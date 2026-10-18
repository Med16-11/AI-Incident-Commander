package main

import (
	"fmt"
	"net/http"
	"regexp"
)

var requestCorrelationPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

func correlationMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		correlationID := r.Header.Get("X-Correlation-ID")
		if !requestCorrelationPattern.MatchString(correlationID) {
			generated, err := newUUID()
			if err != nil {
				http.Error(w, "unable to generate correlation ID", http.StatusInternalServerError)
				return
			}
			correlationID = generated
		}
		w.Header().Set("X-Correlation-ID", correlationID)
		r.Header.Set("X-Correlation-ID", correlationID)
		next.ServeHTTP(w, r)
	})
}

func correlationID(r *http.Request) string { return r.Header.Get("X-Correlation-ID") }

type serviceMetrics struct {
	requests     atomicUint64
	published    atomicUint64
	duplicates   atomicUint64
	invalid      atomicUint64
	failed       atomicUint64
	unauthorized atomicUint64
}

func (m *serviceMetrics) serveHTTP(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	fmt.Fprintf(w, "# HELP opsmind_ingestion_requests_total HTTP event-ingestion requests.\n# TYPE opsmind_ingestion_requests_total counter\nopsmind_ingestion_requests_total %d\n", m.requests.Load())
	fmt.Fprintf(w, "# HELP opsmind_ingestion_events_published_total Events appended to Redis Streams.\n# TYPE opsmind_ingestion_events_published_total counter\nopsmind_ingestion_events_published_total %d\n", m.published.Load())
	fmt.Fprintf(w, "# HELP opsmind_ingestion_events_duplicate_total Duplicate event IDs ignored.\n# TYPE opsmind_ingestion_events_duplicate_total counter\nopsmind_ingestion_events_duplicate_total %d\n", m.duplicates.Load())
	fmt.Fprintf(w, "# HELP opsmind_ingestion_events_invalid_total Invalid event envelopes rejected.\n# TYPE opsmind_ingestion_events_invalid_total counter\nopsmind_ingestion_events_invalid_total %d\n", m.invalid.Load())
	fmt.Fprintf(w, "# HELP opsmind_ingestion_publish_failures_total Redis publish failures.\n# TYPE opsmind_ingestion_publish_failures_total counter\nopsmind_ingestion_publish_failures_total %d\n", m.failed.Load())
	fmt.Fprintf(w, "# HELP opsmind_ingestion_unauthorized_total Unauthorized event requests.\n# TYPE opsmind_ingestion_unauthorized_total counter\nopsmind_ingestion_unauthorized_total %d\n", m.unauthorized.Load())
}

func writeRequestError(w http.ResponseWriter, r *http.Request, status int, code string, detail string) {
	body := map[string]string{"error": code, "correlation_id": correlationID(r)}
	if detail != "" {
		body["detail"] = detail
	}
	writeJSON(w, status, body)
}
