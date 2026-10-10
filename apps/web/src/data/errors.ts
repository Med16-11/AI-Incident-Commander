export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly correlationId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type ApiErrorPayload = {
  error?: { message?: unknown; correlation_id?: unknown };
  detail?: unknown;
};

export function apiErrorFromPayload(status: number, payload: unknown): ApiError {
  const body = typeof payload === "object" && payload !== null ? (payload as ApiErrorPayload) : {};
  const message =
    (typeof body.error?.message === "string" && body.error.message) ||
    (typeof body.detail === "string" && body.detail) ||
    `Request failed (${status})`;
  const correlationId =
    typeof body.error?.correlation_id === "string" ? body.error.correlation_id : undefined;
  return new ApiError(message, status, correlationId);
}
