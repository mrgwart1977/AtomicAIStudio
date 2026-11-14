// C3-B — cloud image generation inside Cloud Studio.
//
// Everything this panel can run is SIMULATED: provider contracts are
// exercised over mocked transports, the spend ledger records synthetic
// entries only, and generated PNGs are deterministic test artifacts —
// the UI must never present any of it as real provider output.
import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, fetchAssetObjectUrl } from "../../api/client";
import { MaskEditor } from "./MaskEditor";
import { computePad } from "./editorMath";
import { commitCommand, previewCommand } from "../../api/commands";
import type {
  CommandProposal, ImageContracts, Job,
  ProviderDetail, ProviderSummary, SpendReservation, SpendSummary,
} from "../../api/types";
import { useApp } from "../../stores/appStore";
import { useAssetObjectUrl } from "../../components/AuthImage";
import { SimBadge, fmtMicro } from "./cloudShared";

const inputCls =
  "w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg placeholder:text-faint";
const labelCls = "mb-1 block text-xs font-medium text-muted";
const btnCls =
  "rounded-md bg-action px-3 py-1.5 text-sm text-white hover:bg-action/80 disabled:opacity-40";
const ghostBtn =
  "rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-fg disabled:opacity-40";

// Stable empty refs — selectors must never allocate `?? []`/`?? {}`.
const NO_ENTITIES = { character: [], world: [], location: [] } as const;
const NO_STORYBOARD: never[] = [];
const NO_ASSETS = { items: [], total: 0, offset: 0, limit: 0 } as const;

type ImageModel = ProviderDetail["models"][number];

/** Mirrors backend cloud_cost.t1_image_strategy: A-class provider with
 * a documented T1 official_api strategy covering image.* — eligibility
 * for generation additionally requires an implemented contract (the
 * /api/providers/image-contracts list). */
function t1ImageEligible(d: ProviderDetail): boolean {
  return d.access_class === "A" && (d.strategies || []).some(
    (s) => s.method === "official_api" && s.tier === "T1"
      && (s.capabilities || []).some((c) => (c || "").startsWith("image")),
  );
}

/** Mirrors backend image_tier_rate: a named quality picks its tier;
 * anything unspecified (incl. "auto") reserves the HIGHEST documented
 * tier — worst-case estimate, never an under-estimate. */
function tierRate(model: ImageModel | undefined, quality: string): number | null {
  const table = model?.cost_table;
  if (!table) return null;
  if (table.unit === "per_credit") {
    // Documented per-image credit price × USD-per-credit (Recraft,
    // BFL, Firefly, Ideogram) — preliminary hint only; the backend
    // preview remains authoritative.
    const c = table.credits_per_image, u = table.usd_per_credit;
    return c != null && u != null ? c * u : null;
  }
  if (table.unit !== "per_image") return null;
  const rates = table.rates || {};
  if (quality && quality !== "auto" && rates[quality] != null) return rates[quality];
  const vals = Object.values(rates);
  if (vals.length) return Math.max(...vals);
  return table.rate ?? null;
}

function imageModels(d?: ProviderDetail): ImageModel[] {
  return (d?.models || []).filter((m) => (m.capability || "").startsWith("image"));
}

function outcomeLabel(j: Job): string | null {
  const o = j.progress_json?.remote_outcome;
  switch (o) {
    case "artifact_pending_retrieval":
      return "Generated remotely — awaiting artifact retrieval · reconcile required";
    case "ambiguous":
      return "Outcome unknown — reconcile required";
    case "provider_rejected":
      return "Refused by provider contract — no charge recorded";
    case "provider_failed":
      return "Provider reported failure — billing held for reconcile";
    case "succeeded":
      return "Completed";
    case "cancelled":
      return "Cancelled";
    default:
      return null;
  }
}

function needsReconcile(j: Job): boolean {
  return j.recovery_state === "needs_reconciliation"
    || j.progress_json?.reconcile_required === true
    || j.progress_json?.remote_outcome === "ambiguous";
}

interface Pending {
  fingerprint: string;
  payload: Record<string, unknown>;
  proposal: CommandProposal;
  commandType: string;
}

/** Authenticated thumbnail loader — shared implementation lives in
 * components/AuthImage (token in the request header, never a URL). */
const useAssetThumb = useAssetObjectUrl;

