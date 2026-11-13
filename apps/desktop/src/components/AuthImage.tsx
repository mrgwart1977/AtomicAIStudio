import { useEffect, useState, type ReactNode } from "react";
import { fetchAssetObjectUrl } from "../api/client";

/** Authenticated asset-media loader. The session token travels in the
 * request header, never in a URL — nothing reaches server access logs,
 * browser history, `Referer` headers, or a copied `src`.
 *
 * Guarantees:
 *  - stale-guard: a response that arrives after the asset changed or the
 *    consumer unmounted is revoked immediately and can never overwrite
 *    the newer image;
 *  - bounded retention: at most one live object URL per mounted
 *    consumer, revoked on asset change and on unmount;
 *  - safe failure: missing/denied/failed fetches resolve "error".
 *
 * Returns `null` while loading, `"error"` on failure, or the object URL. */
export function useAssetObjectUrl(
  projectId: string | null | undefined,
  assetId: string | null | undefined,
): string | "error" | null {
  const [state, setState] = useState<string | "error" | null>(null);
  useEffect(() => {
    if (!projectId || !assetId) {
      setState(null);
      return;
    }
    setState(null);
    let url: string | null = null;
    let dead = false;
    fetchAssetObjectUrl(projectId, assetId)
      .then((u) => {
        if (dead) URL.revokeObjectURL(u);
        else {
          url = u;
          setState(u);
        }
      })
      .catch(() => {
        if (!dead) setState("error");
      });
    return () => {
      dead = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [projectId, assetId]);
  return state;
}

/** Drop-in replacement for `<img src={assetFileUrl(...)}>` — renders
 * `fallback` while the authenticated fetch is in flight and if it fails,
 * so every call site keeps its existing empty/loading/failure surface. */
export function AuthImage({ pid, assetId, alt, className, fallback, draggable }: {
  pid: string;
  assetId: string | null | undefined;
  alt: string;
  className?: string;
  fallback: ReactNode;
  draggable?: boolean;
}) {
  const url = useAssetObjectUrl(pid, assetId);
  if (!assetId || url === "error" || url === null) return <>{fallback}</>;
  return <img src={url} alt={alt} className={className} draggable={draggable} />;
}
