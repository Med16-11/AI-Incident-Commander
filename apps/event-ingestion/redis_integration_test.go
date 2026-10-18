package main

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestRedisPublishOnceIsAtomicAndIdempotent(t *testing.T) {
	redisURL := os.Getenv("OPSMIND_REDIS_URL")
	if redisURL == "" {
		t.Skip("OPSMIND_REDIS_URL is unset; live Redis integration test skipped")
	}
	options, err := redis.ParseURL(redisURL)
	if err != nil {
		t.Fatalf("parse Redis URL: %v", err)
	}
	client := redis.NewClient(options)
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		t.Fatalf("connect to Redis: %v", err)
	}

	stream := "opsmind:test:event-ingestion"
	eventID := "5f7622a1-5d2f-4b6f-b981-64ea003d195a"
	key := "opsmind:event-ingestion:idempotency:" + eventID
	if err := client.Del(ctx, stream, key).Err(); err != nil {
		t.Fatalf("clear test keys: %v", err)
	}
	t.Cleanup(func() { _ = client.Del(context.Background(), stream, key).Err() })

	publisher := &redisPublisher{client: client}
	payload := []byte(`{"event_id":"5f7622a1-5d2f-4b6f-b981-64ea003d195a"}`)
	firstID, firstPublished, err := publisher.PublishOnce(ctx, stream, eventID, time.Minute, payload)
	if err != nil {
		t.Fatalf("first publish: %v", err)
	}
	secondID, secondPublished, err := publisher.PublishOnce(ctx, stream, eventID, time.Minute, payload)
	if err != nil {
		t.Fatalf("replay publish: %v", err)
	}
	if !firstPublished || secondPublished || firstID != secondID {
		t.Fatalf("unexpected publish results: first=(%q,%v), second=(%q,%v)", firstID, firstPublished, secondID, secondPublished)
	}
	entries, err := client.XRange(ctx, stream, "-", "+").Result()
	if err != nil {
		t.Fatalf("read test stream: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("stream entries = %d, want exactly one", len(entries))
	}
}
