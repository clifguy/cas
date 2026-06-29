// Base fetch helpers for SAGE Core API and application backend.
// All paths are relative; Vite proxy routes them to the FastAPI backend.

export class ApiError extends Error {
  code: string;
  detail: unknown;

  constructor(code: string, message: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.detail = detail;
  }
}

// --- Session-expiry signal ---------------------------------------------------
// A live session can lapse mid-use (the server-side session has a fixed TTL).
// When that happens any data call comes back with the `auth_required` code; the
// app needs to drop the user back to the sign-in surface rather than show a dead
// error banner. Every non-ok response flows through errorFromResponse below, so
// that is the single place to detect the code and notify subscribers.

type AuthRequiredListener = () => void;

const authRequiredListeners = new Set<AuthRequiredListener>();

/**
 * Subscribe to the "a call returned auth_required" signal (an absent or expired
 * session surfaced mid-use). Returns an unsubscribe function.
 */
export function onAuthRequired(listener: AuthRequiredListener): () => void {
  authRequiredListeners.add(listener);
  return () => {
    authRequiredListeners.delete(listener);
  };
}

/**
 * Notify every subscriber that a call hit auth_required. Called internally when
 * a response carries that code; also exported so the session state can be driven
 * the same way the real signal drives it.
 */
export function notifyAuthRequired(): void {
  for (const listener of authRequiredListeners) {
    listener();
  }
}

/**
 * Build an ApiError from a non-ok response, parsing the structured error body
 * when present. Emits the session-expiry signal when the body's code is
 * auth_required so an expired session re-gates instead of erroring opaquely.
 */
async function errorFromResponse(response: Response): Promise<ApiError> {
  let body: { code?: string; message?: string; detail?: unknown } | undefined;
  try {
    body = await response.json();
  } catch {
    // non-JSON error body
  }
  const error = new ApiError(
    body?.code ?? `HTTP_${response.status}`,
    body?.message ?? response.statusText,
    body?.detail,
  );
  if (error.code === 'auth_required') {
    notifyAuthRequired();
  }
  return error;
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  return response.json() as Promise<T>;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path);
  return handleResponse<T>(response);
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return handleResponse<T>(response);
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return handleResponse<T>(response);
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return handleResponse<T>(response);
}

/**
 * POST expecting an empty (204 No Content) success response. Unlike apiPost it
 * does not read or parse the body on success, so a no-content response does not
 * trip a JSON parse error. Non-ok responses surface as ApiError as elsewhere.
 */
export async function apiPostVoid(path: string): Promise<void> {
  const response = await fetch(path, { method: 'POST' });
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
}

/**
 * POST returning a Server-Sent Events stream. The caller reads the stream
 * line-by-line and parses JSON from `data: {...}` lines.
 */
export async function apiStream(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<ReadableStream<Uint8Array>> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  if (!response.body) {
    throw new ApiError('NO_BODY', 'Response has no body stream');
  }
  return response.body;
}

/**
 * POST a multipart/form-data body returning a Server-Sent Events stream.
 * The Content-Type header is deliberately omitted so the browser sets the
 * multipart boundary itself; setting it by hand corrupts the boundary and the
 * server fails to parse the parts. The caller reads the returned stream with
 * readSSEStream.
 */
export async function apiUploadStream(
  path: string,
  formData: FormData,
  signal?: AbortSignal,
): Promise<ReadableStream<Uint8Array>> {
  const response = await fetch(path, {
    method: 'POST',
    body: formData,
    signal,
  });
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  if (!response.body) {
    throw new ApiError('NO_BODY', 'Response has no body stream');
  }
  return response.body;
}

/**
 * Parse an SSE stream, calling onEvent for each `data: {...}` line.
 * Returns when the stream ends.
 */
export async function readSSEStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (data: Record<string, unknown>) => void,
): Promise<void> {
  const reader = stream.pipeThrough(new TextDecoderStream() as unknown as ReadableWritablePair<string, Uint8Array>).getReader();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += value;

    // Process complete lines
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? ''; // keep incomplete trailing line

    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('data: ')) {
        try {
          const parsed = JSON.parse(trimmed.slice(6));
          onEvent(parsed);
        } catch (err) {
          console.warn('[SSE] Malformed JSON in data line:', trimmed, err);
        }
      }
    }
  }

  // Process any remaining buffer
  if (buffer.trim().startsWith('data: ')) {
    try {
      const parsed = JSON.parse(buffer.trim().slice(6));
      onEvent(parsed);
    } catch (err) {
      console.warn('[SSE] Malformed JSON in trailing buffer:', buffer.trim(), err);
    }
  }
}