export function CloudImagePanel({ pid, revision, simSummary, reservations,
                                  onCommitted }: {
  pid: string;
  revision: number;
  simSummary: SpendSummary | null;
  reservations: SpendReservation[];
  onCommitted: () => void;
}) {
  const jobs = useApp((s) => s.jobs);
  const entities = useApp((s) => s.entities ?? NO_ENTITIES);
  const storyboard = useApp((s) => s.storyboard ?? NO_STORYBOARD);
  const assetsPage = useApp((s) => s.assetsPage ?? NO_ASSETS);
  const setWorkspaceTab = useApp((s) => s.setWorkspaceTab);
  const setAssetKindFilter = useApp((s) => s.setAssetKindFilter);
  const refreshAssets = useApp((s) => s.refreshAssets);

  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [details, setDetails] = useState<Record<string, ProviderDetail>>({});
  const [contracts, setContracts] = useState<string[]>([]);
  const [img2imgContracts, setImg2imgContracts] = useState<string[]>([]);
  const [inpaintContracts, setInpaintContracts] = useState<string[]>([]);
  const [outpaintContracts, setOutpaintContracts] = useState<string[]>([]);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  // C3-D2 — mode: "generate" (text→image) or "img2img" (edit an
  // existing project image). Everything about img2img stays simulated.
  // C3-D3 — inpaint/outpaint extend the edit surface (generate.image.edit).
  const [mode, setMode] = useState<"generate" | "img2img" | "inpaint" | "outpaint">("generate");
  const [sourceAssetId, setSourceAssetId] = useState("");
  const [maskAssetId, setMaskAssetId] = useState("");
  const [outW, setOutW] = useState("");
  const [outH, setOutH] = useState("");
  const [placement, setPlacement] = useState("center");
  const [srcObjUrl, setSrcObjUrl] = useState<string | null>(null);
  const [refIds, setRefIds] = useState<string[]>([]);
  const [refWeights, setRefWeights] = useState<Record<string, string>>({});

  // generate form — every field participates in the preview
  // fingerprint: touching ANY of them invalidates a pending approval.
  const [providerKey, setProviderKey] = useState("");
  const [modelId, setModelId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [negativePrompt, setNegativePrompt] = useState("");
  const [size, setSize] = useState("auto");
  const [quality, setQuality] = useState("auto");
  const [seed, setSeed] = useState("");
  const [sceneId, setSceneId] = useState("");
  const [shotId, setShotId] = useState("");
  const [characterId, setCharacterId] = useState("");
  const [maxCost, setMaxCost] = useState("");

  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const inFlight = useRef(false);

  const reload = () => {
    setLoadErr(null);
    apiFetch<ProviderSummary[]>("/api/providers?category=image")
      .then(async (list) => {
        setProviders(list);
        const ds = await Promise.all(
          list.map((p) =>
            apiFetch<ProviderDetail>(`/api/providers/${p.key}`)
              .then((d) => [p.key, d] as const).catch(() => null)),
        );
        setDetails(Object.fromEntries(ds.filter((d): d is NonNullable<typeof d> => !!d)));
      })
      .catch((e) => setLoadErr(`providers: ${String(e)}`));
    apiFetch<ImageContracts>("/api/providers/image-contracts")
      .then((c) => {
        setContracts(c.implemented);
        setImg2imgContracts(c.img2img_implemented ?? []);
        setInpaintContracts(c.inpaint_implemented ?? []);
        setOutpaintContracts(c.outpaint_implemented ?? []);
      })
      .catch((e) => setLoadErr(`image contracts: ${String(e)}`));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(reload, []);

  // A provider is selectable only when the catalog marks it eligible
  // AND a contract is implemented — everything else is listed disabled
  // with the reason, never presented as operational. In img2img mode
  // the implemented-contract list narrows to providers with a real
  // edits endpoint — a catalog editing flag alone is never enough.
  const activeContracts = mode === "img2img" ? img2imgContracts
    : mode === "inpaint" ? inpaintContracts
    : mode === "outpaint" ? outpaintContracts : contracts;
  const selectable = useMemo(
    () => providers.filter((p) =>
      activeContracts.includes(p.key) && details[p.key] && t1ImageEligible(details[p.key])),
    [providers, activeContracts, details],
  );
  const unavailable = useMemo(
    () => providers.filter((p) => !selectable.some((s) => s.key === p.key)),
    [providers, selectable],
  );

  const detail = providerKey ? details[providerKey] : undefined;
  const models = useMemo(
    () => imageModels(detail).filter((m) =>
      mode === "img2img" ? m.editing?.img2img === true
      : mode === "inpaint" ? m.editing?.mask_inpaint === true
      : mode === "outpaint" ? m.editing?.outpaint === true : true),
    [detail, mode],
  );

  const model = models.find((m) => m.id === modelId);
  const caps = model?.editing ?? {};
  const maxRefs = mode === "img2img" ? (caps.max_reference_images ?? 0) : 0;

  // Mode switch: drop selections that no longer apply so a stale model
  // or oversized reference set can never be committed by accident.
  useEffect(() => {
    if (mode !== "img2img") { setRefIds([]); setRefWeights({}); }
    if (mode === "generate") { setSourceAssetId(""); setMaskAssetId(""); }
    if (mode !== "inpaint") setMaskAssetId("");
    if (providerKey && !selectable.some((p) => p.key === providerKey)) {
      setProviderKey(""); setModelId("");
    }
    if (modelId && !models.some((m) => m.id === modelId)) {
      setModelId(models[0]?.id ?? "");
    }
    if ((mode === "img2img" || mode === "inpaint") && refIds.length > maxRefs) {
      setRefIds((r) => r.slice(0, maxRefs));
    }
  }, [mode, selectable, providerKey, modelId, models, maxRefs, refIds.length]);

  // C3-D4 — authenticated object URL for the painted-mask editor.
  useEffect(() => {
    let revoke: string | null = null;
    if (mode === "inpaint" && sourceAssetId) {
      fetchAssetObjectUrl(pid, sourceAssetId)
        .then((u) => { revoke = u; setSrcObjUrl(u); })
        .catch(() => setSrcObjUrl(null));
    } else setSrcObjUrl(null);
    return () => { if (revoke) URL.revokeObjectURL(revoke); };
  }, [mode, pid, sourceAssetId]);

  // Project image assets usable as img2img source/references.
  const imageAssets = useMemo(
    () => assetsPage.items.filter((a) => a.kind === "image"),
    [assetsPage],
  );
  const srcDims = useMemo(() => {
    const a = imageAssets.find((x) => x.id === sourceAssetId);
    const m = (a?.media_json ?? {}) as Record<string, unknown>;
    const w = Number(m.width), h = Number(m.height);
    return w && h ? { w, h } : null;
  }, [imageAssets, sourceAssetId]);

  const sizes = model?.sizes ?? [];
  const qualities = useMemo(() => {
    if (model?.qualities?.length) return model.qualities;
    const tiers = Object.keys(model?.cost_table?.rates ?? {});
    return tiers.length ? ["auto", ...tiers] : [];
  }, [model]);

  const estRate = useMemo(() => tierRate(model, quality), [model, quality]);
  const maxCostNum = parseFloat(maxCost);

  // Budget availability (synthetic simulated ledger): the smallest
  // available headroom across capped scopes gates nothing client-side —
  // it is an honest hint that the backend commit could refuse.
  const budgetHint = useMemo(() => {
    if (!simSummary || estRate == null) return null;
    const rows = simSummary.scopes.filter((r) => r.limit_micro != null);
    if (!rows.length) return null;
    const tightest = rows.reduce((a, b) =>
      (a.limit_micro! - a.consumed_micro) <= (b.limit_micro! - b.consumed_micro) ? a : b);
    const avail = tightest.limit_micro! - tightest.consumed_micro;
    return { scope: tightest.scope, avail, over: estRate * 1e6 > avail };
  }, [simSummary, estRate]);

  // Filmmaking associations — catalog data already in the store.
  const scenes = useMemo(
    () => storyboard.map((s) => ({ id: s.scene.id, title: s.scene.title })),
    [storyboard],
  );
  const shots = useMemo(() => {
    const all = storyboard.flatMap((s) =>
      s.shots.map((sh) => ({
        id: sh.shot.id, title: sh.shot.title, sceneId: s.scene.id,
        sceneTitle: s.scene.title,
      })));
    return sceneId ? all.filter((sh) => sh.sceneId === sceneId) : all;
  }, [storyboard, sceneId]);
  const characters = useMemo(
    () => entities.character.filter((c) => c.status !== "archived"),
    [entities],
  );

  const isEdit = mode === "inpaint" || mode === "outpaint";
  const commandType = mode === "img2img" ? "generate.image.img2img"
    : isEdit ? "generate.image.edit" : "generate.image.cloud";

  const payload = (): Record<string, unknown> => ({
    provider_key: providerKey,
    model_id: modelId || undefined,
    prompt,
    negative_prompt: negativePrompt || undefined,
    execution_target: "cloud",
    size: size && size !== "auto" ? size : undefined,
    quality: quality && quality !== "auto" ? quality : undefined,
    seed: seed.trim() === "" ? undefined : Number(seed),
    scene_id: sceneId || undefined,
    shot_id: shotId || undefined,
    character_id: characterId || undefined,
    max_cost_usd: maxCost.trim() === "" ? undefined : maxCostNum,
    ...(isEdit ? {
      operation: mode,
      source_asset_id: sourceAssetId,
      ...(mode === "inpaint" ? { mask_asset_id: maskAssetId } : {
        out_width: outW.trim() === "" ? undefined : Number(outW),
        out_height: outH.trim() === "" ? undefined : Number(outH),
        placement,
      }),
    } : {}),
    ...(mode === "img2img" ? {
      source_asset_id: sourceAssetId,
      reference_asset_ids: refIds.length ? refIds : undefined,
      reference_weights: caps.reference_weights && refIds.length
        ? refIds.map((id) => {
            const w = parseFloat(refWeights[id] ?? "");
            return Number.isFinite(w) ? w : undefined;
          })
        : undefined,
    } : {}),
  });

  const fingerprint = JSON.stringify([
    mode, providerKey, modelId, prompt, negativePrompt, size, quality,
    seed, sceneId, shotId, characterId, maxCost,
    sourceAssetId, maskAssetId, outW, outH, placement,
    refIds, refWeights]);

  // Preview → approval integrity: ANY parameter change after a preview
  // drops the pending approval — a fresh preview is required. This is
  // the UI-side rule; the backend re-validates the full payload anyway.
  useEffect(() => {
    if (pending && pending.fingerprint !== fingerprint) setPending(null);
  }, [fingerprint, pending]);

  const genReady = !!providerKey && selectable.some((p) => p.key === providerKey)
    && !!prompt.trim() && prompt.length <= 8000
    && ((mode === "img2img" || isEdit) ? !!sourceAssetId : true)
    && (mode !== "inpaint" || !!maskAssetId)
    && (mode !== "outpaint"
        || (!!outW.trim() && !!outH.trim()
            && Number(outW) > 0 && Number(outH) > 0));

  // If the backend previewed but could not price (a path that exists
  // for future billing units), approving cannot be offered — the
  // commit would refuse, so the button is disabled up front.
  const unpriced = !!pending && pending.proposal.cost_estimate != null
    && pending.proposal.cost_estimate.est_cost_usd == null;

  const doPreview = () => {
    if (!genReady || inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(null); setNotice(null);
    const p = payload();
    previewCommand(pid, commandType, p)
      .then((proposal) => setPending(
        { fingerprint, payload: p, proposal, commandType }))
      .catch((e) => setError(`Preview failed: ${String(e)}`))
      .finally(() => { inFlight.current = false; setBusy(false); });
  };

  const doCommit = () => {
    // Commit the exact previewed payload — never the live form state,
    // so a changed form can never slip through a stale approval.
    if (!pending || pending.fingerprint !== fingerprint || inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(null); setNotice(null);
    commitCommand(pid, revision, pending.commandType, pending.payload)
      .then((r) => {
        setNotice(`Committed ${pending.commandType} — job ${r.job_id?.slice(0, 8) ?? ""} (simulated)`);
        setPending(null);
        onCommitted();
      })
      .catch((e) => setError(`Commit failed: ${String(e)}`))
      .finally(() => { inFlight.current = false; setBusy(false); });
  };

  const imageJobs = useMemo(
    () => Object.values(jobs)
      .filter((j) => j.adapter_id === "cloud.image.sim"
        || j.adapter_id === "cloud.image.img2img.sim")
      .sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [jobs],
  );

  const assetById = useMemo(
    () => new Map(assetsPage.items.map((a) => [a.id, a])),
    [assetsPage],
  );

  const viewInAssets = () => {
    setAssetKindFilter("image");
    void refreshAssets();
    setWorkspaceTab("assets");
  };

  return (
    <section className="mt-4 rounded-md border border-line bg-card p-4" data-testid="cloud-image-panel">
      <h2 className="text-sm font-semibold">Generate image — simulated cloud</h2>
      <SimBadge className="mt-2" />
      <p className="mt-2 text-xs text-muted">
        Runs a documented provider request shape on a <b>simulated transport</b> —
        the provider is never contacted and the result is a deterministic test
        image, not real provider output. Simulated accounting only.
      </p>

      {/* ---------- C3-D2: mode toggle ---------- */}
      <div className="mt-3 flex gap-2" role="group" aria-label="Generation mode">
        <button type="button"
          className={mode === "generate" ? btnCls : ghostBtn}
          data-testid="ci-mode-generate"
          onClick={() => setMode("generate")}>
          New image from text
        </button>
        <button type="button"
          className={mode === "img2img" ? btnCls : ghostBtn}
          data-testid="ci-mode-img2img"
          onClick={() => setMode("img2img")}>
          Edit an existing image
        </button>
        <button type="button"
          className={mode === "inpaint" ? btnCls : ghostBtn}
          data-testid="ci-mode-inpaint"
          onClick={() => setMode("inpaint")}>
          Inpaint region
        </button>
        <button type="button"
          className={mode === "outpaint" ? btnCls : ghostBtn}
          data-testid="ci-mode-outpaint"
          onClick={() => setMode("outpaint")}>
          Outpaint canvas
        </button>
      </div>
      {mode === "img2img" && (
        <p className="mt-2 rounded border border-line bg-panel p-2 text-xs text-muted"
          data-testid="ci-img2img-note">
          Image-to-image transforms one of your project images into a new
          asset — the original is never changed. Only providers with an
          implemented edits contract appear below.
        </p>
      )}
      {mode === "inpaint" && (
        <p className="mt-2 rounded border border-line bg-panel p-2 text-xs text-muted"
          data-testid="ci-inpaint-note">
          Inpainting regenerates only the masked region — the mask is a
          project image where bright pixels (luminance ≥ 128) mark what
          may change. Mask must match the source dimensions exactly;
          neither source nor mask is ever modified. Output is a new asset.
        </p>
      )}
      {mode === "outpaint" && (
        <p className="mt-2 rounded border border-line bg-panel p-2 text-xs text-muted"
          data-testid="ci-outpaint-note">
          Outpainting extends the canvas — the original image region is
          preserved and the new border is generated. Choose explicit
          target dimensions and where the original sits. Output is a
          new asset; the source is never modified.
        </p>
      )}

      {loadErr && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{loadErr}</p>}
      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}
      {notice && <p className="mt-3 rounded border border-teal/40 bg-teal/10 p-2 text-sm text-teal">{notice}</p>}

      {/* ---------- approval card (fresh preview required on change) ---------- */}
      {pending && (
        <section className="mt-4 rounded-md border border-amber/60 bg-panel p-4" data-testid="ci-approval">
          <h3 className="text-sm font-semibold">
            Review — {pending.commandType === "generate.image.edit"
              ? `${String(pending.payload.operation ?? "edit")} (simulated)`
              : pending.commandType === "generate.image.img2img"
              ? "image-to-image edit" : "generate cloud image"}
          </h3>
          <SimBadge className="mt-2" />
          <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 rounded border border-line bg-card p-2.5 text-xs sm:grid-cols-2" data-testid="ci-approval-detail">
            <div className="flex justify-between"><dt className="text-faint">Provider</dt><dd className="text-fg">{String(pending.payload.provider_key)}</dd></div>
            <div className="flex justify-between"><dt className="text-faint">Model</dt><dd className="text-fg">{String(pending.payload.model_id ?? "provider default")}</dd></div>
            <div className="flex justify-between"><dt className="text-faint">Simulation status</dt><dd className="text-amber">simulated — mocked transport</dd></div>
            <div className="flex justify-between"><dt className="text-faint">Output</dt><dd className="text-fg">1 image · test PNG{pending.commandType === "generate.image.img2img" ? " · new asset" : ""}</dd></div>
            {pending.commandType === "generate.image.img2img" && (
              <div className="flex justify-between"><dt className="text-faint">Source</dt><dd className="text-fg" data-testid="ci-approval-source">{String(pending.payload.source_asset_id)}{Array.isArray(pending.payload.reference_asset_ids) && (pending.payload.reference_asset_ids as unknown[]).length > 0 ? ` · ${(pending.payload.reference_asset_ids as unknown[]).length} reference(s)` : ""}</dd></div>
            )}
            {pending.commandType === "generate.image.edit" && (
              <div className="flex justify-between"><dt className="text-faint">Source</dt><dd className="text-fg" data-testid="ci-approval-source">{String(pending.payload.source_asset_id)}{pending.payload.mask_asset_id ? ` · mask ${String(pending.payload.mask_asset_id)}` : ""}{pending.payload.out_width ? ` · → ${String(pending.payload.out_width)}×${String(pending.payload.out_height)} ${String(pending.payload.placement ?? "")}` : ""}</dd></div>
            )}
            <div className="flex justify-between"><dt className="text-faint">Size</dt><dd className="text-fg">{String(pending.payload.size ?? "provider default")}</dd></div>
            <div className="flex justify-between"><dt className="text-faint">Quality</dt><dd className="text-fg">{String(pending.payload.quality ?? "provider default")}</dd></div>
            <div className="flex justify-between"><dt className="text-faint">Est. cost</dt><dd className="text-fg" data-testid="ci-approval-cost">{pending.proposal.cost_estimate?.est_cost_usd != null ? `$${pending.proposal.cost_estimate.est_cost_usd.toFixed(4)} (authoritative preview — estimate, not a charge)` : "not priced — commit may refuse"}</dd></div>
            <div className="flex justify-between"><dt className="text-faint">Data out</dt><dd className="text-fg">prompt text (~{String(pending.payload.prompt ?? "").length} chars){pending.commandType === "generate.image.img2img" ? " + image bytes" : ""} — nothing else sent</dd></div>
          </dl>
          <p className="mt-2 text-sm text-fg">{pending.proposal.summary}</p>
          {pending.proposal.warnings.length > 0 && (
            <ul className="mt-2 list-inside list-disc text-xs text-amber">
              {pending.proposal.warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          )}
          <div className="mt-3 flex gap-2">
            <button onClick={doCommit} disabled={busy || unpriced} className={btnCls}
              data-testid="ci-approve">
              {pending.commandType === "generate.image.img2img"
                || pending.commandType === "generate.image.edit"
                ? "Approve & generate simulated edit"
                : "Approve & generate simulated image"}
            </button>
            <button onClick={() => setPending(null)} disabled={busy} className={ghostBtn}>
              Discard preview
            </button>
          </div>
          <p className="mt-2 text-[10px] text-faint">
            Changing any setting above discards this approval — a fresh preview is required.
            {unpriced && " This request cannot be priced — the commit would be refused."}
          </p>
        </section>
      )}

      {/* ---------- form ---------- */}
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label className={labelCls} htmlFor="ci-provider">Image provider</label>
          <select id="ci-provider" className={inputCls} value={providerKey}
            data-testid="ci-provider"
            onChange={(e) => {
              setProviderKey(e.target.value);
              // First image-capable model preselected — one less thing
              // for a nontechnical user to get wrong.
              setModelId(imageModels(details[e.target.value])[0]?.id ?? "");
            }}>
            <option value="">Choose provider…</option>
            {selectable.map((p) => (
              <option key={p.key} value={p.key}>{p.label} (simulated API)</option>
            ))}
            {unavailable.map((p) => (
              <option key={p.key} value={p.key} disabled>
                {p.label} — no implemented contract in this build
              </option>
            ))}
          </select>
          {unavailable.length > 0 && (
            <p className="mt-1 text-xs text-faint" data-testid="ci-unavailable">
              {unavailable.length} catalog provider{unavailable.length > 1 ? "s" : ""} listed
              without an implemented contract — shown for visibility only,
              cannot be selected.
            </p>
          )}
          {selectable.length === 0 && providers.length > 0 && (
            <p className="mt-1 text-xs text-faint">No implemented image contracts in this build.</p>
          )}
        </div>
        <div>
          <label className={labelCls} htmlFor="ci-model">Image model</label>
          <select id="ci-model" className={inputCls} value={modelId}
            data-testid="ci-model"
            onChange={(e) => setModelId(e.target.value)} disabled={!providerKey}>
            {models.length === 0 && <option value="">No image models in catalog</option>}
            {models.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
          </select>
        </div>
      </div>

      {/* ---------- C3-D2: source + references (img2img only) ---------- */}
      {mode === "img2img" && (
        <fieldset className="mt-3 rounded-md border border-line bg-panel p-3"
          data-testid="ci-img2img-inputs">
          <legend className="px-1 text-xs font-medium text-muted">
            Source &amp; references
          </legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className={labelCls} htmlFor="ci-source">Source image *</label>
              <select id="ci-source" className={inputCls} value={sourceAssetId}
                data-testid="ci-source"
                onChange={(e) => {
                  setSourceAssetId(e.target.value);
                  setRefIds((r) => r.filter((id) => id !== e.target.value));
                }}>
                <option value="">Choose an image asset…</option>
                {imageAssets.map((a) => (
                  <option key={a.id} value={a.id}>{a.name || a.id}</option>
                ))}
              </select>
              {imageAssets.length === 0 && (
                <p className="mt-1 text-xs text-faint">
                  No image assets in this project yet — import one first.
                </p>
              )}
            </div>
            <div>
              <span className={labelCls} id="ci-refs-label">
                Reference images (optional{maxRefs ? `, up to ${maxRefs}` : ""})
              </span>
              <ul className="max-h-32 overflow-auto rounded border border-line bg-card p-1"
                aria-labelledby="ci-refs-label" data-testid="ci-refs">
                {imageAssets.filter((a) => a.id !== sourceAssetId).map((a) => {
                  const on = refIds.includes(a.id);
                  const full = !on && refIds.length >= maxRefs;
                  return (
                    <li key={a.id} className="flex items-center gap-2 px-1 py-0.5">
                      <label className="flex flex-1 items-center gap-2 text-xs text-fg">
                        <input type="checkbox" checked={on} disabled={full}
                          data-testid={`ci-ref-${a.id}`}
                          onChange={() => setRefIds((r) =>
                            on ? r.filter((x) => x !== a.id) : [...r, a.id])} />
                        {a.name || a.id}
                      </label>
                      {on && caps.reference_weights && (
                        <input type="number" min="0" max="2" step="0.1"
                          className="w-16 rounded border border-line bg-card px-1 py-0.5 text-xs text-fg"
                          aria-label={`weight for ${a.name || a.id}`}
                          data-testid={`ci-refw-${a.id}`}
                          value={refWeights[a.id] ?? "1"}
                          onChange={(e) => setRefWeights(
                            (w) => ({ ...w, [a.id]: e.target.value }))} />
                      )}
                    </li>
                  );
                })}
              </ul>
              {maxRefs === 0 && (
                <p className="mt-1 text-xs text-faint">
                  This model accepts no reference images.
                </p>
              )}
              {caps.reference_weights && refIds.length > 0 && (
                <p className="mt-1 text-[10px] text-faint">
                  Weights are guidance strength (0–2) — they steer the edit
                  but do not guarantee identity.
                </p>
              )}
            </div>
          </div>
        </fieldset>
      )}

      {/* ---------- C3-D3: source + mask (inpaint) ---------- */}
      {mode === "inpaint" && (
        <fieldset className="mt-3 rounded-md border border-line bg-panel p-3"
          data-testid="ci-inpaint-inputs">
          <legend className="px-1 text-xs font-medium text-muted">
            Source &amp; mask
          </legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className={labelCls} htmlFor="ci-source">Source image *</label>
              <select id="ci-source" className={inputCls} value={sourceAssetId}
                data-testid="ci-source"
                onChange={(e) => {
                  setSourceAssetId(e.target.value);
                  if (e.target.value === maskAssetId) setMaskAssetId("");
                }}>
                <option value="">Choose an image asset…</option>
                {imageAssets.map((a) => (
                  <option key={a.id} value={a.id}>{a.name || a.id}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={labelCls} htmlFor="ci-mask">Mask image *</label>
              <select id="ci-mask" className={inputCls} value={maskAssetId}
                data-testid="ci-mask"
                onChange={(e) => setMaskAssetId(e.target.value)}>
                <option value="">Choose a mask asset…</option>
                {imageAssets.filter((a) => a.id !== sourceAssetId).map((a) => (
                  <option key={a.id} value={a.id}>{a.name || a.id}</option>
                ))}
              </select>
              <p className="mt-1 text-[10px] text-faint">
                Same dimensions as the source; bright pixels mark the
                editable region (import a mask PNG via Assets first).
              </p>
            </div>
          </div>
          <div className="mt-3">
            <span className={labelCls}>Paint a mask</span>
            <MaskEditor pid={pid} revision={revision} imageUrl={srcObjUrl}
              onMaskImported={(id) => { setMaskAssetId(id); refreshAssets(); }}
              onNotice={(m) => setNotice(m)} onError={(m) => setError(m)} />
          </div>
        </fieldset>
      )}

      {/* ---------- C3-D3: source + canvas (outpaint) ---------- */}
      {mode === "outpaint" && (
        <fieldset className="mt-3 rounded-md border border-line bg-panel p-3"
          data-testid="ci-outpaint-inputs">
          <legend className="px-1 text-xs font-medium text-muted">
            Source &amp; canvas
          </legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
            <div className="sm:col-span-2">
              <label className={labelCls} htmlFor="ci-source">Source image *</label>
              <select id="ci-source" className={inputCls} value={sourceAssetId}
                data-testid="ci-source"
                onChange={(e) => setSourceAssetId(e.target.value)}>
                <option value="">Choose an image asset…</option>
                {imageAssets.map((a) => (
                  <option key={a.id} value={a.id}>{a.name || a.id}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={labelCls} htmlFor="ci-outw">Target W×H *</label>
              <div className="flex items-center gap-1">
                <input id="ci-outw" type="number" min="1" max="4096"
                  className={inputCls} value={outW} data-testid="ci-outw"
                  placeholder="width" onChange={(e) => setOutW(e.target.value)} />
                <span className="text-xs text-faint">×</span>
                <input id="ci-outh" type="number" min="1" max="4096"
                  className={inputCls} value={outH} data-testid="ci-outh"
                  placeholder="height" onChange={(e) => setOutH(e.target.value)} />
              </div>
            </div>
            <div>
              <label className={labelCls} htmlFor="ci-placement">Placement *</label>
              <select id="ci-placement" className={inputCls} value={placement}
                data-testid="ci-placement"
                onChange={(e) => setPlacement(e.target.value)}>
                {["center", "top", "bottom", "left", "right",
                  "top_left", "top_right", "bottom_left", "bottom_right"]
                  .map((p2) => <option key={p2} value={p2}>{p2.replace("_", " ")}</option>)}
              </select>
              <p className="mt-1 text-[10px] text-faint">
                Where the original sits inside the new canvas — its pixels
                are preserved; only the expansion is generated.
              </p>
            </div>
            <div className="sm:col-span-4" data-testid="ci-outpaint-preview">
              {(() => {
                const sw = srcDims?.w ?? 0, sh = srcDims?.h ?? 0;
                const ow = Number(outW) || 0, oh = Number(outH) || 0;
                if (!sw || !ow || !oh) return (
                  <p className="text-[10px] text-faint">
                    Pick a source and enter target dimensions to preview placement.
                  </p>);
                const sc = Math.min(220 / ow, 140 / oh, 1);
                const pad = computePad(sw, sh, ow, oh, placement as never);
                return (
                  <div className="relative mx-auto mt-1 border border-line bg-black/40"
                    style={{ width: ow * sc, height: oh * sc }}>
                    <div className="absolute bg-teal/60"
                      style={{ left: pad.left * sc, top: pad.top * sc,
                               width: sw * sc, height: sh * sc }} />
                    <span className="absolute inset-0 flex items-center justify-center text-[9px] text-faint">
                      {ow}×{oh} — teal = original preserved region
                    </span>
                  </div>);
              })()}
            </div>
          </div>
        </fieldset>
      )}

      <div className="mt-3">
        <label className={labelCls} htmlFor="ci-prompt">
          {mode === "img2img" ? "Edit prompt" : "Image prompt"}
        </label>
        <textarea id="ci-prompt" className={`${inputCls} min-h-24`} value={prompt}
          data-testid="ci-prompt"
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Describe the image — a keyframe, a poster, a texture…" />
        <p className="mt-0.5 text-right text-[10px] text-faint">{prompt.length}/8000</p>
      </div>

      <div className="mt-2">
        <label className={labelCls} htmlFor="ci-negative">
          Negative prompt (optional — {model?.negative_prompt
            ? "sent to the provider"
            : "saved with the asset metadata; not sent for this contract"})
        </label>
        <textarea id="ci-negative" className={`${inputCls} min-h-10`} value={negativePrompt}
          onChange={(e) => setNegativePrompt(e.target.value)}
          placeholder="What to avoid…" />
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div>
          <label className={labelCls} htmlFor="ci-size">Size</label>
          <select id="ci-size" className={inputCls} value={size}
            data-testid="ci-size"
            onChange={(e) => setSize(e.target.value)} disabled={!model}>
            <option value="auto">Provider default</option>
            {sizes.filter((s) => s !== "auto").map((s) => {
              const [w, h] = s.split("x").map(Number);
              const ar = w && h ? (w / h).toFixed(2) : null;
              return <option key={s} value={s}>{s}{ar ? ` (${ar}:1)` : ""}</option>;
            })}
          </select>
        </div>
        <div>
          <label className={labelCls} htmlFor="ci-quality">Quality</label>
          <select id="ci-quality" className={inputCls} value={quality}
            data-testid="ci-quality"
            onChange={(e) => setQuality(e.target.value)} disabled={!model || qualities.length === 0}>
            {qualities.length === 0 && <option value="auto">Provider default</option>}
            {qualities.map((q) => (
              <option key={q} value={q}>
                {q === "auto" ? "Provider default (estimated at highest tier)" : q}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className={labelCls} htmlFor="ci-seed">
            Seed (optional — {model?.seed
              ? "sent to the provider"
              : "saved in metadata; not sent for this contract"})
          </label>
          <input id="ci-seed" className={inputCls} type="number" value={seed}
            onChange={(e) => setSeed(e.target.value)} placeholder="random" />
        </div>
      </div>

      {/* ---------- filmmaking associations ---------- */}
      <fieldset className="mt-3 rounded-md border border-line bg-panel p-3">
        <legend className="px-1 text-xs font-medium text-muted">
          Attach to project (optional)
        </legend>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div>
            <label className={labelCls} htmlFor="ci-scene">Scene</label>
            <select id="ci-scene" className={inputCls} value={sceneId}
              data-testid="ci-scene"
              onChange={(e) => { setSceneId(e.target.value); setShotId(""); }}>
              <option value="">None</option>
              {scenes.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
            </select>
          </div>
          <div>
            <label className={labelCls} htmlFor="ci-shot">Shot</label>
            <select id="ci-shot" className={inputCls} value={shotId}
              data-testid="ci-shot"
              onChange={(e) => setShotId(e.target.value)}>
              <option value="">None</option>
              {shots.map((s) => (
                <option key={s.id} value={s.id}>
                  {sceneId ? s.title : `${s.sceneTitle} · ${s.title}`}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelCls} htmlFor="ci-character">Character</label>
            <select id="ci-character" className={inputCls} value={characterId}
              data-testid="ci-character"
              onChange={(e) => setCharacterId(e.target.value)}>
              <option value="">None</option>
              {characters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
        </div>
        <p className="mt-2 text-[10px] text-faint">
          Associations are stored on the generated asset — they make it
          findable from the storyboard and bibles, nothing is sent anywhere.
        </p>
      </fieldset>

      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label className={labelCls} htmlFor="ci-maxcost">
            Max cost cap (USD, optional — defaults to the estimate)
          </label>
          <input id="ci-maxcost" className={inputCls} type="number" min="0" step="0.01"
            value={maxCost} onChange={(e) => setMaxCost(e.target.value)}
            placeholder={estRate != null ? estRate.toFixed(4) : "0.19"} />
        </div>
        <div className="flex flex-col justify-end">
          <p className="text-xs text-muted" data-testid="ci-cost-estimate">
            Estimated cost:{" "}
            <b>{estRate != null ? `$${estRate.toFixed(4)}` : "shown at preview"}</b>
            <span className="text-faint"> — per image, preliminary estimate; the backend preview is authoritative</span>
          </p>
          {budgetHint && (
            <p className={`mt-1 text-xs ${budgetHint.over ? "text-amber" : "text-faint"}`}
              data-testid="ci-budget-hint">
              {budgetHint.over
                ? `May exceed the ${budgetHint.scope} budget — only ${fmtMicro(budgetHint.avail)} (synthetic) remains; the commit could be refused.`
                : `${fmtMicro(budgetHint.avail)} (synthetic) available in the ${budgetHint.scope} budget.`}
            </p>
          )}
        </div>
      </div>

      <p className="mt-2 rounded border border-line bg-panel p-2 text-xs text-muted" data-testid="ci-data-out">
        Data leaving this device (if this were a live call):{" "}
        <b>prompt text (~{prompt.length} chars){detail ? ` → ${detail.label}` : ""}</b>
        {mode === "img2img" ? " plus the selected image bytes" : ""}.
        No project files, bibles, or credentials are sent{mode === "img2img" ? " beyond the images you picked" : ""} — and nothing
        is sent at all on the simulated transport.
      </p>

      <div className="mt-3">
        <button className={btnCls} disabled={!genReady || busy}
          data-testid="ci-preview-btn" onClick={doPreview}>
          {mode === "img2img" ? "Preview edit request" : "Preview image request"}
        </button>
      </div>

      {/* ---------- job lifecycle + results ---------- */}
      <div className="mt-4 border-t border-line pt-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Cloud image jobs
        </h3>
        {imageJobs.length === 0 && (
          <p className="mt-2 text-xs text-muted">No cloud image generations yet.</p>
        )}
        <ul className="mt-3 space-y-3">
          {imageJobs.map((j) => (
            <CloudImageJobCard key={j.id} job={j} pid={pid}
              reservation={reservations.find((r) => r.job_id === j.id)}
              asset={j.output_asset_id ? assetById.get(j.output_asset_id) : undefined}
              scenes={scenes} shots={shots} characters={characters}
              onViewAssets={viewInAssets} />
          ))}
        </ul>
      </div>
    </section>
  );
}

// ---------- job card ----------

const STATUS_STYLE: Record<string, string> = {
  queued: "border-line text-muted",
  running: "border-amber/50 text-amber",
  succeeded: "border-teal/50 text-teal",
  failed: "border-danger/50 text-danger",
  cancelled: "border-line text-faint",
};

function CloudImageJobCard({ job, pid, reservation, asset, scenes,
                             shots, characters, onViewAssets }: {
  job: Job;
  pid: string;
  reservation?: SpendReservation;
  asset?: { id: string; media_json?: Record<string, unknown> };
  scenes: { id: string; title: string }[];
  shots: { id: string; title: string; sceneId: string }[];
  characters: { id: string; name: string }[];
  onViewAssets: () => void;
}) {
  const d = job.progress_json;
  const intent = d?.remote_intent;
  const running = job.status === "running" || job.status === "queued";
  const outcome = outcomeLabel(job);
  const reconcile = needsReconcile(job)
    || d?.remote_outcome === "artifact_pending_retrieval";
  const media = asset?.media_json ?? {};
  const thumb = useAssetThumb(pid, job.output_asset_id);

  const links = [
    media.scene_id && `scene: ${scenes.find((s) => s.id === media.scene_id)?.title ?? "linked"}`,
    media.shot_id && `shot: ${shots.find((s) => s.id === media.shot_id)?.title ?? "linked"}`,
    media.character_id && `character: ${characters.find((c) => c.id === media.character_id)?.name ?? "linked"}`,
  ].filter(Boolean).join(" · ");

  return (
    <li className="rounded-md border border-line bg-panel p-3" data-testid={`ci-job-${job.id}`}>
      <SimBadge />
      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs">
        <span className="text-muted">
          {intent?.provider_key ?? job.adapter_id}
          {intent?.model_id ? ` · ${intent.model_id}` : ""}
        </span>
        <span className="flex items-center gap-2">
          <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${STATUS_STYLE[job.status] ?? STATUS_STYLE.queued}`}
            data-testid={`ci-status-${job.id}`}>
            {job.status}
          </span>
          <span className="text-faint">{job.id.slice(0, 8)}</span>
        </span>
      </div>

      {running && (
        <div className="mt-2">
          <div className="h-1.5 w-full overflow-hidden rounded bg-line/40">
            <div className="h-full bg-amber transition-all"
              style={{ width: `${Math.round((job.progress || 0) * 100)}%` }} />
          </div>
          <p className="mt-1 text-[10px] text-faint">
            {job.message || "working…"} — generation status (financial status is separate below)
          </p>
        </div>
      )}

      {outcome && (
        <p className={`mt-2 text-xs ${reconcile ? "text-amber" : "text-muted"}`}
          data-testid={`ci-outcome-${job.id}`}>
          {outcome}
        </p>
      )}
      {job.error && <p className="mt-1 text-xs text-danger">{job.error}</p>}

      {/* financial status — always labeled synthetic, never implied real */}
      <p className="mt-1 text-[10px] text-faint" data-testid={`ci-fin-${job.id}`}>
        simulated accounting:{" "}
        {reservation
          ? <>reservation <b>{reservation.status}</b>
              {reservation.status === "open" ? " (hold — reconcile required)" : ""}
              {" "}· reserved {fmtMicro(reservation.reserved_micro)}</>
          : "no reservation row"}
        {" "}(synthetic — no real charge exists)
      </p>

      {links && (
        <p className="mt-1 text-[10px] text-faint" data-testid={`ci-links-${job.id}`}>
          linked: {links}
        </p>
      )}

      {/* result — the C3-A job already registered the asset; this only
          navigates to it, never writes a second copy. The thumbnail is
          fetched with header auth → object URL: no token ever appears
          in a URL, and a missing/unauthorized image fails safe. */}
      {job.status === "succeeded" && job.output_asset_id && (
        <div className="mt-3 flex items-start gap-3">
          {thumb && thumb !== "error" ? (
            <img src={thumb}
              alt="Simulated cloud image result"
              className="h-24 w-24 rounded border border-line object-cover"
              data-testid={`ci-thumb-${job.id}`} />
          ) : (
            <div className="flex h-24 w-24 items-center justify-center rounded border border-line bg-card text-center text-[10px] text-faint"
              data-testid={`ci-thumb-fallback-${job.id}`}>
              {thumb === "error" ? "preview unavailable" : "loading…"}
            </div>
          )}
          <div className="text-xs">
            <p className="text-fg">Simulated test image — saved to assets.</p>
            <p className="mt-0.5 text-[10px] text-faint">
              provenance: simulated · deterministic test PNG, not provider output
            </p>
            <button onClick={onViewAssets}
              className="mt-1.5 rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg"
              data-testid={`ci-view-assets-${job.id}`}>
              View in Assets
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
