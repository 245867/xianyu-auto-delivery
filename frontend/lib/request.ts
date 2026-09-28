type QueryParams = Record<string, string | number | boolean | null | undefined>;
type RequestData = Record<string, unknown> | FormData | undefined;

function buildUrl(path: string, params?: QueryParams): string {
  if (!params) return path;

  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null) {
      query.set(key, String(value));
    }
  });

  const queryString = query.toString();
  return queryString ? `${path}${path.includes('?') ? '&' : '?'}${queryString}` : path;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = localStorage.getItem('auth_token');

  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(path, { ...init, headers });
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json')
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    if (response.status === 401 && path !== '/login') {
      localStorage.removeItem('auth_token');
      window.dispatchEvent(new Event('auth:logout'));
    }
    const errorPayload = typeof payload === 'object' && payload !== null
      ? payload as { detail?: unknown; message?: unknown }
      : undefined;
    const message = errorPayload?.detail ?? errorPayload?.message ?? `Request failed (${response.status})`;
    throw new Error(String(message));
  }

  return payload as T;
}

export function get<T = unknown>(path: string, params?: QueryParams): Promise<T> {
  return request<T>(buildUrl(path, params));
}

export function post<T = unknown>(path: string, data?: RequestData): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: data instanceof FormData ? data : data === undefined ? undefined : JSON.stringify(data),
  });
}

export function put<T = unknown>(path: string, data?: RequestData): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    body: data instanceof FormData ? data : data === undefined ? undefined : JSON.stringify(data),
  });
}

export function del<T = unknown>(path: string, data?: RequestData): Promise<T> {
  return request<T>(path, {
    method: 'DELETE',
    body: data instanceof FormData ? data : data === undefined ? undefined : JSON.stringify(data),
  });
}
