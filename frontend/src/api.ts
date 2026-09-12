export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch("/api" + path, { signal, cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
  return data as T;
}
