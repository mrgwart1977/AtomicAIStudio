# N2-C — Audio Gain and Track Controls

Gain/mute/solo as durable timeline state. This milestone pins the
**control model** — persistent fields, validated commands, undo,
snapshots, EDL metadata, and the UI. The mix engine that *applies*
these decisions during render is N2-E; populated visible audio lanes
still refuse export until then.

## Storage (single representation)

| Field | Model | Default | Meaning |
|---|---|---|---|
| `gain_db` | `timeline_clips` | `0.0` | per-clip gain, decibels |
| `gain_db` | `timeline_tracks` | `0.0` | per-lane gain, decibels |
| `solo` | `timeline_tracks` | `false` | lane joins the solo set |
| `muted` | `timeline_tracks` | `false` | lane silenced (pre-N1) |

Migration `0015_n2c` is additive and idempotent; existing rows
backfill to unity (`0.0` / `false`).

**Decibels are the only stored representation.** Linear amplitude is
derived (`10 ** (db / 20)`) at mix time — there is no parallel linear
column that could disagree. Bounds: `[-60, +12] dB` — +12 is a
beginner-safe headroom cap. **−60 dB is attenuation, not
silence** (~0.1 % amplitude remains audible in a mix context); the
actual silence control is `muted`. NaN/±inf are refused by
`check_gain_db` regardless of the entry point.

## Semantics

- **Unity** = `0.0 dB` (linear 1.0). Negative attenuates, positive
  amplifies.
- **Effective gain** = clip gain × track gain in linear, i.e. the dB
  values **add**: `effective_db = clip.gain_db + track.gain_db`.
- **Audibility precedence** (`lane_audible`), in order:
  1. `visible=false` → render-inert, produces no audio;
  2. `muted=true` → silent;
  3. any lane soloed → every non-soloed lane is silent.
  Multiple soloed lanes are **all** audible; an empty solo set leaves
  mute as the only silencer.
- **Per-clip gain applies to clips on audio lanes only.** Video clips
  carry embedded audio whose level is a lane/mix decision — video
  lanes accept `gain_db`/`muted`/`solo` exactly like audio lanes and
  the rules above apply to the embedded stream at N2-E.
- **`gain_db`/`solo` on overlay lanes are refused** — overlays have
  no audio output, so mix controls there would be ambiguous.
- Mix controls are **settable on locked tracks** — lock guards clip
  edits, not mix state (unlock must remain reachable anyway).

### Audio/video separation policy (review-pinned)

Audibility controls never touch compositing:

- **`muted` on a video lane suppresses its embedded audio only.** The
  picture still composites — `composite_plan` gates the overlay chain
  on `visible`, while `muted` only filters the clip out of
  `audio_inputs`. (`test_plan_muted_lane_keeps_video_drops_audio`)
- **`solo` is an audio-set membership flag, never a visibility flag.**
  The composite plan never consults `solo`, so soloing an audio lane
  cannot hide any picture. (`test_solo_never_removes_video_from_composite`)
- **Video lanes ARE included in the audio solo set** — a soloed video
  lane isolates its embedded audio exactly like a soloed audio lane.
  This keeps one unambiguous rule: the solo set decides which
  audio-producing lanes are heard, whatever their kind.
- **Hidden video lanes produce no embedded audio** — `visible=false`
  removes the lane from the composite plan entirely, so its audio
  can't reach the mix. Same rule for hidden audio lanes
  (`lane_audible` returns false first).
- **One slider adjustment = one command = one undo step.** The
  `GainSlider` drafts locally and commits once on release (or click
  readout to reset), so dragging can't flood the history.

## Commands

| Command | Payload | Behavior |
|---|---|---|
| `timeline.clip.gain` | `clip_id`, `gain_db` | sets clip gain; audio-lane clips only |
| `timeline.track.update` | `track_id`, `gain_db`/`solo` (+existing fields) | same command, extended field set |

Both go through preview → commit → audit with expected-revision
checks, idempotency keys, undo/redo (revision snapshots capture the
new columns), and project isolation. Reset-to-unity is simply
`gain_db: 0`; "clear current gain" = reset on that control. No
redundant fields → nothing can disagree.

## EDL v2

`ade-edl/2` gains — deterministically, sorted-document hash changes
accordingly:

- track entries: `gain_db`, `solo` (`muted`/`visible` already present)
- clip entries: `gain_db`

`ade-edl/1` is untouched and carries no mix metadata. Video-only v2
documents gain the fields with unity defaults — additive only.

## UI

- **Lane header mix row** (audio + video lanes only, +22 px):
  `GainSlider` −60…+12 dB, dB readout that resets to unity on click,
  "soloed" indicator.
- **Solo** toggle sits next to Mute in the lane controls row.
- **Clip inspector**: `Clip volume` slider + effective readout
  (`clip + track`) for audio clips.
- `GainSlider` drafts locally while dragging and commits **one**
  command on release/Enter/blur — one adjustment = one undo step.
  Native `<input type=range>` gives keyboard control (arrows ±0.5,
  Shift ±5, Home/End); every control carries a label and tooltip.

## Out of scope

Mixing engine, metering, fades/crossfades (N2-D/E/F), gain
automation, audio preview playback.
