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

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let body: { code?: string; message?: string; detail?: unknown } | undefined;
    try {
      body = await response.json();
    } catch {
      // non-JSON error
    }
    throw new ApiError(
      body?.code ?? `HTTP_${response.status}`,
      body?.message ?? response.statusText,
      body?.detail,
    );
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
    let errBody: { code?: string; message?: string; detail?: unknown } | undefined;
    try {
      errBody = await response.json();
    } catch {
      // non-JSON
    }
    throw new ApiError(
      errBody?.code ?? `HTTP_${response.status}`,
      errBody?.message ?? response.statusText,
      errBody?.detail,
    );
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
