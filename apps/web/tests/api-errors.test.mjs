import assert from "node:assert/strict";
import { test } from "node:test";
import { apiErrorFromPayload, ApiError } from "../src/data/errors.ts";

test("uses structured API message and preserves the correlation ID", () => {
  const error = apiErrorFromPayload(409, {
    error: { message: "Incident changed", correlation_id: "request-123" },
  });

  assert.ok(error instanceof ApiError);
  assert.equal(error.message, "Incident changed");
  assert.equal(error.status, 409);
  assert.equal(error.correlationId, "request-123");
});

test("supports FastAPI detail payloads", () => {
  const error = apiErrorFromPayload(422, { detail: "Invalid incident state" });

  assert.equal(error.message, "Invalid incident state");
  assert.equal(error.status, 422);
});

test("falls back safely for empty or malformed payloads", () => {
  assert.equal(apiErrorFromPayload(503, null).message, "Request failed (503)");
  assert.equal(apiErrorFromPayload(500, { error: { message: 7 } }).message, "Request failed (500)");
});
