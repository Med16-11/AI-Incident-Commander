package main

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"
	"time"
)

var correlationPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

type eventEnvelope struct {
	EventID          *string         `json:"event_id"`
	EventType        string          `json:"event_type"`
	Timestamp        *string         `json:"timestamp"`
	OccurredAt       *string         `json:"occurred_at,omitempty"`
	CorrelationID    string          `json:"correlation_id"`
	CausationID      *string         `json:"causation_id"`
	Source           string          `json:"source"`
	SchemaVersion    *int            `json:"schema_version"`
	TenantID         *string         `json:"tenant_id"`
	AggregateType    *string         `json:"aggregate_type"`
	AggregateID      *string         `json:"aggregate_id"`
	AggregateVersion *int            `json:"aggregate_version"`
	Payload          json.RawMessage `json:"payload"`
}

func normalizeEnvelope(body []byte) (eventEnvelope, []byte, error) {
	var envelope eventEnvelope
	decoder := json.NewDecoder(bytes.NewReader(body))
	if err := decoder.Decode(&envelope); err != nil {
		return eventEnvelope{}, nil, fmt.Errorf("decode event: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return eventEnvelope{}, nil, errors.New("request must contain a single JSON object")
		}
		return eventEnvelope{}, nil, fmt.Errorf("decode trailing JSON: %w", err)
	}
	var rawFields map[string]json.RawMessage
	if err := json.Unmarshal(body, &rawFields); err != nil || rawFields == nil {
		return eventEnvelope{}, nil, errors.New("event envelope must be a JSON object")
	}
	for _, field := range []string{"event_id", "timestamp", "occurred_at", "schema_version"} {
		if value, exists := rawFields[field]; exists && bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return eventEnvelope{}, nil, fmt.Errorf("%s cannot be null", field)
		}
	}

	if envelope.EventID == nil {
		generatedID, err := newUUID()
		if err != nil {
			return eventEnvelope{}, nil, fmt.Errorf("generate event_id: %w", err)
		}
		envelope.EventID = &generatedID
	}
	var err error
	if *envelope.EventID, err = normalizeUUID("event_id", *envelope.EventID); err != nil {
		return eventEnvelope{}, nil, err
	}
	if strings.TrimSpace(envelope.EventType) == "" {
		return eventEnvelope{}, nil, errors.New("event_type is required")
	}
	if envelope.SchemaVersion == nil {
		defaultVersion := 1
		envelope.SchemaVersion = &defaultVersion
	}
	if *envelope.SchemaVersion < 1 {
		return eventEnvelope{}, nil, errors.New("schema_version must be at least 1")
	}
	if envelope.Timestamp == nil {
		envelope.Timestamp = envelope.OccurredAt
	}
	if envelope.Timestamp == nil {
		now := time.Now().UTC().Format(time.RFC3339Nano)
		envelope.Timestamp = &now
	} else if strings.TrimSpace(*envelope.Timestamp) != *envelope.Timestamp {
		return eventEnvelope{}, nil, errors.New("timestamp must not contain surrounding whitespace")
	}
	if _, err := time.Parse(time.RFC3339Nano, *envelope.Timestamp); err != nil {
		return eventEnvelope{}, nil, errors.New("timestamp must be an RFC3339 date-time")
	}
	envelope.OccurredAt = nil
	if !correlationPattern.MatchString(envelope.CorrelationID) {
		return eventEnvelope{}, nil, errors.New("correlation_id must match [A-Za-z0-9._-]{1,128}")
	}
	if envelope.Source == "" || len([]rune(envelope.Source)) > 160 {
		return eventEnvelope{}, nil, errors.New("source must contain between 1 and 160 characters")
	}
	if len(envelope.Payload) == 0 || bytes.Equal(bytes.TrimSpace(envelope.Payload), []byte("null")) {
		return eventEnvelope{}, nil, errors.New("payload must be a JSON object")
	}
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(envelope.Payload, &payload); err != nil || payload == nil {
		return eventEnvelope{}, nil, errors.New("payload must be a JSON object")
	}
	if err := validateOptionalUUID("causation_id", envelope.CausationID); err != nil {
		return eventEnvelope{}, nil, err
	}
	if err := validateOptionalUUID("tenant_id", envelope.TenantID); err != nil {
		return eventEnvelope{}, nil, err
	}
	if err := validateOptionalUUID("aggregate_id", envelope.AggregateID); err != nil {
		return eventEnvelope{}, nil, err
	}
	if envelope.AggregateVersion != nil && *envelope.AggregateVersion < 1 {
		return eventEnvelope{}, nil, errors.New("aggregate_version must be at least 1")
	}
	serialized, err := json.Marshal(envelope)
	if err != nil {
		return eventEnvelope{}, nil, fmt.Errorf("serialize normalized event: %w", err)
	}
	return eventEnvelope{EventID: envelope.EventID, EventType: envelope.EventType, Timestamp: envelope.Timestamp, CorrelationID: envelope.CorrelationID, CausationID: envelope.CausationID, Source: envelope.Source, SchemaVersion: envelope.SchemaVersion, TenantID: envelope.TenantID, AggregateType: envelope.AggregateType, AggregateID: envelope.AggregateID, AggregateVersion: envelope.AggregateVersion, Payload: envelope.Payload}, serialized, nil
}

func validateOptionalUUID(name string, value *string) error {
	if value == nil {
		return nil
	}
	normalized, err := normalizeUUID(name, *value)
	if err != nil {
		return err
	}
	*value = normalized
	return nil
}

func normalizeUUID(name, value string) (string, error) {
	value = strings.ToLower(value)
	if len(value) != 36 || value[8] != '-' || value[13] != '-' || value[18] != '-' || value[23] != '-' {
		return "", fmt.Errorf("%s must be a UUID", name)
	}
	for i, char := range value {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			continue
		}
		if !((char >= '0' && char <= '9') || (char >= 'a' && char <= 'f')) {
			return "", fmt.Errorf("%s must be a UUID", name)
		}
	}
	return value, nil
}

func newUUID() (string, error) {
	var data [16]byte
	if _, err := rand.Read(data[:]); err != nil {
		return "", err
	}
	data[6] = (data[6] & 0x0f) | 0x40
	data[8] = (data[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", data[0:4], data[4:6], data[6:8], data[8:10], data[10:16]), nil
}
