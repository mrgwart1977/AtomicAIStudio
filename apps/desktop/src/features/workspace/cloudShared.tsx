// Shared Cloud Studio simulation honesty — banner, badge, and the
// µUSD formatter used by the text and image panels. Every cloud
// surface in this build is SIMULATED; these keep the labeling uniform.

export const SIM_BANNER = "SIMULATION — No external AI provider contacted.";

export function SimBadge({ className = "" }: { className?: string }) {
  return (
    <p role="note" className={`rounded border border-amber/50 bg-amber/10 px-2.5 py-1.5 text-xs font-semibold uppercase tracking-wide text-amber ${className}`}>
      {SIM_BANNER}
    </p>
  );
}

/** µUSD → display dollars. Financial values are always labeled by the
 * surrounding copy — this only formats magnitude. */
export function fmtMicro(micro: number | null | undefined): string {
  if (micro == null) return "—";
  const usd = micro / 1e6;
  return usd === 0 ? "$0" : usd < 0.0001 ? "<$0.0001" : `$${usd.toFixed(4)}`;
}
