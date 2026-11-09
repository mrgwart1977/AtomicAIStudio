# Outstanding release requirements

Tracked, non-blocking items before a public release. Opened at M1; none are
closed yet.

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | **Clean Windows VM install verification** | open | M0.1/M1/M1.1 validated on the dev machine (env-scrubbed + real install). Still needs a pristine VM/Hyper-V run: install → launch → screens → uninstall. |
| 2 | **Windows code signing** | open | Unsigned NSIS → SmartScreen warning. Needs a cert decision (EV OV vs. self-sign + documented fingerprint). |
| 3 | **Security review of test-only env overrides** | open | `ATOMIC_SIDECAR_TOKEN` (fixed token for automation) and `ATOMIC_PYTHON` (interpreter override). Decide: keep documented, gate behind debug builds, or remove for release. |
| 4 | **Installer upgrade path + migration testing** | open | Per-user NSIS install over an existing install works; 0001→0002 (M0.1→M1) and 0002→0003 (M1→M1.1) both observed on the installed DB. Still needs a scripted upgrade matrix incl. old-installer→new-installer and downgrade-refusal behavior. |
| 5 | Model download approval workflow | open | M3 gate: name/version/source/license/size/VRAM/use/alternatives must precede any download. |
| 6 | First verified local adapter | open | M3: pick after hardware survey; never pre-commit. |
| 7 | Usability test with target users | open | M9: moderated beginner sessions. |
| 8 | Network-share backup soak test | open | M1.1: UNC path validation exists; real NAS/SMB backup+cancel cycle needs an actual share to verify (latency, disconnect mid-write). |
