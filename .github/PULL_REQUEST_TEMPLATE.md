## Summary

<!-- What and — more importantly — why. -->

## Checklist

- [ ] `scripts\validate.ps1` passes locally (or CI is green)
- [ ] New/changed behavior has tests; no real model downloads,
      cloud calls, or inference in tests
- [ ] No secrets, credentials, or personal data added
- [ ] Project-owned data paths enforce `project_id` isolation
- [ ] Spec amendments extend `docs/handoff/` rather than silently remove
- [ ] Mock adapters remain labeled `adapter_kind="mock"`

## Test plan
